"""Profit-protection study for the two live 4h families (docs/research-profit-protection.md).

    python -m app.research.alt.protection                      both families, every variant, 12 windows
    python -m app.research.alt.protection --family mom          one family (mom | turtle)
    python -m app.research.alt.protection --variants base,trail3_1r
    python -m app.research.alt.protection --combo trail3_1r+crash6_exit   merge two variants' overrides
    python -m app.research.alt.protection --summary             rebuild the summary from the files on disk

The base is exactly `xs_momentum_regime_exit` / `turtle_regime_risk1` as validated
(generators.family_params over app.strategies.core4h), run through alt.sim.simulate_ext
with the live circuit breakers on (censored) and off (uncensored). Every variant is
the same AltParams plus one or two of the protection hooks. The decision frames do
not depend on the exit rules, so they are built once per family and window.

Results: RESEARCH_DIR/results/alt/protection/<family>_<variant>.csv (per window),
trades/<family>_<variant>.csv (censored trades of all windows), summary.csv (one
row per family x variant x block: is = w01-w06, oos = w07-w12, all), and
mfe_<family>.csv (peak open gain vs realised gain of every base trade).
"""
from __future__ import annotations

import argparse
import dataclasses
import time

import numpy as np
import pandas as pd

from app.research.alt.generators import ALL, Spec
from app.research.alt.run import DEFAULT_WINDOWS, _flat, _gross, _log, build_frames, uncensored
from app.research.alt.sim import simulate_ext
from app.research.data import RESULTS_DIR
from app.research.portfolio import trades_frame
from app.research.windows import WINDOW_BY_ID, Window

OUT = RESULTS_DIR / "alt" / "protection"
FAMILIES: dict[str, Spec] = {
    "mom": ALL["xs_momentum_regime_exit"],
    "turtle": ALL["turtle_regime_risk1"],
}
IS_WINDOWS = list(DEFAULT_WINDOWS)
OOS_WINDOWS = [
    "w07_etf_bull_2024", "w08_aug_2024_crash", "w09_election_2024",
    "w10_tariff_2025", "w11_oct_2025_liquidation", "w12_recent_2026",
]
ALL_WINDOWS = IS_WINDOWS + OOS_WINDOWS
BLOCKS = {"is": IS_WINDOWS, "oos": OOS_WINDOWS, "all": ALL_WINDOWS}


def variants() -> dict[str, dict]:
    """name -> AltParams overrides. `base` is the validated family unchanged."""
    v: dict[str, dict] = {"base": {}}
    for k in (2, 3, 4):
        v[f"trail{k}_1r"] = dict(trail_close_atr=float(k), trail_close_activate_r=1.0)
        v[f"trail{k}_0r"] = dict(trail_close_atr=float(k), trail_close_activate_r=0.0)
    for g in (6, 10, 15):
        for f in (0.5, 0.7):
            v[f"giveback{g}_{int(f * 100)}"] = dict(giveback_peak_pct=float(g), giveback_keep=f)
    for x in (4, 6):
        v[f"crash{x}_exit"] = dict(crash_btc_pct=float(x), crash_symbol_atr=2.5, crash_mode="exit")
        v[f"crash{x}_block"] = dict(crash_btc_pct=float(x), crash_symbol_atr=2.5, crash_mode="block")
    for t in (8, 12, 20):
        v[f"target{t}"] = dict(target_pct=float(t))
    return v


VARIANTS = variants()


def resolve(name: str) -> dict:
    """`a+b` merges the overrides of variants a and b (the combined test)."""
    merged: dict = {}
    for part in name.split("+"):
        if part not in VARIANTS:
            raise SystemExit(f"unknown variant {part!r}; known: {sorted(VARIANTS)}")
        merged.update(VARIANTS[part])
    return merged


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def run_family(family: str, names: list[str], windows: list[Window], capital: float) -> None:
    spec = FAMILIES[family]
    frames_by_window: dict[str, dict] = {}
    for w in windows:
        t0 = time.time()
        frames_by_window[w.id] = build_frames(spec, w)
        _log(f"{family} frames {w.id}: {len(frames_by_window[w.id])} symbols ({time.time() - t0:.0f}s)")

    for name in names:
        params = dataclasses.replace(spec.params, **resolve(name))
        rows: list[dict] = []
        pooled: list[pd.DataFrame] = []
        for w in windows:
            frames = frames_by_window[w.id]
            if not frames:
                continue
            res = simulate_ext(frames, spec.tf, params, capital, w.start_ms, w.end_ms)
            unc = simulate_ext(frames, spec.tf, uncensored(params), capital, w.start_ms, w.end_ms)
            m, mu = res.metrics, unc.metrics
            gp, gl = _gross(res)
            ugp, ugl = _gross(unc)
            rows.append({
                "family": family, "variant": name, "window": w.id, "regime": w.regime, "days": w.days,
                **_flat(m),
                "killed": res.kill_triggered_at is not None, "daily_pauses": res.daily_pauses,
                "gross_profit": gp, "gross_loss": gl,
                "exit_reasons": str(m.get("exit_reasons", {})),
                "unc_trades": mu.get("trades", 0), "unc_net_pct": mu.get("net_pct", 0.0),
                "unc_max_dd_pct": mu.get("max_dd_pct", 0.0), "unc_profit_factor": mu.get("profit_factor", 0.0),
                "unc_gross_profit": ugp, "unc_gross_loss": ugl,
            })
            tf = trades_frame(res)
            if not tf.empty:
                tf.insert(0, "window", w.id)
                pooled.append(tf)
            _log(
                f"{family:<6} {name:<16} {w.id:<24} trades={m['trades']:>3} net={m['net_pct']:>+7.2f}% "
                f"dd={m['max_dd_pct']:>5.2f}% pf={min(m['profit_factor'], 99):>5.2f} "
                f"| unc net={mu.get('net_pct', 0.0):>+7.2f}% dd={mu.get('max_dd_pct', 0.0):>5.2f}"
            )
        pd.DataFrame(rows).to_csv(OUT / f"{family}_{name}.csv", index=False)
        trades = pd.concat(pooled, ignore_index=True) if pooled else pd.DataFrame()
        trades.to_csv(OUT / "trades" / f"{family}_{name}.csv", index=False)
        if name == "base":
            mfe_table(family, trades, frames_by_window).to_csv(OUT / f"mfe_{family}.csv", index=False)


def mfe_table(family: str, trades: pd.DataFrame, frames_by_window: dict[str, dict]) -> pd.DataFrame:
    """Peak open gain (highest close after entry) and realised gain of every base trade."""
    rows = []
    for r in trades.itertuples(index=False):
        df = frames_by_window[r.window].get(r.symbol)
        if df is None:
            continue
        sub = df[(df["open_time"] > r.entry_time) & (df["open_time"] <= r.exit_time)]
        peak = float(sub["close"].max()) if not sub.empty else r.entry_price
        rows.append({
            "family": family, "window": r.window, "symbol": r.symbol, "entry_at": r.entry_at, "exit_at": r.exit_at,
            "bars_held": r.bars_held, "reason": r.reason, "pnl": r.pnl,
            "mfe_pct": (peak - r.entry_price) / r.entry_price * 100,
            "realised_pct": (r.exit_price - r.entry_price) / r.entry_price * 100,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def block_stats(s: pd.DataFrame, trades: pd.DataFrame) -> dict:
    gp, gl = s["gross_profit"].sum(), s["gross_loss"].sum()
    net_total = float(trades["pnl"].sum()) if not trades.empty else 0.0
    top3 = trades["pnl"].nlargest(3) if not trades.empty else pd.Series(dtype=float)
    top3_sum = float(top3.sum())
    # mean net without the three best trades (their PnL removed from their own windows)
    ex = s.set_index("window")["net_pct"].copy()
    if not trades.empty:
        for idx in top3.index:
            ex[trades.at[idx, "window"]] -= trades.at[idx, "pnl"]
    reasons = trades["reason"].value_counts().to_dict() if not trades.empty else {}
    protect_exits = sum(v for k, v in reasons.items() if k in ("protect_stop", "crash_guard", "take_profit"))
    return {
        "windows": len(s), "pos": int((s["net_pct"] > 0).sum()),
        "mean_net": s["net_pct"].mean(), "median_net": s["net_pct"].median(), "worst_net": s["net_pct"].min(),
        "best_net": s["net_pct"].max(), "worst_dd": s["max_dd_pct"].max(), "mean_dd": s["max_dd_pct"].mean(),
        "pf": gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0),
        "trades": int(s["trades"].sum()), "killed": int(s["killed"].sum()),
        "net_usdt": net_total, "top3_usdt": top3_sum,
        "top3_share": top3_sum / net_total if net_total > 0 else float("nan"),
        "mean_net_ex_top3": ex.mean(),
        "protect_exits": int(protect_exits),
        "unc_mean_net": s["unc_net_pct"].mean(), "unc_worst_net": s["unc_net_pct"].min(),
        "unc_worst_dd": s["unc_max_dd_pct"].max(),
        "robust": s["net_pct"].mean() + 0.5 * s["net_pct"].min() - 0.3 * s["max_dd_pct"].max(),
    }


def summarize() -> pd.DataFrame:
    rows = []
    for path in sorted(OUT.glob("*_*.csv")):
        if path.name.startswith(("mfe_", "summary")):
            continue
        family, variant = path.stem.split("_", 1)
        if family not in FAMILIES:
            continue
        s = pd.read_csv(path)
        tpath = OUT / "trades" / path.name
        trades = pd.read_csv(tpath) if tpath.exists() and tpath.stat().st_size > 1 else pd.DataFrame()
        for block, ids in BLOCKS.items():
            sb = s[s["window"].isin(ids)]
            if len(sb) != len(ids):
                continue
            tb = trades[trades["window"].isin(ids)] if not trades.empty else trades
            rows.append({"family": family, "variant": variant, "block": block, **block_stats(sb, tb)})
    table = pd.DataFrame(rows)
    table.to_csv(OUT / "summary.csv", index=False)
    return table


def print_summary(table: pd.DataFrame) -> None:
    cols = ["variant", "pos", "mean_net", "median_net", "worst_net", "worst_dd", "pf", "trades", "killed",
            "top3_share", "mean_net_ex_top3", "protect_exits", "robust"]
    for family in FAMILIES:
        for block in ("all", "is", "oos"):
            sub = table[(table["family"] == family) & (table["block"] == block)]
            if sub.empty:
                continue
            base = sub[sub["variant"] == "base"]
            order = list(base.index) + list(sub.drop(base.index).sort_values("robust", ascending=False).index)
            print(f"\n== {family} / {block} ({sub['windows'].iloc[0]} windows) ==", flush=True)
            print(sub.loc[order, cols].to_string(index=False, float_format=lambda v: f"{v:.2f}"), flush=True)


def print_mfe() -> None:
    for family in FAMILIES:
        path = OUT / f"mfe_{family}.csv"
        if not path.exists():
            continue
        m = pd.read_csv(path)
        big = m[m["mfe_pct"] >= 10]
        print(f"\n== {family}: peak open gain vs realised (base, {len(m)} trades) ==", flush=True)
        print(f"trades with peak >= +10%: {len(big)}; mean peak {big['mfe_pct'].mean():+.1f}%, "
              f"mean realised {big['realised_pct'].mean():+.1f}%, "
              f"kept < half of the peak: {(big['realised_pct'] < 0.5 * big['mfe_pct']).sum()}, "
              f"closed below entry: {(big['realised_pct'] < 0).sum()}", flush=True)
        keep = m[m["mfe_pct"] > 0].copy()
        keep["kept"] = keep["realised_pct"] / keep["mfe_pct"]
        print(f"all trades with a positive peak: {len(keep)}; median share of the peak kept "
              f"{keep['kept'].median():.2f}; PnL share of trades whose peak was >= 10%: "
              f"{big['pnl'].sum() / m['pnl'].sum():.0%}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default=None, help="mom | turtle (default both)")
    parser.add_argument("--variants", default=None, help="comma list; a+b merges two variants")
    parser.add_argument("--combo", action="append", default=[], help="extra a+b variant(s) to run")
    parser.add_argument("--windows", nargs="*", default=None)
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--summary", action="store_true", help="only rebuild summary.csv from disk")
    args = parser.parse_args()

    (OUT / "trades").mkdir(parents=True, exist_ok=True)
    if not args.summary:
        families = [args.family] if args.family else list(FAMILIES)
        names = [n.strip() for n in args.variants.split(",")] if args.variants else list(VARIANTS)
        names += [c for c in args.combo if c not in names]
        windows = [WINDOW_BY_ID[w] for w in (args.windows or ALL_WINDOWS)]
        started = time.time()
        for family in families:
            run_family(family, names, windows, args.capital)
        _log(f"runs done in {(time.time() - started) / 60:.1f} min -> {OUT}")
    table = summarize()
    if table.empty:
        _log("nothing on disk")
        return
    print_summary(table)
    print_mfe()


if __name__ == "__main__":
    main()
