"""The 4h portfolio core: two BTC-regime-gated strategy families.

Both families were tested on twelve historical regime windows, 2021-2026,
with the research simulator in app/research. The rules live here, not in the research
generators, so the live engine and the simulator that validated them share one
definition - `app.research.alt.generators` calls these functions and the
research run must reproduce its published numbers exactly.

  momentum  (research name xs_momentum_regime_exit)
      On every 4h bar that opens at 00:00 UTC rank the universe by 7-day
      return minus 1-day return. Long the top 3 (score 1.0 / 0.9 / 0.8) while
      BTC's 4h close is above its EMA200. Exit at the close when the name
      ranks below 5 at a rebalance, or on any bar where BTC closes under its
      EMA200. 3 ATR stop, no target, no trailing, no cooldown; 1.5% risk,
      35% position cap.

  turtle    (research name turtle_regime_risk1)
      Gate: BTC 4h close above its EMA200 and that EMA above its value 24h
      earlier. Enter on a 4h close above the previous 55-bar high while the
      symbol is above its own EMA200. Exit at the close below the previous
      20-bar low, or when BTC closes under its EMA200. 2.5 ATR stop, no
      target, no trailing, 240 min cooldown after a loss; 1% risk, 25%
      position cap - the sizing that keeps the family inside the kill switch.

Pure pandas/numpy: no database, no asyncio, no settings. Frames are what
`app.features.build.build_features` returns (open_time, ohlcv, atr, adx,
ema_200). Channel references use the previous bar; the BTC gate reads the BTC
bar with the same open time, which closes at the same instant as the decision
bar, so nothing here looks ahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.util.timeframes import tf_seconds

TIMEFRAME = "4h"
MARKET_SYMBOL = "BTCUSDT"
# EMA200 plus the 55-bar channel need this much history before the first
# decision can be trusted (same floor as the research feature loader).
MIN_BARS = 260
DAY_MS = 86_400_000
# "EMA200 rising" compares with the value 6 bars = 24h earlier on 4h.
EMA_RISING_BARS = 6

# ---- momentum rules ----
LOOKBACK_DAYS = 7
SKIP_DAYS = 1
TOP_N = 3
HOLD_N = 5

# ---- turtle rules ----
ENTRY_PERIOD = 55
EXIT_PERIOD = 20


@dataclass(frozen=True)
class Family:
    """One strategy family and the sizing the study validated it with."""

    name: str
    research_name: str
    risk_pct: float
    max_position_pct: float
    atr_stop: float
    cooldown_min: int
    min_score: float
    # Every validated run of a family held at most this many positions at
    # once (`research.portfolio.PortfolioParams.max_positions`). The live
    # engine enforces it per family, whatever the account-wide cap is set to,
    # so the exposure stays the one the study measured.
    max_positions: int = 3


MOMENTUM = Family("momentum", "xs_momentum_regime_exit", 1.5, 35.0, 3.0, 0, 0.75)
TURTLE = Family("turtle", "turtle_regime_risk1", 1.0, 25.0, 2.5, 240, 0.5)
FAMILIES: dict[str, Family] = {f.name: f for f in (MOMENTUM, TURTLE)}


# --------------------------------------------------------------------------
# BTC regime
# --------------------------------------------------------------------------


def regime_series(btc: pd.DataFrame) -> pd.Series:
    """BTC close above its EMA200, indexed by open_time (True = risk on)."""
    b = btc.drop_duplicates("open_time").set_index("open_time")
    return b["close"] > b["ema_200"]


def btc_regime_ok(btc: pd.DataFrame) -> bool:
    """The gate on the latest closed BTC bar."""
    if btc is None or btc.empty:
        return False
    row = btc.iloc[-1]
    return bool(row["close"] > row["ema_200"])


def align_market(market: pd.DataFrame, open_times: np.ndarray, index: pd.Index) -> pd.DataFrame:
    """BTC context for each decision bar: the BTC bar with the same open time.

    If that bar is missing the last earlier one is used, which is the same
    rule the research harness applies for same-timeframe context. Columns:
    close, ema200, rising (EMA200 above its value EMA_RISING_BARS earlier).
    """
    m = market.drop_duplicates("open_time")
    market_open = m["open_time"].to_numpy()
    idx = np.searchsorted(market_open, open_times, side="right") - 1
    valid = idx >= 0
    safe = np.clip(idx, 0, len(m) - 1)

    def take(values: np.ndarray) -> np.ndarray:
        return np.where(valid, values[safe], np.nan)

    ema = m["ema_200"]
    rising = (ema > ema.shift(EMA_RISING_BARS)).astype(float).to_numpy()
    return pd.DataFrame(
        {
            "close": take(m["close"].to_numpy(dtype=float)),
            "ema200": take(ema.to_numpy(dtype=float)),
            "rising": take(rising) > 0,
        },
        index=index,
    )


# --------------------------------------------------------------------------
# momentum family
# --------------------------------------------------------------------------


@dataclass
class MomentumPanel:
    """Cross-sectional ranks on every bar of the universe."""

    rank: pd.DataFrame        # 1 = strongest, NaN before the lookback is filled
    momentum: pd.DataFrame    # lookback return minus skip return
    rebalance: pd.Series      # True on bars opening at 00:00 UTC
    btc_ok: pd.Series         # BTC above its EMA200 on that bar

    @property
    def symbols(self) -> list[str]:
        return list(self.rank.columns)

    def for_symbol(self, symbol: str, open_times: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(rank, rebalance, btc_ok) aligned to one symbol's bars."""
        rank = self.rank[symbol].reindex(open_times).to_numpy()
        rebalance = self.rebalance.reindex(open_times).fillna(False).to_numpy(dtype=bool)
        btc_ok = self.btc_ok.reindex(open_times).fillna(False).to_numpy(dtype=bool)
        return rank, rebalance, btc_ok


def momentum_panel(
    closes: dict[str, pd.Series],
    btc_ok: pd.Series | None,
    timeframe: str = TIMEFRAME,
    lookback_days: int = LOOKBACK_DAYS,
    skip_days: int = SKIP_DAYS,
) -> MomentumPanel:
    """Rank the universe on every bar.

    `closes` maps symbol -> close indexed by open_time; `btc_ok` is
    `regime_series(btc)` (None = no BTC data, which switches the gate off
    for every bar). Returns are measured in bars, so a symbol with a hole in
    its history is ranked on a slightly longer horizon on those bars - the
    same thing the research run did.
    """
    panel = pd.DataFrame(closes).sort_index()
    bars_per_day = int(DAY_MS / (tf_seconds(timeframe) * 1000))
    ret_long = panel / panel.shift(lookback_days * bars_per_day) - 1
    ret_short = panel / panel.shift(skip_days * bars_per_day) - 1
    momentum = ret_long - ret_short
    rank = momentum.rank(axis=1, ascending=False)
    rebalance = pd.Series((panel.index.to_numpy() % DAY_MS) == 0, index=panel.index)
    if btc_ok is None:
        ok = pd.Series(False, index=panel.index)
    else:
        ok = btc_ok.reindex(panel.index).fillna(False).astype(bool)
    return MomentumPanel(rank=rank, momentum=momentum, rebalance=rebalance, btc_ok=ok)


def momentum_arrays(
    rank: np.ndarray,
    rebalance: np.ndarray,
    btc_ok: np.ndarray,
    top_n: int = TOP_N,
    hold_n: int = HOLD_N,
    regime_exit: bool = True,
    btc_gate: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(signal, score, exit) per bar for one symbol from its aligned rank arrays."""
    if not btc_gate:
        btc_ok = np.ones(len(rank), dtype=bool)
    with np.errstate(invalid="ignore"):
        signal = rebalance & (rank <= top_n) & btc_ok
        dropped = rebalance & (rank > hold_n)
    score = np.where(signal, 1.0 - (np.nan_to_num(rank, nan=top_n) - 1) * 0.1, 0.0)
    exit_flag = dropped | (~btc_ok) if regime_exit else dropped
    return signal, score, exit_flag


# --------------------------------------------------------------------------
# turtle family
# --------------------------------------------------------------------------


@dataclass
class TurtleSignals:
    signal: pd.Series      # bool: breakout entry on this bar
    score: pd.Series
    exit: pd.Series        # bool: channel exit or market off
    entry_high: pd.Series  # previous bar's 55-bar high
    exit_low: pd.Series    # previous bar's 20-bar low
    market_on: pd.Series
    market_off: pd.Series


def turtle_arrays(
    f: pd.DataFrame,
    market_close: pd.Series,
    market_ema200: pd.Series,
    market_rising: pd.Series | None = None,
    entry_period: int = ENTRY_PERIOD,
    exit_period: int = EXIT_PERIOD,
    market_gate: bool = True,
    require_rising: bool = True,
) -> TurtleSignals:
    """Channel entries and exits for one symbol under the BTC gate.

    `market_*` are the BTC values aligned to `f`'s bars (`align_market`, or
    the research `context()`); `market_rising` is a boolean series and is
    required when `require_rising` is on.
    """
    close, atr, adx = f["close"], f["atr"], f["adx"]
    market_on = market_close > market_ema200
    if require_rising:
        market_on &= market_rising
    market_off = market_close < market_ema200
    if not market_gate:
        market_on = pd.Series(True, index=f.index)
        market_off = pd.Series(False, index=f.index)
    entry_high = f["high"].rolling(entry_period, min_periods=entry_period).max().shift(1)
    exit_low = f["low"].rolling(exit_period, min_periods=exit_period).min().shift(1)
    signal = (close > entry_high) & market_on & (close > f["ema_200"])
    exit_flag = (close < exit_low) | market_off
    score = 0.5 + 0.25 * ((close - entry_high) / atr).clip(0, 1) + 0.25 * ((adx - 15.0) / 25.0).clip(0, 1)
    return TurtleSignals(signal, score, exit_flag, entry_high, exit_low, market_on, market_off)


# --------------------------------------------------------------------------
# live view: one evaluation of the whole universe on a closed bar
# --------------------------------------------------------------------------


@dataclass
class Signal:
    """What one family says about one symbol on one closed bar."""

    symbol: str
    family: str
    open_time: int
    side: str                 # long / flat
    score: float
    price: float              # the bar close: the simulator's entry and exit price
    atr: float
    stop_price: float         # price - atr_stop x ATR (0 when flat)
    exit: bool                # an open position of this family must close at this bar
    exit_reason: str = ""     # signal_exit (rank / channel) or regime_exit (BTC gate)
    detail: dict = field(default_factory=dict)

    @property
    def tradable(self) -> bool:
        return self.side == "long" and self.score >= FAMILIES[self.family].min_score


@dataclass
class Universe:
    open_time: int
    market_present: bool      # BTC has a bar for open_time - nothing is decided without it
    regime_ok: bool           # BTC close > EMA200
    regime_rising: bool       # EMA200 above its value 24h earlier
    market_close: float
    market_ema200: float
    ranked: list[str]         # symbols in the momentum ranking
    signals: dict[tuple[str, str], Signal]

    def signal(self, family: str, symbol: str) -> Signal | None:
        return self.signals.get((family, symbol))

    def candidates(self) -> list[Signal]:
        """Long signals above their family's score floor, best first.

        The simulator sorts candidates by score; on a tie the universe order
        is kept, with momentum ahead of turtle for the same symbol.
        """
        longs = [s for s in self.signals.values() if s.tradable]
        return sorted(longs, key=lambda s: s.score, reverse=True)

    def exit_check(self, family: str, symbol: str) -> tuple[bool, str]:
        """(must exit, reason) for an open position of `family` in `symbol`."""
        signal = self.signal(family, symbol)
        if signal is None or not signal.exit:
            return False, ""
        return True, signal.exit_reason


def _bar_index(open_times: np.ndarray, open_time: int) -> int:
    pos = int(np.searchsorted(open_times, open_time))
    if pos >= len(open_times) or int(open_times[pos]) != open_time:
        return -1
    return pos


def evaluate_universe(
    features: dict[str, pd.DataFrame],
    open_time: int,
    market_symbol: str = MARKET_SYMBOL,
) -> Universe:
    """Evaluate both families for every symbol on the bar that opened at `open_time`.

    `features` maps symbol -> feature frame (oldest first, at least MIN_BARS
    rows). Symbols without a bar for `open_time` get no signal on this
    evaluation but still take part in the ranking of the others. Without the
    BTC bar the view carries no signals at all: the gate cannot be read, and
    a missing gate would otherwise read as "risk off" and flatten everything.
    """
    btc = features.get(market_symbol)
    if btc is None or _bar_index(btc["open_time"].to_numpy(), open_time) < 0:
        return Universe(open_time, False, False, False, float("nan"), float("nan"), [], {})

    regime = regime_series(btc)
    closes = {
        symbol: f.drop_duplicates("open_time").set_index("open_time")["close"]
        for symbol, f in features.items()
    }
    panel = momentum_panel(closes, regime)

    btc_ot = btc["open_time"].to_numpy()
    btc_row = btc.iloc[_bar_index(btc_ot, open_time)]
    market_now = align_market(btc, np.array([open_time]), pd.RangeIndex(1)).iloc[0]

    signals: dict[tuple[str, str], Signal] = {}
    for symbol, f in features.items():
        ot = f["open_time"].to_numpy()
        pos = _bar_index(ot, open_time)
        if pos < 0:
            continue
        row = f.iloc[pos]
        price = float(row["close"])
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        valid = atr > 0

        rank, rebalance, btc_ok = panel.for_symbol(symbol, ot)
        m_signal, m_score, m_exit = momentum_arrays(rank, rebalance, btc_ok)
        momentum_value = panel.momentum[symbol].reindex(ot).to_numpy()[pos]
        m_long = bool(m_signal[pos]) and valid
        signals[(MOMENTUM.name, symbol)] = Signal(
            symbol=symbol,
            family=MOMENTUM.name,
            open_time=open_time,
            side="long" if m_long else "flat",
            score=float(m_score[pos]) if m_long else 0.0,
            price=price,
            atr=atr,
            stop_price=price - MOMENTUM.atr_stop * atr if m_long else 0.0,
            exit=bool(m_exit[pos]),
            exit_reason=(
                "regime_exit" if not btc_ok[pos] else "signal_exit" if m_exit[pos] else ""
            ),
            detail={
                "rank": None if np.isnan(rank[pos]) else float(rank[pos]),
                "universe": len(panel.symbols),
                "momentum_pct": None if np.isnan(momentum_value) else float(momentum_value) * 100,
                "rebalance": bool(rebalance[pos]),
                "btc_ok": bool(btc_ok[pos]),
            },
        )

        market = align_market(btc, ot, f.index)
        t = turtle_arrays(f, market["close"], market["ema200"], market["rising"])
        t_long = bool(t.signal.iloc[pos]) and valid and pd.notna(t.score.iloc[pos])
        entry_high = t.entry_high.iloc[pos]
        exit_low = t.exit_low.iloc[pos]
        signals[(TURTLE.name, symbol)] = Signal(
            symbol=symbol,
            family=TURTLE.name,
            open_time=open_time,
            side="long" if t_long else "flat",
            score=float(t.score.iloc[pos]) if t_long else 0.0,
            price=price,
            atr=atr,
            stop_price=price - TURTLE.atr_stop * atr if t_long else 0.0,
            exit=bool(t.exit.iloc[pos]),
            exit_reason=(
                "regime_exit" if bool(t.market_off.iloc[pos])
                else "signal_exit" if bool(t.exit.iloc[pos]) else ""
            ),
            detail={
                "entry_high": None if pd.isna(entry_high) else float(entry_high),
                "exit_low": None if pd.isna(exit_low) else float(exit_low),
                "breakout_atr": (
                    None if pd.isna(entry_high) or not valid else float((price - entry_high) / atr)
                ),
                "above_ema200": bool(price > row["ema_200"]) if pd.notna(row["ema_200"]) else False,
                "adx": float(row["adx"]) if pd.notna(row["adx"]) else 0.0,
                "market_on": bool(t.market_on.iloc[pos]),
            },
        )

    return Universe(
        open_time=open_time,
        market_present=True,
        regime_ok=bool(btc_row["close"] > btc_row["ema_200"]),
        regime_rising=bool(market_now["rising"]),
        market_close=float(btc_row["close"]),
        market_ema200=float(btc_row["ema_200"]) if pd.notna(btc_row["ema_200"]) else float("nan"),
        ranked=panel.symbols,
        signals=signals,
    )


# ---- thin wrappers with the names the engine design uses ----


def momentum_universe(frames_4h: dict[str, pd.DataFrame], now_open_time: int) -> dict[str, Signal]:
    """Momentum signals for every symbol with a bar at `now_open_time`."""
    view = evaluate_universe(frames_4h, now_open_time)
    return {s.symbol: s for (family, _), s in view.signals.items() if family == MOMENTUM.name}


def turtle_signal(symbol_4h: pd.DataFrame, btc_4h: pd.DataFrame, symbol: str, open_time: int | None = None) -> Signal | None:
    """Turtle signal for one symbol on its latest bar (or on `open_time`)."""
    if open_time is None:
        open_time = int(symbol_4h["open_time"].iloc[-1])
    view = evaluate_universe({symbol: symbol_4h, MARKET_SYMBOL: btc_4h}, open_time)
    return view.signal(TURTLE.name, symbol)


def momentum_exit(view: Universe, symbol: str) -> tuple[bool, str]:
    return view.exit_check(MOMENTUM.name, symbol)


def turtle_exit(view: Universe, symbol: str) -> tuple[bool, str]:
    return view.exit_check(TURTLE.name, symbol)
