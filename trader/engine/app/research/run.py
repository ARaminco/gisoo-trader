"""Research CLI. Every stage writes CSV/JSON under RESEARCH_DIR/results.

    python -m app.research.run fetch                      download all windows
    python -m app.research.run decide   --tf 30m          precompute decisions
    python -m app.research.run baseline --tf 30m          default params, every window
    python -m app.research.run grid     --tf 30m          parameter sweep
    python -m app.research.run walkforward --tf 30m       out-of-sample parameter choice
    python -m app.research.run events   --tf 30m          behaviour around news events
    python -m app.research.run ml       --tf 30m          out-of-sample value of the ML gate
    python -m app.research.run all      --tf 30m          decide + baseline + grid + walkforward + events
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import itertools
import json
import time
from pathlib import Path

import pandas as pd

from app.research.data import RESULTS_DIR, fetch_all, load_cached
from app.research.decisions import load_or_compute
from app.research.portfolio import (
    SimParams, SimResult, buy_and_hold, simulate, trades_frame,
)
from app.research.windows import SYMBOLS, TIMEFRAMES, WINDOWS, Window

HTF = "4h"


def _ensure_dirs() -> None:
    (RESULTS_DIR / "trades").mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "curves").mkdir(parents=True, exist_ok=True)


def _log(message: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {message}", flush=True)


# ------------------------------------------------------------------ loading


def load_frames(window: Window, tf: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = load_or_compute(window, symbol, tf, HTF)
        if df is not None and df["in_window"].any():
            frames[symbol] = df
    return frames


def _flat_metrics(metrics: dict) -> dict:
    return {k: v for k, v in metrics.items() if not isinstance(v, dict)}


def _run_window(
    window: Window, tf: str, frames: dict[str, pd.DataFrame], params: SimParams, capital: float
) -> SimResult:
    return simulate(frames, tf, params, capital, window.start_ms, window.end_ms)


# ------------------------------------------------------------------ stages


def stage_fetch(args: argparse.Namespace) -> None:
    windows = [w for w in WINDOWS if not args.windows or w.id in args.windows]
    tfs = args.tfs.split(",") if args.tfs else TIMEFRAMES
    counts = asyncio.run(fetch_all(windows, SYMBOLS, tfs))
    rows = [{"window": k[0], "symbol": k[1], "timeframe": k[2], "candles": v} for k, v in counts.items()]
    pd.DataFrame(rows).to_csv(RESULTS_DIR / "fetch_summary.csv", index=False)
    missing = [r for r in rows if r["candles"] <= 0]
    _log(f"fetch done: {len(rows)} series, {len(missing)} empty/failed")


def stage_decide(args: argparse.Namespace) -> None:
    tf = args.tf
    windows = [w for w in WINDOWS if not args.windows or w.id in args.windows]
    started = time.time()
    for window in windows:
        t0 = time.time()
        done = 0
        for symbol in SYMBOLS:
            df = load_or_compute(window, symbol, tf, HTF, force=args.force)
            if df is not None:
                done += 1
        _log(f"decide {window.id} {tf}: {done}/{len(SYMBOLS)} symbols in {time.time() - t0:.0f}s")
    _log(f"decide finished in {(time.time() - started) / 60:.1f} min")


def stage_baseline(args: argparse.Namespace) -> None:
    tf = args.tf
    _ensure_dirs()
    params = SimParams()
    rows: list[dict] = []
    per_symbol_rows: list[dict] = []
    regime_rows: list[dict] = []
    windows = [w for w in WINDOWS if not args.windows or w.id in args.windows]
    for window in windows:
        frames = load_frames(window, tf, SYMBOLS)
        if not frames:
            _log(f"baseline {window.id}: no data")
            continue
        result = _run_window(window, tf, frames, params, args.capital)
        bench = buy_and_hold(frames, window.start_ms, window.end_ms)
        m = result.metrics
        row = {
            "window": window.id,
            "label": window.label,
            "regime": window.regime,
            "days": window.days,
            "symbols": len(frames),
            **_flat_metrics(m),
            "bh_equal_weight_pct": bench["equal_weight_pct"],
            "bh_btc_pct": bench["btc_pct"],
        }
        rows.append(row)
        for symbol, stats in m.get("by_symbol", {}).items():
            per_symbol_rows.append({"window": window.id, "symbol": symbol, **stats,
                                    "bh_pct": bench.get("per_symbol", {}).get(symbol)})
        for regime, stats in m.get("by_regime", {}).items():
            regime_rows.append({"window": window.id, "entry_regime": regime, **stats})

        tf_df = trades_frame(result)
        if not tf_df.empty:
            tf_df.to_csv(RESULTS_DIR / "trades" / f"baseline_{tf}_{window.id}.csv", index=False)
        pd.DataFrame(result.equity_curve, columns=["open_time", "equity"]).to_csv(
            RESULTS_DIR / "curves" / f"baseline_{tf}_{window.id}.csv", index=False
        )
        _log(
            f"baseline {window.id:<26} trades={m['trades']:>3} win={m['win_rate']*100:>5.1f}% "
            f"net={m['net_pct']:>+7.2f}% dd={m['max_dd_pct']:>5.2f}% pf={m['profit_factor']:>5.2f} "
            f"| hold={bench['equal_weight_pct']:>+7.2f}%"
        )

    summary = pd.DataFrame(rows)
    summary.to_csv(RESULTS_DIR / f"baseline_{tf}.csv", index=False)
    pd.DataFrame(per_symbol_rows).to_csv(RESULTS_DIR / f"baseline_{tf}_by_symbol.csv", index=False)
    pd.DataFrame(regime_rows).to_csv(RESULTS_DIR / f"baseline_{tf}_by_regime.csv", index=False)
    if not summary.empty:
        _log(
            f"baseline TOTAL: windows={len(summary)} positive={int((summary['net_pct'] > 0).sum())} "
            f"mean_net={summary['net_pct'].mean():+.2f}% worst={summary['net_pct'].min():+.2f}% "
            f"mean_dd={summary['max_dd_pct'].mean():.2f}% trades={int(summary['trades'].sum())}"
        )


GRID = {
    "min_score": [0.55, 0.62, 0.68, 0.74],
    "min_move_pct": [0.6, 0.8, 1.0],
    "atr_stop": [1.5, 1.8, 2.2],
    "atr_target": [2.5, 3.0, 3.5],
}

EXTRA_GATES: list[dict] = [
    {},
    {"vol_breaker": 2.0},
    {"vol_breaker": 1.6},
    {"blocked_regimes": ("volatile",)},
    {"blocked_regimes": ("volatile", "trend_down")},
    {"min_adx": 20.0},
    {"max_positions": 2},
    {"max_positions": 4},
    {"risk_pct": 1.0},
    {"risk_pct": 2.0},
    {"trailing": False},
    # structural questions
    {"max_dd_pct": 100.0, "daily_loss_pct": 100.0},          # true edge without the circuit breakers
    {"min_htf_bias": 0.0},                                     # never buy against the 4h trend
    {"min_htf_bias": 1.0},                                     # only buy with a bullish 4h trend
    {"allow_short": True},                                     # futures: shorts allowed
    {"allow_short": True, "min_htf_bias": 0.0},                # shorts, each side with the 4h trend
    {"allow_short": True, "min_htf_bias": 1.0},
    {"min_htf_bias": 1.0, "vol_breaker": 2.0},
    {"min_htf_bias": 1.0, "min_adx": 20.0},
    {"min_htf_bias": 1.0, "max_positions": 2, "risk_pct": 1.0},
    {"allow_short": True, "min_htf_bias": 1.0, "vol_breaker": 2.0},
]


def stage_grid(args: argparse.Namespace) -> None:
    tf = args.tf
    _ensure_dirs()
    selected = [w for w in WINDOWS if not args.windows or w.id in args.windows]
    frames_by_window = {w.id: load_frames(w, tf, SYMBOLS) for w in selected}
    frames_by_window = {k: v for k, v in frames_by_window.items() if v}

    combos = list(itertools.product(*GRID.values()))
    rows: list[dict] = []
    started = time.time()
    total = len(combos) + len(EXTRA_GATES) - 1
    done = 0

    def run_params(params: SimParams, tag: str) -> None:
        for window in WINDOWS:
            frames = frames_by_window.get(window.id)
            if not frames:
                continue
            result = _run_window(window, tf, frames, params, args.capital)
            rows.append({"tag": tag, "params": params.key(), "window": window.id,
                         "regime": window.regime, **{k: v for k, v in dataclasses.asdict(params).items()
                                                     if not isinstance(v, tuple)},
                         **_flat_metrics(result.metrics)})

    for combo in combos:
        params = SimParams(**dict(zip(GRID.keys(), combo)))
        run_params(params, "grid")
        done += 1
        if done % 12 == 0:
            _log(f"grid {done}/{total} ({(time.time() - started) / 60:.1f} min)")

    for extra in EXTRA_GATES[1:]:
        params = SimParams(**extra)
        run_params(params, "gate:" + ",".join(f"{k}={v}" for k, v in extra.items()))
        done += 1

    df = pd.DataFrame(rows)
    df.to_csv(RESULTS_DIR / f"grid_{tf}.csv", index=False)

    agg = (
        df.groupby(["tag", "params"])
        .agg(
            windows=("window", "count"),
            positive=("net_pct", lambda s: int((s > 0).sum())),
            mean_net=("net_pct", "mean"),
            median_net=("net_pct", "median"),
            worst_net=("net_pct", "min"),
            mean_dd=("max_dd_pct", "mean"),
            worst_dd=("max_dd_pct", "max"),
            mean_pf=("profit_factor", "mean"),
            trades=("trades", "sum"),
            mean_sharpe=("sharpe", "mean"),
            kills=("kill_triggered", "sum"),
        )
        .reset_index()
    )
    # robustness score: reward consistency, punish the worst window and drawdown
    agg["robust_score"] = agg["mean_net"] + agg["worst_net"] * 0.5 - agg["worst_dd"] * 0.3
    agg = agg.sort_values("robust_score", ascending=False)
    agg.to_csv(RESULTS_DIR / f"grid_{tf}_ranked.csv", index=False)
    _log(f"grid finished: {len(df)} runs in {(time.time() - started) / 60:.1f} min")
    print(agg.head(15).to_string(index=False), flush=True)


def stage_walkforward(args: argparse.Namespace) -> None:
    """Choose parameters on window k using the grid, evaluate on window k+1."""
    tf = args.tf
    grid_path = RESULTS_DIR / f"grid_{tf}.csv"
    if not grid_path.exists():
        _log("run the grid stage first")
        return
    grid = pd.read_csv(grid_path)
    grid = grid[grid["tag"] == "grid"]
    grid["objective"] = grid["net_pct"] - grid["max_dd_pct"] * 0.5

    ordered = [w for w in WINDOWS]
    rows: list[dict] = []
    for prev, current in zip(ordered[:-1], ordered[1:]):
        sub = grid[grid["window"] == prev.id]
        if sub.empty:
            continue
        best = sub.sort_values("objective", ascending=False).iloc[0]
        chosen = {k: best[k] for k in GRID.keys()}
        # also try the cumulative best across all previous windows
        prior = grid[grid["window"].isin([w.id for w in ordered if w.start < current.start])]
        cumulative = prior.groupby("params")["objective"].mean().sort_values(ascending=False)
        cum_key = cumulative.index[0]
        cum_row = prior[prior["params"] == cum_key].iloc[0]
        chosen_cum = {k: cum_row[k] for k in GRID.keys()}

        frames = load_frames(current, tf, SYMBOLS)
        if not frames:
            continue
        for mode, choice in (("previous_window", chosen), ("cumulative", chosen_cum)):
            params = SimParams(**{k: (int(v) if k == "max_positions" else float(v)) for k, v in choice.items()})
            result = _run_window(current, tf, frames, params, args.capital)
            rows.append({"mode": mode, "trained_on": prev.id if mode == "previous_window" else "all_prior",
                         "tested_on": current.id, "regime": current.regime, **choice,
                         **_flat_metrics(result.metrics)})
        default = _run_window(current, tf, frames, SimParams(), args.capital)
        rows.append({"mode": "default", "trained_on": "-", "tested_on": current.id,
                     "regime": current.regime, **_flat_metrics(default.metrics)})

    df = pd.DataFrame(rows)
    df.to_csv(RESULTS_DIR / f"walkforward_{tf}.csv", index=False)
    summary = df.groupby("mode").agg(
        windows=("tested_on", "count"), positive=("net_pct", lambda s: int((s > 0).sum())),
        mean_net=("net_pct", "mean"), worst_net=("net_pct", "min"), mean_dd=("max_dd_pct", "mean"),
    )
    print(summary.to_string(), flush=True)


def stage_events(args: argparse.Namespace) -> None:
    """What happened in the days around each curated event."""
    tf = args.tf
    rows: list[dict] = []
    for window in WINDOWS:
        if not window.events:
            continue
        trades_path = RESULTS_DIR / "trades" / f"baseline_{tf}_{window.id}.csv"
        curve_path = RESULTS_DIR / "curves" / f"baseline_{tf}_{window.id}.csv"
        if not curve_path.exists():
            continue
        curve = pd.read_csv(curve_path)
        trades = pd.read_csv(trades_path) if trades_path.exists() else pd.DataFrame()
        frames = load_frames(window, tf, SYMBOLS)

        for event in window.events:
            day0 = int(dt.datetime.fromisoformat(event.date).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            span_start, span_end = day0 - 86_400_000, day0 + 3 * 86_400_000
            before = curve[curve["open_time"] < span_start]["equity"]
            inside = curve[(curve["open_time"] >= span_start) & (curve["open_time"] < span_end)]["equity"]
            if before.empty or inside.empty:
                continue
            eq_before = float(before.iloc[-1])
            eq_after = float(inside.iloc[-1])
            eq_low = float(inside.min())

            opened = trades[(trades["entry_time"] >= span_start) & (trades["entry_time"] < span_end)] if not trades.empty else trades
            closed = trades[(trades["exit_time"] >= span_start) & (trades["exit_time"] < span_end)] if not trades.empty else trades
            exposed = trades[(trades["entry_time"] < day0) & (trades["exit_time"] >= day0)] if not trades.empty else trades

            bench = buy_and_hold(frames, span_start, span_end)
            rows.append({
                "window": window.id, "date": event.date, "kind": event.kind, "title": event.title,
                "equity_change_pct": (eq_after - eq_before) / eq_before * 100,
                "equity_min_pct": (eq_low - eq_before) / eq_before * 100,
                "positions_open_at_event": len(exposed),
                "trades_opened": len(opened),
                "trades_closed": len(closed),
                "closed_pnl": float(closed["pnl"].sum()) if not closed.empty else 0.0,
                "worst_trade": float(closed["pnl"].min()) if not closed.empty else 0.0,
                "gap_fills": int(closed["gap_fill"].sum()) if not closed.empty else 0,
                "stops_hit": int((closed["reason"] == "stop_loss").sum()) if not closed.empty else 0,
                "hold_equal_weight_pct": bench["equal_weight_pct"],
                "hold_btc_pct": bench["btc_pct"],
            })
    df = pd.DataFrame(rows)
    df.to_csv(RESULTS_DIR / f"events_{tf}.csv", index=False)
    if not df.empty:
        by_kind = df.groupby("kind").agg(
            events=("date", "count"), system_mean=("equity_change_pct", "mean"),
            system_worst=("equity_min_pct", "min"), hold_mean=("hold_equal_weight_pct", "mean"),
            exposed=("positions_open_at_event", "mean"),
        )
        print(by_kind.to_string(), flush=True)


def stage_ml(args: argparse.Namespace) -> None:
    """Train on window k-1, gate window k's entries with the model. Strictly out-of-sample."""
    import joblib

    from app.features.build import build_features, feature_matrix
    from app.ml import model as ml

    tf = args.tf
    rows: list[dict] = []
    ordered = [w for w in WINDOWS if not args.windows or w.id in args.windows]
    for prev, current in zip(ordered[:-1], ordered[1:]):
        frames = load_frames(current, tf, SYMBOLS)
        if not frames:
            continue
        gated: dict[str, pd.DataFrame] = {}
        trained = 0
        for symbol, decisions in frames.items():
            train_candles = load_cached(prev, symbol, tf)
            test_candles = load_cached(current, symbol, tf)
            if train_candles is None or test_candles is None or len(train_candles) < 800:
                gated[symbol] = decisions
                continue
            result = ml.train(symbol, tf, train_candles)
            if result is None:
                gated[symbol] = decisions
                continue
            trained += 1
            bundle = joblib.load(result.path)
            model, columns = bundle["model"], bundle["features"]
            features = build_features(test_candles)
            X = feature_matrix(features).reindex(columns=columns)
            proba = pd.Series(model.predict_proba(X.fillna(X.median()))[:, 1], index=features["open_time"].to_numpy())
            df = decisions.copy()
            p = proba.reindex(df["open_time"].to_numpy()).to_numpy()
            agreement = pd.Series(p, index=df.index).where(df["side"] == "long", 1 - pd.Series(p, index=df.index))
            veto = agreement < args.ml_min
            df.loc[veto.fillna(False), "score"] = 0.0
            gated[symbol] = df
            Path(result.path).unlink(missing_ok=True)

        base = _run_window(current, tf, frames, SimParams(), args.capital)
        with_ml = _run_window(current, tf, gated, SimParams(), args.capital)
        rows.append({"tested_on": current.id, "trained_on": prev.id, "regime": current.regime,
                     "models_trained": trained,
                     "base_trades": base.metrics["trades"], "base_net": base.metrics["net_pct"],
                     "base_dd": base.metrics["max_dd_pct"], "base_pf": base.metrics["profit_factor"],
                     "ml_trades": with_ml.metrics["trades"], "ml_net": with_ml.metrics["net_pct"],
                     "ml_dd": with_ml.metrics["max_dd_pct"], "ml_pf": with_ml.metrics["profit_factor"]})
        _log(f"ml {current.id}: base {base.metrics['net_pct']:+.2f}% ({base.metrics['trades']}) "
             f"-> ml {with_ml.metrics['net_pct']:+.2f}% ({with_ml.metrics['trades']}), models={trained}")
    df = pd.DataFrame(rows)
    df.to_csv(RESULTS_DIR / f"ml_{tf}.csv", index=False)
    if not df.empty:
        _log(f"ml TOTAL: base mean {df['base_net'].mean():+.2f}% vs ml mean {df['ml_net'].mean():+.2f}% "
             f"| base worst {df['base_net'].min():+.2f}% vs ml worst {df['ml_net'].min():+.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["fetch", "decide", "baseline", "grid", "walkforward", "events", "ml", "all"])
    parser.add_argument("--tf", default="30m")
    parser.add_argument("--tfs", default=None, help="fetch only: comma list of timeframes")
    parser.add_argument("--windows", nargs="*", default=None)
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ml-min", type=float, default=0.55)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stages = {
        "fetch": stage_fetch, "decide": stage_decide, "baseline": stage_baseline,
        "grid": stage_grid, "walkforward": stage_walkforward, "events": stage_events, "ml": stage_ml,
    }
    if args.stage == "all":
        for name in ("decide", "baseline", "grid", "walkforward", "events"):
            _log(f"=== {name} ({args.tf}) ===")
            stages[name](args)
    else:
        stages[args.stage](args)


if __name__ == "__main__":
    main()
