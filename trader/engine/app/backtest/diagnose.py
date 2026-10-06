"""Diagnostics: why is (or isn't) the ensemble trading?

Run inside the engine container:

    python -m app.backtest.diagnose
    python -m app.backtest.diagnose --sweep

The first form replays stored candles and reports which gate rejected each
candidate. The second sweeps the tuning knobs so the trade-off between trade
frequency and edge is visible rather than guessed at.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

import pandas as pd

from app.config import settings
from app.db import repo
from app.db.session import init_db, session_scope
from app.features.build import build_features
from app.strategies.ensemble import Ensemble

WARMUP = 210


def replay(symbol: str, timeframe: str, features: pd.DataFrame) -> dict:
    """Walk the history and record what the ensemble decided at each bar."""
    ensemble = Ensemble()
    reasons: Counter[str] = Counter()
    scores: list[float] = []
    moves: list[float] = []
    tradable = 0
    sides: Counter[str] = Counter()

    step = max(1, len(features) // 900)  # cap the work on long histories
    for i in range(WARMUP, len(features), step):
        window = features.iloc[: i + 1]
        decision = ensemble.evaluate(symbol, timeframe, window, None, None)
        scores.append(decision.score)
        sides[decision.side] += 1
        if decision.expected_move_pct:
            moves.append(decision.expected_move_pct)
        if decision.tradable:
            tradable += 1
        else:
            reasons[_bucket(decision.rejection)] += 1

    series = pd.Series(scores)
    move_series = pd.Series(moves) if moves else pd.Series([0.0])
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "evaluated": len(scores),
        "tradable": tradable,
        "score_p50": round(series.quantile(0.5), 3),
        "score_p90": round(series.quantile(0.9), 3),
        "score_p99": round(series.quantile(0.99), 3),
        "score_max": round(series.max(), 3),
        "move_p50": round(move_series.quantile(0.5), 3),
        "move_p90": round(move_series.quantile(0.9), 3),
        "atr_pct_median": round(float(features["atr_pct"].median()), 4),
        "reasons": reasons.most_common(6),
        "sides": dict(sides),
    }


def _bucket(rejection: str) -> str:
    if not rejection:
        return "not tradable (no reason)"
    if rejection.startswith("score "):
        return "score below threshold"
    if "fee hurdle" in rejection:
        return "target below fee hurdle"
    if "reward/risk" in rejection:
        return "reward/risk too low"
    if "ml disagrees" in rejection:
        return "ml disagreed"
    return rejection[:48]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true", help="try several parameter sets")
    parser.add_argument("--timeframes", default=None, help="comma separated, default: configured")
    args = parser.parse_args()

    await init_db()
    timeframes = (
        [t.strip() for t in args.timeframes.split(",")]
        if args.timeframes
        else sorted({settings.timeframe, settings.htf_timeframe})
    )

    loaded: dict[tuple[str, str], pd.DataFrame] = {}
    htf_raw: dict[str, pd.DataFrame] = {}
    async with session_scope() as session:
        for timeframe in timeframes:
            for symbol in settings.symbols:
                candles = await repo.load_candles(session, symbol, timeframe, 20000)
                if len(candles) >= 300:
                    loaded[(symbol, timeframe)] = build_features(candles)
        # Raw higher-timeframe candles, so the sweep applies the same HTF gate
        # the live engine does instead of handing every signal the bonus.
        for symbol in settings.symbols:
            candles = await repo.load_candles(session, symbol, settings.htf_timeframe, 20000)
            if len(candles) >= 60:
                htf_raw[symbol] = candles

    if not loaded:
        print("no candle history stored yet - let the market data worker run first")
        return

    print(f"\n{'=' * 96}")
    print("GATE ANALYSIS - what stops a signal from becoming a trade")
    print(f"{'=' * 96}")
    print(
        f"{'symbol':<10} {'tf':<5} {'bars':>6} {'trades':>7} {'p50':>6} {'p90':>6} "
        f"{'p99':>6} {'move p90':>9} {'atr%':>7}  top rejections"
    )
    print("-" * 96)

    for (symbol, timeframe), features in loaded.items():
        result = replay(symbol, timeframe, features)
        top = ", ".join(f"{name} x{count}" for name, count in result["reasons"][:2])
        print(
            f"{result['symbol']:<10} {result['timeframe']:<5} {result['evaluated']:>6} "
            f"{result['tradable']:>7} {result['score_p50']:>6} {result['score_p90']:>6} "
            f"{result['score_p99']:>6} {result['move_p90']:>9} "
            f"{result['atr_pct_median']:>7}  {top}"
        )

    print(
        f"\nCurrent gates: min_signal_score={settings.min_signal_score} "
        f"min_expected_move={settings.min_expected_move_pct}% "
        f"atr_stop={settings.atr_stop_mult} atr_target={settings.atr_target_mult} "
        f"fee_round_trip={settings.round_trip_fee_pct}%"
    )

    if not args.sweep:
        return

    from app.backtest.engine import backtest_symbol

    grid = [
        (0.62, 0.8, 1.8, 3.0),
        (0.55, 0.6, 1.8, 3.0),
        (0.50, 0.5, 1.5, 2.5),
        (0.45, 0.4, 1.5, 2.5),
        (0.45, 0.4, 1.2, 2.4),
        (0.40, 0.35, 1.2, 2.2),
    ]

    print(f"\n{'=' * 96}")
    print(
        "PARAMETER SWEEP - backtest on stored candles, fees, slippage and the "
        f"{settings.htf_timeframe} gate included"
    )
    print(f"{'=' * 96}")
    print(
        f"{'score':>6} {'move%':>6} {'stop':>5} {'tgt':>5} | {'symbol':<10} {'tf':<5} "
        f"{'trades':>7} {'win%':>6} {'net%':>8} {'PF':>6} {'maxDD%':>7} {'avgR':>6}"
    )
    print("-" * 96)

    original = (
        settings.min_signal_score,
        settings.min_expected_move_pct,
        settings.atr_stop_mult,
        settings.atr_target_mult,
    )

    best: list[tuple] = []
    for score, move, stop, target in grid:
        settings.min_signal_score = score
        settings.min_expected_move_pct = move
        settings.atr_stop_mult = stop
        settings.atr_target_mult = target

        totals = {"trades": 0, "net": 0.0, "wins": 0}
        for (symbol, timeframe), features in loaded.items():
            raw = features[
                ["open_time", "open", "high", "low", "close", "volume",
                 "quote_volume", "trades", "taker_buy_base", "timestamp"]
            ]
            result = backtest_symbol(
                symbol, timeframe, raw, 100.0,
                htf_candles=htf_raw.get(symbol) if timeframe != settings.htf_timeframe else None,
                htf_timeframe=settings.htf_timeframe,
            )
            totals["trades"] += result["trades"]
            totals["net"] += result["net_pnl"]
            totals["wins"] += result["wins"]
            print(
                f"{score:>6} {move:>6} {stop:>5} {target:>5} | {symbol:<10} {timeframe:<5} "
                f"{result['trades']:>7} {result['win_rate'] * 100:>5.1f} "
                f"{result['net_pnl_pct']:>8.2f} {result['profit_factor']:>6.2f} "
                f"{result['max_drawdown_pct']:>7.2f} {result['avg_r']:>6.2f}"
            )
        win_rate = totals["wins"] / totals["trades"] if totals["trades"] else 0
        best.append((totals["net"], totals["trades"], win_rate, (score, move, stop, target)))
        print(
            f"{'':>6} {'':>6} {'':>5} {'':>5} | {'TOTAL':<10} {'':<5} "
            f"{totals['trades']:>7} {win_rate * 100:>5.1f} {totals['net']:>8.2f}"
        )
        print("-" * 96)

    settings.min_signal_score, settings.min_expected_move_pct, \
        settings.atr_stop_mult, settings.atr_target_mult = original

    best.sort(reverse=True)
    print("\nRanked by total net PnL across the stored universe:")
    for net, trades, win_rate, params in best:
        print(
            f"  net={net:>8.2f} USDT  trades={trades:>4}  win={win_rate * 100:>5.1f}%  "
            f"score={params[0]} move={params[1]}% stop={params[2]}xATR target={params[3]}xATR"
        )


if __name__ == "__main__":
    asyncio.run(main())
