"""Backtest of the 4h portfolio core over the stored candles.

Runs the research simulator (`app.research.alt.sim.simulate_ext`, the one
that validated the families) over the whole universe at once, with the
frames built by the same `app.strategies.core4h` functions the live engine
uses. Each family is simulated with exactly its validated AltParams on the
full starting capital and the two PnL streams are added: that is the same
sizing basis as the live engine, where both families draw on one pool and
each sizes from total equity. What it does not share with the live engine is
the position cap (each family has its own three slots here, the engine has
MAX_CONCURRENT_POSITIONS across both) and the session rule (the simulator
takes weekend entries; the risk manager does not).

The output is the per-symbol backtester's shape so the panel can show it
unchanged; `symbol` is "PORTFOLIO".
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.config import CORE4H_TIMEFRAME, MARKET_GATE_SYMBOL
from app.features.build import build_features
from app.research.alt.features import build_frame
from app.research.alt.generators import ALL
from app.research.alt.sim import simulate_ext
from app.research.portfolio import SimResult
from app.strategies import core4h

log = logging.getLogger(__name__)

PORTFOLIO = "PORTFOLIO"


def decision_frames(features: dict[str, pd.DataFrame], start_ms: int) -> tuple[dict, dict]:
    """(momentum frames, turtle frames) in the simulator's schema, per symbol."""
    btc = features[MARKET_GATE_SYMBOL]
    regime = core4h.regime_series(btc)
    closes = {
        symbol: f.drop_duplicates("open_time").set_index("open_time")["close"]
        for symbol, f in features.items()
    }
    panel = core4h.momentum_panel(closes, regime)

    momentum: dict[str, pd.DataFrame] = {}
    turtle: dict[str, pd.DataFrame] = {}
    for symbol, f in features.items():
        ot = f["open_time"].to_numpy()
        rank, rebalance, btc_ok = panel.for_symbol(symbol, ot)
        signal, score, exit_flag = core4h.momentum_arrays(rank, rebalance, btc_ok)
        side = np.where(signal, "long", "flat")
        momentum[symbol] = build_frame(f, side, score, None, start_ms, exit_signal=exit_flag)

        market = core4h.align_market(btc, ot, f.index)
        t = core4h.turtle_arrays(f, market["close"], market["ema200"], market["rising"])
        side = np.where(t.signal, "long", "flat")
        turtle[symbol] = build_frame(f, side, t.score, None, start_ms, exit_signal=t.exit)
    return momentum, turtle


def backtest_portfolio(
    candles: dict[str, pd.DataFrame],
    starting_capital: float = 100.0,
    warmup_bars: int = core4h.MIN_BARS,
) -> dict:
    """Simulate both families over `candles` (symbol -> 4h OHLCV, oldest first)."""
    features = {
        symbol: build_features(df)
        for symbol, df in candles.items()
        if len(df) >= core4h.MIN_BARS
    }
    if MARKET_GATE_SYMBOL not in features:
        raise ValueError(f"{MARKET_GATE_SYMBOL} needs at least {core4h.MIN_BARS} 4h candles")

    btc_times = features[MARKET_GATE_SYMBOL]["open_time"].to_numpy()
    start_ms = int(btc_times[min(warmup_bars, len(btc_times) - 1)])
    momentum_frames, turtle_frames = decision_frames(features, start_ms)

    results: dict[str, SimResult] = {}
    for family, frames in ((core4h.MOMENTUM, momentum_frames), (core4h.TURTLE, turtle_frames)):
        params = ALL[family.research_name].params
        results[family.name] = simulate_ext(frames, CORE4H_TIMEFRAME, params, starting_capital, start_ms)

    return _merge(results, starting_capital, len(btc_times))


def _merge(results: dict[str, SimResult], capital: float, candles: int) -> dict:
    trades = []
    for family, result in results.items():
        for t in result.trades:
            trades.append((family, t))
    trades.sort(key=lambda item: item[1].exit_time)

    # Both curves run over the same bar clock; the portfolio is their PnL summed.
    combined: dict[int, float] = {}
    for result in results.values():
        for t, equity in result.equity_curve:
            combined[t] = combined.get(t, 0.0) + (equity - capital)
    curve = [capital + pnl for _, pnl in sorted(combined.items())]
    peak = capital
    max_drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)

    pnls = [t.pnl for _, t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)

    return {
        "symbol": PORTFOLIO,
        "timeframe": CORE4H_TIMEFRAME,
        "candles": int(candles),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "net_pnl": round(net, 4),
        "net_pnl_pct": round(net / capital * 100, 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0,
        "max_drawdown_pct": round(max_drawdown, 3),
        "avg_r": round(sum(t.r_multiple for _, t in trades) / len(trades), 3) if trades else 0.0,
        "expectancy": round(net / len(trades), 4) if trades else 0.0,
        "fees_paid": round(sum(t.fees for _, t in trades), 4),
        "equity_curve": [round(v, 4) for v in curve[:: max(1, len(curve) // 500)]],
        "trade_log": [
            {
                "symbol": t.symbol,
                "family": family,
                "side": t.side,
                "entry_price": round(t.entry_price, 6),
                "exit_price": round(t.exit_price, 6),
                "qty": round(t.qty, 8),
                "pnl": round(t.pnl, 4),
                "pnl_pct": round(t.pnl_pct, 3),
                "r": round(t.r_multiple, 3),
                "reason": t.reason,
                "bars": t.bars_held,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
            }
            for family, t in trades[-200:]
        ],
    }
