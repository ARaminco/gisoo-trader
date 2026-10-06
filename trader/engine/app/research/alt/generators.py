"""Candidate entry logics. Each gen_<name>(window, symbol, tf) -> decision frame.

Every generator returns the schema consumed by the portfolio simulator
(open_time/ohlcv/side/score/regime/atr/atr_ratio/adx/htf_bias/in_window) plus
the hook columns stop_dist/target_dist/exit read by alt.sim.simulate_ext.
Score is the strategy's own quality measure in 0..1 and is always >= 0.5 on a
valid signal, so `min_score` above 0.5 tightens the filter.

STRATEGIES lists the core candidates with the SimParams that express their
exit rules; VARIANTS are secondary experiments run with --variants.

The two families the live engine runs (xs_momentum_regime_exit and
turtle_regime_risk1) take their rules and sizing from app.strategies.core4h;
the generators here only load the cached features, align the BTC context and
assemble the decision frame, so the simulator and the engine cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from app.indicators import core as ta
from app.research.alt.features import HTF, assemble, context, features
from app.research.alt.sim import AltParams
from app.research.windows import SYMBOLS, Window
from app.strategies import core4h

NO_TARGET = 1_000.0      # atr_target multiple that is never reached
NO_TIME_STOP = 1e9       # time_stop_hours that never fires


# --------------------------------------------------------------------------
# 1. donchian_trend - turtle-style breakout with the 4h trend
# --------------------------------------------------------------------------


def gen_donchian_trend(
    window: Window, symbol: str, tf: str = "4h", period: int = 20, adx_min: float = 20.0
) -> pd.DataFrame | None:
    """Long when close > previous `period`-bar Donchian high, 4h EMA200 rising, ADX > adx_min."""
    f = features(window, symbol, tf)
    if f is None:
        return None
    ctx = context(window, symbol, f, tf)
    if ctx is None:
        return None
    close, atr = f["close"], f["atr"]
    # dc_upper includes the current bar, so the breakout reference is the prior bar's channel
    prev_high = (f["dc_upper"] if period == 20 else f["high"].rolling(period, min_periods=period).max()).shift(1)
    adx = f["adx"] if tf == HTF else ctx["adx"]
    signal = (close > prev_high) & (ctx["ema200_rising"] > 0) & (adx > adx_min)
    strength = ((close - prev_high) / atr).clip(0, 1)
    score = 0.5 + 0.25 * ((adx - adx_min) / 20.0).clip(0, 1) + 0.25 * strength
    side = np.where(signal, "long", "flat")
    return assemble(window, f, side, score, ctx["bias"])


# --------------------------------------------------------------------------
# 2. ema_pullback - buy the dip in a 4h uptrend, structure stop, 2.5R target
# --------------------------------------------------------------------------


def gen_ema_pullback(
    window: Window, symbol: str, tf: str = "30m", lookback: int = 12, rsi_max: float = 45.0,
    min_r_atr: float = 1.5, max_r_atr: float = 3.0, target_r: float = 2.5,
) -> pd.DataFrame | None:
    """4h EMA20 > EMA50 and close > EMA200; a pullback to/below EMA20 with RSI < rsi_max in the
    last `lookback` bars; enter on the first bullish close back above EMA9. Stop = pullback low
    (at least min_r_atr ATR, skip if beyond max_r_atr ATR), target = target_r R."""
    f = features(window, symbol, tf)
    if f is None:
        return None
    ctx = context(window, symbol, f, tf)
    if ctx is None:
        return None
    close, open_, low, atr, rsi = f["close"], f["open"], f["low"], f["atr"], f["rsi"]
    ema9, ema20 = f["ema_9"], f["ema_20"]

    uptrend = (ctx["ema20"] > ctx["ema50"]) & (ctx["close"] > ctx["ema200"])
    pull = ((close <= ema20) & (rsi < rsi_max)).astype(float)
    recent_pull = pull.shift(1).rolling(lookback, min_periods=1).max() > 0
    cross_up = (close > ema9) & (close.shift(1) <= ema9) & (close > open_)
    pull_low = low.rolling(lookback + 1, min_periods=1).min()
    risk = (close - pull_low).clip(lower=min_r_atr * atr)
    structure_ok = risk <= max_r_atr * atr
    signal = uptrend & recent_pull & cross_up & structure_ok

    rsi_min = rsi.rolling(lookback, min_periods=1).min()
    score = (
        0.5
        + 0.20 * ((rsi_max - rsi_min) / 25.0).clip(0, 1)
        + 0.15 * f["body_ratio"].fillna(0.0).clip(0, 1)
        + 0.15 * (ctx["adx"] / 40.0).clip(0, 1)
    )
    side = np.where(signal, "long", "flat")
    return assemble(window, f, side, score, ctx["bias"], stop_dist=risk, target_dist=target_r * risk)


# --------------------------------------------------------------------------
# 3. rsi2_reversion - Connors RSI(2) dip in a 4h uptrend
# --------------------------------------------------------------------------


def gen_rsi2_reversion(
    window: Window, symbol: str, tf: str = "1h", entry: float = 10.0, exit_level: float = 65.0,
    signal_exit: bool = True,
) -> pd.DataFrame | None:
    """close > 4h EMA200 and RSI(2) < entry -> long at close; exit column = RSI(2) > exit_level."""
    f = features(window, symbol, tf)
    if f is None:
        return None
    ctx = context(window, symbol, f, tf)
    if ctx is None:
        return None
    rsi2 = ta.rsi(f["close"], 2)
    signal = (ctx["close"] > ctx["ema200"]) & (rsi2 < entry)
    score = 0.5 + 0.5 * ((entry - rsi2) / entry).clip(0, 1)
    side = np.where(signal, "long", "flat")
    exit_flag = (rsi2 > exit_level) if signal_exit else None
    return assemble(window, f, side, score, ctx["bias"], exit_signal=exit_flag)


# --------------------------------------------------------------------------
# 4. xs_momentum - cross-sectional momentum, daily rebalance, BTC regime gate
# --------------------------------------------------------------------------

_XS_CACHE: dict[tuple, core4h.MomentumPanel] = {}


def _xs_table(
    window: Window, tf: str, symbols: list[str], lookback_days: int = 7, skip_days: int = 1
) -> core4h.MomentumPanel:
    """Panel of momentum ranks (1 = best) on every bar, rebalance flags, BTC regime flag."""
    key = (window.id, tf, tuple(symbols), lookback_days, skip_days)
    if key in _XS_CACHE:
        return _XS_CACHE[key]
    closes = {}
    for s in symbols:
        f = features(window, s, tf)
        if f is not None:
            closes[s] = f.drop_duplicates("open_time").set_index("open_time")["close"]
    btc = features(window, "BTCUSDT", tf)
    btc_ok = None if btc is None else core4h.regime_series(btc)
    table = core4h.momentum_panel(closes, btc_ok, tf, lookback_days, skip_days)
    _XS_CACHE[key] = table
    return table


def gen_xs_momentum(
    window: Window, symbol: str, tf: str = "4h", top_n: int = 3, hold_n: int = 5, regime_exit: bool = False,
    lookback_days: int = 7, skip_days: int = 1, btc_gate: bool = True,
) -> pd.DataFrame | None:
    """Every 24h rank symbols by (lookback-day return - skip-day return); long the top_n when BTC
    is above its 4h EMA200 (btc_gate); exit when the name drops below rank hold_n (and, if
    regime_exit, when BTC closes below its EMA200). The rules are app.strategies.core4h's."""
    f = features(window, symbol, tf)
    if f is None:
        return None
    ctx = context(window, symbol, f, tf)
    if ctx is None:
        return None
    table = _xs_table(window, tf, SYMBOLS, lookback_days, skip_days)
    if symbol not in table.symbols:
        return None
    rank, rebalance, btc_ok = table.for_symbol(symbol, f["open_time"].to_numpy())
    signal, score, exit_flag = core4h.momentum_arrays(
        rank, rebalance, btc_ok, top_n=top_n, hold_n=hold_n, regime_exit=regime_exit, btc_gate=btc_gate
    )
    side = np.where(signal, "long", "flat")
    return assemble(window, f, side, score, ctx["bias"], exit_signal=exit_flag)


# --------------------------------------------------------------------------
# 5. vol_breakout_4h - compression then expansion with volume
# --------------------------------------------------------------------------


def gen_vol_breakout(
    window: Window, symbol: str, tf: str = "4h", tr_frac: float = 0.6, bbw_q: float = 0.2,
    volz_min: float = 1.0, setup_bars: int = 3, min_r_atr: float = 0.75, max_r_atr: float = 3.0,
    target_atr: float = 3.0,
) -> pd.DataFrame | None:
    """Compression (last bar true range < tr_frac * ATR20 and Bollinger width in the bottom bbw_q of
    100 bars) within the previous setup_bars, then a close outside the band with volume z > volz_min.
    Stop at the compression low (at least min_r_atr ATR), target target_atr ATR."""
    f = features(window, symbol, tf)
    if f is None:
        return None
    ctx = context(window, symbol, f, tf)
    if ctx is None:
        return None
    h, l, c, atr = f["high"], f["low"], f["close"], f["atr"]
    tr = ta.true_range(h, l, c)
    atr20 = ta.atr(h, l, c, 20)
    bbw_pct = f["bb_width"].rolling(100, min_periods=50).rank(pct=True)
    compressed = ((tr < tr_frac * atr20) & (bbw_pct <= bbw_q)).astype(float)
    setup = compressed.shift(1).rolling(setup_bars, min_periods=1).max() > 0
    vol_ok = f["vol_z"] > volz_min

    comp_low = l.rolling(setup_bars + 1, min_periods=1).min()
    comp_high = h.rolling(setup_bars + 1, min_periods=1).max()
    risk_long = (c - comp_low).clip(lower=min_r_atr * atr)
    risk_short = (comp_high - c).clip(lower=min_r_atr * atr)
    long_sig = setup & (c > f["bb_upper"]) & vol_ok & (risk_long <= max_r_atr * atr)
    short_sig = setup & (c < f["bb_lower"]) & vol_ok & (risk_short <= max_r_atr * atr) & ~long_sig

    side = np.where(long_sig, "long", np.where(short_sig, "short", "flat"))
    dist = pd.Series(np.where(long_sig, c - f["bb_upper"], f["bb_lower"] - c), index=f.index) / atr
    tightness = bbw_pct.shift(1).rolling(setup_bars, min_periods=1).min()
    score = (
        0.5
        + 0.20 * ((f["vol_z"] - volz_min) / 2.0).clip(0, 1)
        + 0.15 * dist.clip(0, 1)
        + 0.15 * (1.0 - tightness / bbw_q).clip(0, 1)
    )
    stop_dist = pd.Series(np.where(long_sig, risk_long, risk_short), index=f.index)
    return assemble(window, f, side, score, ctx["bias"], stop_dist=stop_dist, target_dist=target_atr * atr)


# --------------------------------------------------------------------------
# 6. turtle_regime (own candidate) - market-gated 55/20 Donchian channel system
# --------------------------------------------------------------------------


def gen_turtle_regime(
    window: Window, symbol: str, tf: str = "4h", entry_period: int = 55, exit_period: int = 20,
    market_symbol: str = "BTCUSDT", market_gate: bool = True, require_rising: bool = True,
) -> pd.DataFrame | None:
    """Market gate: BTC close > BTC 4h EMA200 and (require_rising) that EMA200 rising. Entry: close >
    previous entry_period-bar high and symbol above its own EMA200. Exit: close < previous
    exit_period-bar low, or BTC closes below its EMA200 (flatten everything). Initial stop 2.5 ATR,
    no target. market_gate=False removes the BTC condition entirely (ablation).
    The rules are app.strategies.core4h's."""
    f = features(window, symbol, tf)
    if f is None:
        return None
    ctx = context(window, symbol, f, tf)
    market = context(window, symbol, f, tf, ctx_symbol=market_symbol)
    if ctx is None or market is None:
        return None
    out = core4h.turtle_arrays(
        f, market["close"], market["ema200"], market["ema200_rising"] > 0,
        entry_period=entry_period, exit_period=exit_period,
        market_gate=market_gate, require_rising=require_rising,
    )
    side = np.where(out.signal, "long", "flat")
    return assemble(window, f, side, out.score, ctx["bias"], exit_signal=out.exit)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def family_params(family: core4h.Family, **extra) -> AltParams:
    """AltParams for a live family: exactly what the study validated it with."""
    return AltParams(
        min_score=family.min_score, atr_stop=family.atr_stop, atr_target=NO_TARGET, trailing=False,
        time_stop_hours=NO_TIME_STOP, cooldown_min=family.cooldown_min, min_rr=0.0,
        risk_pct=family.risk_pct, max_position_pct=family.max_position_pct, **extra,
    )


@dataclass(frozen=True)
class Spec:
    name: str
    gen: Callable[..., pd.DataFrame | None]
    tf: str
    params: AltParams
    rules: str
    gen_kwargs: dict = field(default_factory=dict)
    variant_of: str | None = None


STRATEGIES: dict[str, Spec] = {
    "donchian_trend": Spec(
        "donchian_trend", gen_donchian_trend, "4h",
        AltParams(min_score=0.5, atr_stop=2.0, atr_target=NO_TARGET, trailing=True, trail_activate_r=1.0,
                  trail_atr=3.0, time_stop_hours=NO_TIME_STOP, cooldown_min=240, min_rr=0.0),
        "4h close > prior 20-bar Donchian high, EMA200(4h) rising over 24h, ADX(4h) > 20. "
        "Stop 2 ATR, no target, 3 ATR trail once +1R (trail floor = breakeven), no time stop.",
    ),
    "ema_pullback": Spec(
        "ema_pullback", gen_ema_pullback, "30m",
        AltParams(min_score=0.5, atr_stop=1.0, atr_target=2.5, trailing=True, trail_activate_r=1.0,
                  trail_atr=2.0, time_stop_hours=24.0, cooldown_min=120, min_rr=1.3, min_move_pct=0.5),
        "4h EMA20 > EMA50 and close > EMA200. 30m: pullback to/below EMA20 with RSI(14) < 45 in the last "
        "12 bars, then the first bullish close back above EMA9. Stop = pullback low (>= 1.5 ATR, skip if "
        "> 3 ATR), target 2.5R, 2 ATR trail after +1R, flat-trade time stop 24h.",
    ),
    "rsi2_reversion": Spec(
        "rsi2_reversion", gen_rsi2_reversion, "1h",
        AltParams(min_score=0.5, atr_stop=3.0, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  hard_time_stop_hours=8.0, cooldown_min=60, min_rr=0.0, min_move_pct=0.3),
        "1h close > 4h EMA200 and RSI(2) < 10 -> long at close. Exit at close when RSI(2) > 65 or after "
        "8 bars, hard stop 3 ATR, no target, no trailing.",
    ),
    "xs_momentum": Spec(
        "xs_momentum", gen_xs_momentum, "4h",
        AltParams(min_score=0.75, atr_stop=3.0, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=0, min_rr=0.0),
        "Every 00:00 UTC 4h bar rank the 12 symbols by 7-day return minus 1-day return; long the top 3 "
        "(score 1.0/0.9/0.8) only if BTC > its 4h EMA200. Hold until the name ranks below 5 at a "
        "rebalance. Stop 3 ATR, no target, no trailing.",
    ),
    "vol_breakout_4h": Spec(
        "vol_breakout_4h", gen_vol_breakout, "4h",
        AltParams(min_score=0.5, atr_stop=1.0, atr_target=3.0, trailing=True, trail_activate_r=1.0,
                  trail_atr=2.0, time_stop_hours=NO_TIME_STOP, cooldown_min=240, min_rr=1.0, min_move_pct=0.5),
        "4h: a bar with true range < 60% of ATR(20) and Bollinger width in the bottom 20% of 100 bars "
        "within the previous 3 bars, then a close above the upper band with volume z-score > 1. Stop at "
        "the compression low (>= 0.75 ATR, skip if > 3 ATR), target 3 ATR, 2 ATR trail after +1R.",
    ),
    "turtle_regime": Spec(
        "turtle_regime", gen_turtle_regime, "4h",
        AltParams(min_score=0.5, atr_stop=2.5, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=240, min_rr=0.0),
        "Own candidate. Market gate: BTC > BTC 4h EMA200 with that EMA rising. Entry: 4h close > prior "
        "55-bar high and close > own EMA200. Exit: close < prior 20-bar low, or BTC closes below its "
        "EMA200 (flatten all). Stop 2.5 ATR, no target, no trailing.",
    ),
}

VARIANTS: dict[str, Spec] = {
    "donchian_trend_1h": Spec(
        "donchian_trend_1h", gen_donchian_trend, "1h",
        AltParams(min_score=0.5, atr_stop=2.0, atr_target=NO_TARGET, trailing=True, trail_activate_r=1.0,
                  trail_atr=3.0, time_stop_hours=NO_TIME_STOP, cooldown_min=60, min_rr=0.0),
        "donchian_trend on 1h bars (20-hour channel), 4h EMA200 rising and 4h ADX > 20.",
        variant_of="donchian_trend",
    ),
    "donchian_trend_noadx": Spec(
        "donchian_trend_noadx", gen_donchian_trend, "4h",
        AltParams(min_score=0.5, atr_stop=2.0, atr_target=NO_TARGET, trailing=True, trail_activate_r=1.0,
                  trail_atr=3.0, time_stop_hours=NO_TIME_STOP, cooldown_min=240, min_rr=0.0),
        "donchian_trend without the ADX filter.", gen_kwargs={"adx_min": 0.0}, variant_of="donchian_trend",
    ),
    "ema_pullback_1h": Spec(
        "ema_pullback_1h", gen_ema_pullback, "1h",
        AltParams(min_score=0.5, atr_stop=1.0, atr_target=2.5, trailing=True, trail_activate_r=1.0,
                  trail_atr=2.0, time_stop_hours=48.0, cooldown_min=120, min_rr=1.3, min_move_pct=0.5),
        "ema_pullback on 1h entry bars.", variant_of="ema_pullback",
    ),
    "rsi2_reversion_pure": Spec(
        "rsi2_reversion_pure", gen_rsi2_reversion, "1h",
        AltParams(min_score=0.5, atr_stop=3.0, atr_target=1.5, trailing=False, time_stop_hours=8.0,
                  cooldown_min=60, min_rr=0.4, min_move_pct=0.3),
        "rsi2_reversion expressed only with the original simulator: 1.5 ATR target stands in for the "
        "RSI(2) > 65 exit, the built-in 8h time stop (flat trades only), 3 ATR stop.",
        gen_kwargs={"signal_exit": False}, variant_of="rsi2_reversion",
    ),
    "rsi2_reversion_deep": Spec(
        "rsi2_reversion_deep", gen_rsi2_reversion, "1h",
        AltParams(min_score=0.75, atr_stop=3.0, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  hard_time_stop_hours=8.0, cooldown_min=60, min_rr=0.0, min_move_pct=0.3),
        "rsi2_reversion with min_score 0.75 (RSI(2) < 5).", variant_of="rsi2_reversion",
    ),
    # The live engine's base layer (STRATEGY_CORE=portfolio4h, family "momentum").
    "xs_momentum_regime_exit": Spec(
        "xs_momentum_regime_exit", gen_xs_momentum, "4h", family_params(core4h.MOMENTUM),
        "xs_momentum that also flattens when BTC closes below its 4h EMA200.",
        gen_kwargs={"regime_exit": True}, variant_of="xs_momentum",
    ),
    "xs_momentum_30d": Spec(
        "xs_momentum_30d", gen_xs_momentum, "4h",
        AltParams(min_score=0.75, atr_stop=3.0, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=0, min_rr=0.0),
        "xs_momentum ranked on 30-day return minus 1-day return, with the BTC regime exit.",
        gen_kwargs={"regime_exit": True, "lookback_days": 30}, variant_of="xs_momentum",
    ),
    "vol_breakout_4h_loose": Spec(
        "vol_breakout_4h_loose", gen_vol_breakout, "4h",
        AltParams(min_score=0.5, atr_stop=1.0, atr_target=3.0, trailing=True, trail_activate_r=1.0,
                  trail_atr=2.0, time_stop_hours=NO_TIME_STOP, cooldown_min=240, min_rr=1.0, min_move_pct=0.5),
        "vol_breakout_4h with looser compression (TR < 80% ATR20, width in bottom 35%, vol z > 0.5).",
        gen_kwargs={"tr_frac": 0.8, "bbw_q": 0.35, "volz_min": 0.5}, variant_of="vol_breakout_4h",
    ),
    "turtle_regime_trail": Spec(
        "turtle_regime_trail", gen_turtle_regime, "4h",
        AltParams(min_score=0.5, atr_stop=2.5, atr_target=NO_TARGET, trailing=True, trail_activate_r=1.0,
                  trail_atr=3.0, time_stop_hours=NO_TIME_STOP, cooldown_min=240, min_rr=0.0),
        "turtle_regime with a 3 ATR trail after +1R on top of the channel exit.", variant_of="turtle_regime",
    ),
    "turtle_regime_20": Spec(
        "turtle_regime_20", gen_turtle_regime, "4h",
        AltParams(min_score=0.5, atr_stop=2.5, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=240, min_rr=0.0),
        "turtle_regime with a 20-bar entry channel and 10-bar exit channel (faster).",
        gen_kwargs={"entry_period": 20, "exit_period": 10}, variant_of="turtle_regime",
    ),
    # ---- ablations: is the edge the market gate or the entry logic? ----
    "turtle_regime_nogate": Spec(
        "turtle_regime_nogate", gen_turtle_regime, "4h",
        AltParams(min_score=0.5, atr_stop=2.5, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=240, min_rr=0.0),
        "turtle_regime with the BTC market gate removed (own EMA200 filter only).",
        gen_kwargs={"market_gate": False}, variant_of="turtle_regime",
    ),
    "turtle_regime_norise": Spec(
        "turtle_regime_norise", gen_turtle_regime, "4h",
        AltParams(min_score=0.5, atr_stop=2.5, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=240, min_rr=0.0),
        "turtle_regime with the gate reduced to BTC > EMA200 (no rising requirement).",
        gen_kwargs={"require_rising": False}, variant_of="turtle_regime",
    ),
    "turtle_regime_40_15": Spec(
        "turtle_regime_40_15", gen_turtle_regime, "4h",
        AltParams(min_score=0.5, atr_stop=2.5, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=240, min_rr=0.0),
        "turtle_regime with 40/15 channels (between the 55/20 and 20/10 settings).",
        gen_kwargs={"entry_period": 40, "exit_period": 15}, variant_of="turtle_regime",
    ),
    # The live engine's upside layer (STRATEGY_CORE=portfolio4h, family "turtle").
    "turtle_regime_risk1": Spec(
        "turtle_regime_risk1", gen_turtle_regime, "4h", family_params(core4h.TURTLE),
        "turtle_regime sized at 1.0% risk and a 25% position cap (drawdown control).",
        variant_of="turtle_regime",
    ),
    "xs_momentum_nogate": Spec(
        "xs_momentum_nogate", gen_xs_momentum, "4h",
        AltParams(min_score=0.75, atr_stop=3.0, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=0, min_rr=0.0),
        "xs_momentum with the BTC gate removed: pure cross-sectional ranking.",
        gen_kwargs={"btc_gate": False}, variant_of="xs_momentum",
    ),
    "xs_momentum_top2": Spec(
        "xs_momentum_top2", gen_xs_momentum, "4h",
        AltParams(min_score=0.75, atr_stop=3.0, atr_target=NO_TARGET, trailing=False, time_stop_hours=NO_TIME_STOP,
                  cooldown_min=0, min_rr=0.0, max_positions=2),
        "xs_momentum_regime_exit holding only the top 2 (exit below rank 4).",
        gen_kwargs={"regime_exit": True, "top_n": 2, "hold_n": 4}, variant_of="xs_momentum",
    ),
}

ALL: dict[str, Spec] = {**STRATEGIES, **VARIANTS}
