"""Alt-strategy runner. Results go to RESEARCH_DIR/results/alt.

    python -m app.research.alt.run                              six core candidates, six 2021-2023 windows
    python -m app.research.alt.run --strategies a,b             subset (variant names allowed)
    python -m app.research.alt.run --variants                   core candidates plus the variants
    python -m app.research.alt.run --windows w01_bull_2021 ...  window subset
    python -m app.research.alt.run --parity                     prove simulate_ext == simulate without hooks

For every strategy and window the run reports the censored result (the live
risk gates: 5% daily-loss pause, 20% max-drawdown kill switch) and the
uncensored one (both gates at 100%) so the raw edge is visible; the ranked
table at the end scores both.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import time

import pandas as pd

from app.research.alt.generators import ALL, STRATEGIES, VARIANTS, Spec
from app.research.alt.sim import simulate_ext
from app.research.data import RESULTS_DIR
from app.research.portfolio import SimResult, buy_and_hold, simulate, trades_frame
from app.research.windows import SYMBOLS, WINDOW_BY_ID, Window

ALT_DIR = RESULTS_DIR / "alt"
DEFAULT_WINDOWS = [
    "w01_bull_2021", "w02_crash_may_2021", "w03_bull_q4_2021",
    "w04_luna_2022", "w05_ftx_2022", "w06_chop_2023",
]
# The current ensemble (research/results/baseline_30m.csv); used only if that file is missing.
BASELINE_FALLBACK = {
    "w01_bull_2021": (482, 68.05, 15.26, 1.16), "w02_crash_may_2021": (90, -3.86, 22.53, 0.94),
    "w03_bull_q4_2021": (100, -16.31, 20.40, 0.68), "w04_luna_2022": (109, -19.48, 20.28, 0.57),
    "w05_ftx_2022": (175, -11.62, 20.16, 0.82), "w06_chop_2023": (195, -18.54, 20.04, 0.68),
}


def _log(message: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {message}", flush=True)


def _flat(metrics: dict) -> dict:
    return {k: v for k, v in metrics.items() if not isinstance(v, dict)}


def _gross(result: SimResult) -> tuple[float, float]:
    profit = sum(t.pnl for t in result.trades if t.pnl > 0)
    loss = abs(sum(t.pnl for t in result.trades if t.pnl <= 0))
    return profit, loss


def uncensored(params):
    return dataclasses.replace(params, max_dd_pct=100.0, daily_loss_pct=100.0)


def build_frames(spec: Spec, window: Window) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in SYMBOLS:
        df = spec.gen(window, symbol, spec.tf, **spec.gen_kwargs)
        if df is not None and df["in_window"].any():
            frames[symbol] = df
    return frames


def _signals(frames: dict[str, pd.DataFrame], min_score: float) -> int:
    return int(sum(
        ((df["side"] == "long") & (df["score"] >= min_score) & df["in_window"]).sum() for df in frames.values()
    ))


def run_strategy(spec: Spec, windows: list[Window], capital: float, tag: str = "") -> pd.DataFrame:
    rows: list[dict] = []
    for window in windows:
        t0 = time.time()
        frames = build_frames(spec, window)
        if not frames:
            _log(f"{spec.name} {window.id}: no data")
            continue
        res = simulate_ext(frames, spec.tf, spec.params, capital, window.start_ms, window.end_ms)
        unc = simulate_ext(frames, spec.tf, uncensored(spec.params), capital, window.start_ms, window.end_ms)
        bench = buy_and_hold(frames, window.start_ms, window.end_ms)
        m, mu = res.metrics, unc.metrics
        gp, gl = _gross(res)
        ugp, ugl = _gross(unc)
        rows.append({
            "strategy": spec.name, "tf": spec.tf,
            "window": window.id, "label": window.label, "regime": window.regime, "days": window.days,
            "symbols": len(frames), "signals": _signals(frames, spec.params.min_score),
            **_flat(m),
            "gross_profit": gp, "gross_loss": gl,
            "bh_equal_weight_pct": bench["equal_weight_pct"], "bh_btc_pct": bench["btc_pct"],
            "unc_trades": mu.get("trades", 0), "unc_win_rate": mu.get("win_rate", 0.0),
            "unc_net_pct": mu.get("net_pct", 0.0), "unc_max_dd_pct": mu.get("max_dd_pct", 0.0),
            "unc_profit_factor": mu.get("profit_factor", 0.0),
            "unc_gross_profit": ugp, "unc_gross_loss": ugl,
            "unc_avg_bars_held": mu.get("avg_bars_held", 0.0),
            "unc_exit_reasons": str(mu.get("exit_reasons", {})),
        })
        for suffix, result in ((tag, res), (f"{tag}_unc", unc)):
            tf_df = trades_frame(result)
            if not tf_df.empty:
                tf_df.to_csv(ALT_DIR / "trades" / f"{spec.name}_{spec.tf}_{window.id}{suffix}.csv", index=False)
        pd.DataFrame(unc.equity_curve, columns=["open_time", "equity"]).to_csv(
            ALT_DIR / "curves" / f"{spec.name}_{spec.tf}_{window.id}{tag}_unc.csv", index=False
        )
        _log(
            f"{spec.name:<24} {window.id:<20} trades={m['trades']:>3} win={m['win_rate'] * 100:>5.1f}% "
            f"net={m['net_pct']:>+7.2f}% dd={m['max_dd_pct']:>5.2f}% pf={min(m['profit_factor'], 99):>5.2f} "
            f"| unc net={mu.get('net_pct', 0.0):>+7.2f}% dd={mu.get('max_dd_pct', 0.0):>5.2f}% "
            f"trades={mu.get('trades', 0):>3} | hold={bench['equal_weight_pct']:>+7.2f}% "
            f"btc={bench['btc_pct']:>+6.1f}% ({time.time() - t0:.0f}s)"
        )
    summary = pd.DataFrame(rows)
    summary.to_csv(ALT_DIR / f"{spec.name}_{spec.tf}{tag}.csv", index=False)
    return summary


def rank_row(name: str, tf: str, s: pd.DataFrame) -> dict:
    """windows positive, mean/worst net, worst DD, pooled PF, trades and the robustness score."""
    if s.empty:
        return {"strategy": name, "tf": tf}
    days = float(s["days"].sum())

    def block(prefix: str, net: str, dd: str, gp: str, gl: str, trades: str) -> dict:
        pooled = s[gp].sum() / s[gl].sum() if s[gl].sum() > 0 else (99.0 if s[gp].sum() > 0 else 0.0)
        mean_net, worst_net, worst_dd = s[net].mean(), s[net].min(), s[dd].max()
        return {
            f"{prefix}pos": int((s[net] > 0).sum()),
            f"{prefix}mean_net": mean_net, f"{prefix}worst_net": worst_net, f"{prefix}worst_dd": worst_dd,
            f"{prefix}pf": pooled, f"{prefix}trades": int(s[trades].sum()),
            f"{prefix}per_day": s[trades].sum() / days if days else 0.0,
            f"{prefix}robust": mean_net + 0.5 * worst_net - 0.3 * worst_dd,
        }

    return {
        "strategy": name, "tf": tf, "windows": len(s),
        **block("", "net_pct", "max_dd_pct", "gross_profit", "gross_loss", "trades"),
        **block("unc_", "unc_net_pct", "unc_max_dd_pct", "unc_gross_profit", "unc_gross_loss", "unc_trades"),
    }


def baseline_row(window_ids: list[str]) -> dict | None:
    """The current ensemble from research/results/baseline_30m.csv and its trade lists."""
    path = RESULTS_DIR / "baseline_30m.csv"
    rows = []
    if path.exists():
        base = pd.read_csv(path)
        base = base[base["window"].isin(window_ids)]
        for r in base.itertuples(index=False):
            gp = gl = 0.0
            tpath = RESULTS_DIR / "trades" / f"baseline_30m_{r.window}.csv"
            if tpath.exists():
                pnl = pd.read_csv(tpath)["pnl"]
                gp, gl = float(pnl[pnl > 0].sum()), float(abs(pnl[pnl <= 0].sum()))
            rows.append({"days": r.days, "trades": r.trades, "net_pct": r.net_pct, "max_dd_pct": r.max_dd_pct,
                         "gross_profit": gp, "gross_loss": gl})
    else:
        for wid in window_ids:
            if wid in BASELINE_FALLBACK:
                trades, net, dd, pf = BASELINE_FALLBACK[wid]
                rows.append({"days": WINDOW_BY_ID[wid].days, "trades": trades, "net_pct": net, "max_dd_pct": dd,
                             "gross_profit": pf, "gross_loss": 1.0})
    if not rows:
        return None
    s = pd.DataFrame(rows)
    for col in ("trades", "net_pct", "max_dd_pct", "gross_profit", "gross_loss"):
        s[f"unc_{col}"] = float("nan") if col != "trades" else 0
    row = rank_row("ensemble_baseline (current)", "30m", s)
    for k in list(row):
        if k.startswith("unc_"):
            row[k] = float("nan")
    return row


def stage_parity(windows: list[Window], capital: float) -> bool:
    """Run hook-free frames through both simulators; they must produce identical trades."""
    ok = True
    for name in ("donchian_trend", "vol_breakout_4h"):
        spec = ALL[name]
        params = dataclasses.replace(spec.params, frame_stops=False) if name == "vol_breakout_4h" else spec.params
        for window in windows:
            frames = build_frames(spec, window)
            if not frames:
                continue
            a = simulate(frames, spec.tf, params, capital, window.start_ms, window.end_ms)
            b = simulate_ext(frames, spec.tf, params, capital, window.start_ms, window.end_ms)
            same = (
                len(a.trades) == len(b.trades)
                and abs(a.final_equity - b.final_equity) < 1e-9
                and all(abs(x.pnl - y.pnl) < 1e-9 and x.exit_time == y.exit_time for x, y in zip(a.trades, b.trades))
            )
            ok &= same
            _log(f"parity {name:<16} {window.id:<20} trades={len(a.trades):>3} "
                 f"equity {a.final_equity:.6f} vs {b.final_equity:.6f} -> {'OK' if same else 'MISMATCH'}")
    _log("parity " + ("PASSED" if ok else "FAILED"))
    return ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", default=None, help="comma list; core and variant names accepted")
    parser.add_argument("--variants", action="store_true", help="also run the VARIANTS registry")
    parser.add_argument("--windows", nargs="*", default=None)
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--parity", action="store_true")
    parser.add_argument("--tag", default="", help="suffix for every output file, e.g. _oos for a second window set")
    args = parser.parse_args()
    tag = args.tag

    for sub in ("trades", "curves"):
        (ALT_DIR / sub).mkdir(parents=True, exist_ok=True)
    window_ids = args.windows or DEFAULT_WINDOWS
    windows = [WINDOW_BY_ID[w] for w in window_ids]

    if args.parity:
        stage_parity(windows, args.capital)
        return

    if args.strategies:
        names = [n.strip() for n in args.strategies.split(",") if n.strip()]
        unknown = [n for n in names if n not in ALL]
        if unknown:
            parser.error(f"unknown strategies {unknown}; known: {sorted(ALL)}")
    else:
        names = list(STRATEGIES) + (list(VARIANTS) if args.variants else [])

    started = time.time()
    ranked: list[dict] = []
    for name in names:
        spec = ALL[name]
        _log(f"=== {name} ({spec.tf}) ===")
        summary = run_strategy(spec, windows, args.capital, tag)
        if not summary.empty:
            ranked.append(rank_row(name, spec.tf, summary))
            _log(
                f"{name} TOTAL: positive={int((summary['net_pct'] > 0).sum())}/{len(summary)} "
                f"mean_net={summary['net_pct'].mean():+.2f}% worst={summary['net_pct'].min():+.2f}% "
                f"worst_dd={summary['max_dd_pct'].max():.2f}% trades={int(summary['trades'].sum())} "
                f"| unc mean={summary['unc_net_pct'].mean():+.2f}% worst={summary['unc_net_pct'].min():+.2f}%"
            )

    # Rank every strategy with results on disk for exactly these windows, not only this run,
    # so a partial rerun (one variant) refreshes its row without dropping the others.
    ranked = []
    for name, spec in ALL.items():
        path = ALT_DIR / f"{name}_{spec.tf}{tag}.csv"
        if not path.exists():
            continue
        s = pd.read_csv(path)
        s = s[s["window"].isin(window_ids)]
        if len(s) != len(window_ids):
            continue
        ranked.append({**rank_row(name, spec.tf, s), "core": name in STRATEGIES, "ran_now": name in names})
    base = baseline_row(window_ids)
    if base:
        ranked.append({**base, "core": True, "ran_now": False})
    table = pd.DataFrame(ranked)
    if table.empty:
        _log("nothing ran")
        return
    table = table.sort_values("robust", ascending=False)
    table.to_csv(ALT_DIR / f"ranked{tag}.csv", index=False)

    show = table[["strategy", "tf", "pos", "mean_net", "worst_net", "worst_dd", "pf", "trades", "per_day", "robust"]]
    print("\nRANKED (censored: 5% daily-loss pause + 20% kill switch, as live)", flush=True)
    print(show.to_string(index=False, float_format=lambda v: f"{v:.2f}"), flush=True)
    unc = table[["strategy", "unc_pos", "unc_mean_net", "unc_worst_net", "unc_worst_dd", "unc_pf", "unc_trades",
                 "unc_robust"]].sort_values("unc_robust", ascending=False)
    print("\nRANKED (uncensored: no daily pause, no kill switch)", flush=True)
    print(unc.to_string(index=False, float_format=lambda v: f"{v:.2f}"), flush=True)
    _log(f"done in {(time.time() - started) / 60:.1f} min -> {ALT_DIR}")


if __name__ == "__main__":
    main()
