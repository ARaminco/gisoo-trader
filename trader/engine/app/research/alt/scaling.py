"""Staged exits (scale-out) and staged entries (scale-in) for the two live 4h families
(docs/research-scaling.md).

    python -m app.research.alt.scaling                          both families, every variant, 12 windows
    python -m app.research.alt.scaling --parity                 base through the extended sim == published CSVs
    python -m app.research.alt.scaling --family mom --variants base,so3_5_12_g8
    python -m app.research.alt.scaling --combo so3_8_20_g8+si_atr3_split   merge two variants' overrides
    python -m app.research.alt.scaling --summary                rebuild summary.csv from the files on disk
    python -m app.research.alt.scaling --dot so3_8_20_g8        matched peak-bucket view (the DOT question)
    python -m app.research.alt.scaling --per-window a,b         per-window table of named variants

The base is exactly `xs_momentum_regime_exit` / `turtle_regime_risk1` as validated, run
through alt.sim.simulate_ext with the live circuit breakers on (censored) and off. Every
variant is the same AltParams plus the scale-out / scale-in hooks. A scale-out leg is its
own trade row (`partial=True`), so the tables below aggregate rows into round trips
(one entry = one trade) before counting trades, profit factor and the top-3 share; net %,
drawdown and the money-weighted columns come from the equity curve and need no
aggregation.

Results: RESEARCH_DIR/results/alt/scaling/<family>_<variant>.csv (per window),
trades/<family>_<variant>.csv (censored rows of all windows), summary.csv (one row per
family x variant x block: is = w01-w06, oos = w07-w12, all), mfe_<family>.csv (peak open
gain of every base round trip, for the matched view).
"""
from __future__ import annotations

import argparse
import dataclasses
import math
import time

import numpy as np
import pandas as pd

from app.indicators import core as ta
from app.research.alt.protection import ALL_WINDOWS, BLOCKS, FAMILIES, IS_WINDOWS, OOS_WINDOWS
from app.research.alt.run import ALT_DIR, _flat, _log, build_frames, uncensored
from app.research.alt.sim import simulate_ext
from app.research.data import RESULTS_DIR
from app.research.portfolio import SimResult, trades_frame
from app.research.windows import WINDOW_BY_ID, Window

OUT = RESULTS_DIR / "alt" / "scaling"
KEY = ["window", "symbol", "entry_time"]
THIRD = 1.0 / 3.0


def variants() -> dict[str, dict]:
    """name -> AltParams overrides. `base` is the validated family unchanged.

    so3_{T1}_{T2}_g{G}: 1/3 at +T1 %, 1/3 at +T2 %, the rest rides; G = give-back on the rest (0 = none).
    so2_{T1}_g{G}:      1/2 at +T1 %, the rest rides.
    si_atr3_{risk}:     3 tranches, adds at +0.5 ATR and +1.0 ATR (each 0.5 ATR above the previous fill).
    si_atr2_{risk}:     2 tranches, one add at +1.0 ATR.
    si_pb2 / si_pb3:    2 / 3 tranches, adds on a pullback to EMA20 or previous fill - 0.5 ATR.
    risk = split (total planned risk = base, first tranche 1/N) or extra (first tranche full, adds extra).
    base_r67 / base_r50: the base at 2/3 and 1/2 of its risk - the size control a "split" scale-in
    (which runs at lower exposure) has to beat.
    """
    v: dict[str, dict] = {"base": {}, "base_r67": dict(size_multiplier=2.0 / 3.0), "base_r50": dict(size_multiplier=0.5)}
    for t1 in (5, 8, 12):
        for t2 in (12, 20):
            if t2 <= t1:
                continue
            for g in (None, 5, 8, 12):
                v[f"so3_{t1}_{t2}_g{g or 0}"] = dict(
                    scale_out=((float(t1), THIRD), (float(t2), THIRD)), remainder_giveback_pct=g,
                )
    for t1 in (5, 8, 12):
        for g in (None, 5, 8, 12):
            v[f"so2_{t1}_g{g or 0}"] = dict(scale_out=((float(t1), 0.5),), remainder_giveback_pct=g)
    for risk in ("split", "extra"):
        v[f"si_atr3_{risk}"] = dict(scale_in="atr", scale_in_tranches=3, scale_in_step_atr=0.5, scale_in_risk=risk)
        v[f"si_atr2_{risk}"] = dict(scale_in="atr", scale_in_tranches=2, scale_in_step_atr=1.0, scale_in_risk=risk)
        v[f"si_pb2_{risk}"] = dict(scale_in="pullback", scale_in_tranches=2, scale_in_risk=risk)
        v[f"si_pb3_{risk}"] = dict(scale_in="pullback", scale_in_tranches=3, scale_in_risk=risk)
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


def add_ema20(frames: dict[str, pd.DataFrame]) -> None:
    """EMA20 of the frame's own closes (the same series features() computes) for the pullback rule."""
    for df in frames.values():
        if "ema20" not in df.columns:
            df["ema20"] = ta.ema(df["close"], 20).fillna(0.0)


# --------------------------------------------------------------------------
# round trips and per-window rows
# --------------------------------------------------------------------------


def round_trips(rows: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trade rows (legs + final close) into one row per entry."""
    if rows.empty:
        return pd.DataFrame(columns=[*KEY, "pnl", "notional", "fees", "legs", "legs_under_min", "adds",
                                     "adds_under_min", "reason", "exit_time", "bars_held", "pnl_pct"])
    if "partial" not in rows.columns:
        rows = rows.assign(partial=False, leg=0, adds=0, under_min=False, adds_under_min=0)
    final = rows[~rows["partial"]].set_index(KEY)
    rows = rows.assign(leg_under_min=(rows["partial"] & rows["under_min"]).astype(int))
    g = rows.groupby(KEY, sort=False)
    out = pd.DataFrame({
        "pnl": g["pnl"].sum(),
        "notional": g["notional"].sum(),
        "fees": g["fees"].sum(),
        "legs": g["partial"].sum().astype(int),
        "legs_under_min": g["leg_under_min"].sum().astype(int),
        "adds": g["adds"].max().astype(int),
        "adds_under_min": g["adds_under_min"].max().astype(int),
        "rows": g.size(),
    })
    out["reason"] = final["reason"].reindex(out.index)
    out["exit_time"] = final["exit_time"].reindex(out.index)
    out["bars_held"] = final["bars_held"].reindex(out.index)
    out["remainder_under_min"] = final["under_min"].reindex(out.index).fillna(False).astype(bool)
    out["pnl_pct"] = out["pnl"] / out["notional"] * 100
    return out.reset_index()


def _daily_stats(res: SimResult) -> dict:
    curve = pd.Series(
        [e for _, e in res.equity_curve],
        index=pd.to_datetime([t for t, _ in res.equity_curve], unit="ms", utc=True),
    )
    daily = curve.resample("1D").last().dropna().pct_change().dropna()
    inv = getattr(res, "invested_curve", None) or [0.0]
    return {
        "dr_n": int(len(daily)), "dr_sum": float(daily.sum()), "dr_sumsq": float((daily ** 2).sum()),
        "inv_mean": float(np.mean(inv)), "eq_mean": float(curve.mean()), "bars": int(len(curve)),
    }


def window_row(family: str, name: str, w: Window, res: SimResult, unc: SimResult) -> tuple[dict, pd.DataFrame]:
    m, mu = res.metrics, unc.metrics
    rows = trades_frame(res)
    if not rows.empty:
        rows.insert(0, "window", w.id)
    rt = round_trips(rows)
    urt = round_trips(trades_frame(unc).assign(window=w.id) if unc.trades else pd.DataFrame())
    gp, gl = float(rt.loc[rt["pnl"] > 0, "pnl"].sum()), float(abs(rt.loc[rt["pnl"] <= 0, "pnl"].sum()))
    ugp, ugl = float(urt.loc[urt["pnl"] > 0, "pnl"].sum()), float(abs(urt.loc[urt["pnl"] <= 0, "pnl"].sum()))
    reasons = rt["reason"].value_counts().to_dict() if not rt.empty else {}
    row = {
        "family": family, "variant": name, "window": w.id, "regime": w.regime, "days": w.days,
        **{k: v for k, v in _flat(m).items() if k not in ("trades", "profit_factor")},
        "trades": int(len(rt)), "rows": int(len(rows)),
        "legs": int(rt["legs"].sum()) if not rt.empty else 0,
        "legs_under_min": int(rt["legs_under_min"].sum()) if not rt.empty else 0,
        "remainders_under_min": int(rt["remainder_under_min"].sum()) if not rt.empty else 0,
        "adds": int(rt["adds"].sum()) if not rt.empty else 0,
        "adds_under_min": int(rt["adds_under_min"].sum()) if not rt.empty else 0,
        "profit_factor": gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0),
        "killed": res.kill_triggered_at is not None, "daily_pauses": res.daily_pauses,
        "gross_profit": gp, "gross_loss": gl, "exit_reasons": str(reasons),
        **_daily_stats(res),
        "unc_trades": int(len(urt)), "unc_net_pct": mu.get("net_pct", 0.0),
        "unc_max_dd_pct": mu.get("max_dd_pct", 0.0),
        "unc_profit_factor": ugp / ugl if ugl > 0 else (99.0 if ugp > 0 else 0.0),
        "unc_gross_profit": ugp, "unc_gross_loss": ugl,
    }
    return row, rows


def mfe_table(family: str, rows: pd.DataFrame, frames_by_window: dict[str, dict]) -> pd.DataFrame:
    """Peak open gain (highest close after entry, % of entry) of every base round trip."""
    out = []
    for r in round_trips(rows).itertuples(index=False):
        df = frames_by_window[r.window].get(r.symbol)
        if df is None:
            continue
        sub = df[(df["open_time"] > r.entry_time) & (df["open_time"] <= r.exit_time)]
        entry = float(rows[(rows["window"] == r.window) & (rows["symbol"] == r.symbol)
                           & (rows["entry_time"] == r.entry_time)]["entry_price"].iloc[0])
        peak = float(sub["close"].max()) if not sub.empty else entry
        out.append({"family": family, "window": r.window, "symbol": r.symbol, "entry_time": r.entry_time,
                    "mfe_pct": (peak - entry) / entry * 100, "realised_pct": r.pnl_pct, "pnl": r.pnl})
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def run_family(family: str, names: list[str], windows: list[Window], capital: float) -> None:
    spec = FAMILIES[family]
    frames_by_window: dict[str, dict] = {}
    for w in windows:
        t0 = time.time()
        frames_by_window[w.id] = build_frames(spec, w)
        add_ema20(frames_by_window[w.id])
        _log(f"{family} frames {w.id}: {len(frames_by_window[w.id])} symbols ({time.time() - t0:.0f}s)")

    for name in names:
        params = dataclasses.replace(spec.params, **resolve(name))
        out_rows: list[dict] = []
        pooled: list[pd.DataFrame] = []
        for w in windows:
            frames = frames_by_window[w.id]
            if not frames:
                continue
            res = simulate_ext(frames, spec.tf, params, capital, w.start_ms, w.end_ms)
            unc = simulate_ext(frames, spec.tf, uncensored(params), capital, w.start_ms, w.end_ms)
            row, rows = window_row(family, name, w, res, unc)
            out_rows.append(row)
            if not rows.empty:
                pooled.append(rows)
            _log(
                f"{family:<6} {name:<18} {w.id:<24} trades={row['trades']:>3} legs={row['legs']:>3} "
                f"adds={row['adds']:>3} net={row['net_pct']:>+7.2f}% dd={row['max_dd_pct']:>5.2f}% "
                f"pf={min(row['profit_factor'], 99):>5.2f} | unc net={row['unc_net_pct']:>+7.2f}%"
            )
        pd.DataFrame(out_rows).to_csv(OUT / f"{family}_{name}.csv", index=False)
        trades = pd.concat(pooled, ignore_index=True) if pooled else pd.DataFrame()
        trades.to_csv(OUT / "trades" / f"{family}_{name}.csv", index=False)
        if name == "base" and not trades.empty:
            mfe_table(family, trades, frames_by_window).to_csv(OUT / f"mfe_{family}.csv", index=False)


# --------------------------------------------------------------------------
# parity with the published CSVs
# --------------------------------------------------------------------------

PARITY_COLS = ["trades", "net_pct", "max_dd_pct", "profit_factor", "gross_profit", "gross_loss",
               "unc_trades", "unc_net_pct", "unc_max_dd_pct", "unc_profit_factor"]


def parity(capital: float, tol: float = 1e-9) -> bool:
    """Base through the extended simulator must equal research/results/alt/<family>_4h[_oos].csv
    and the per-window trade lists in research/results/alt/trades/, to the last digit."""
    ok = True
    for family, spec in FAMILIES.items():
        for tag, ids in (("", IS_WINDOWS), ("_oos", OOS_WINDOWS)):
            pub = pd.read_csv(ALT_DIR / f"{spec.name}_{spec.tf}{tag}.csv").set_index("window")
            for wid in ids:
                w = WINDOW_BY_ID[wid]
                frames = build_frames(spec, w)
                add_ema20(frames)
                res = simulate_ext(frames, spec.tf, spec.params, capital, w.start_ms, w.end_ms)
                unc = simulate_ext(frames, spec.tf, uncensored(spec.params), capital, w.start_ms, w.end_ms)
                row, rows = window_row(family, "base", w, res, unc)
                ref = pub.loc[wid]
                bad = [c for c in PARITY_COLS if abs(float(row[c]) - float(ref[c])) > tol]
                tpath = ALT_DIR / "trades" / f"{spec.name}_{spec.tf}_{wid}{tag}.csv"
                if tpath.exists():
                    ref_t = pd.read_csv(tpath)
                    same = len(ref_t) == len(rows) and bool(
                        np.allclose(ref_t["pnl"].to_numpy(), rows["pnl"].to_numpy(), atol=tol)
                        and (ref_t["exit_time"].to_numpy() == rows["exit_time"].to_numpy()).all()
                        and (ref_t["reason"].to_numpy() == rows["reason"].to_numpy()).all()
                    )
                    if not same:
                        bad.append("trade_list")
                elif len(rows):
                    bad.append("trade_list_missing")
                if not rows.empty and (rows["partial"].any() or rows["adds"].any()):
                    bad.append("scaling_rows_in_base")
                ok &= not bad
                _log(f"parity {family:<6} {wid:<24} trades={row['trades']:>3} net={row['net_pct']:>+8.4f} "
                     f"(pub {float(ref['net_pct']):>+8.4f}) -> {'OK' if not bad else 'MISMATCH ' + ','.join(bad)}")
    _log("parity " + ("PASSED" if ok else "FAILED"))
    return ok


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def block_stats(s: pd.DataFrame, rt: pd.DataFrame) -> dict:
    gp, gl = s["gross_profit"].sum(), s["gross_loss"].sum()
    net_total = float(rt["pnl"].sum()) if not rt.empty else 0.0
    top3 = rt["pnl"].nlargest(3) if not rt.empty else pd.Series(dtype=float)
    top3_sum = float(top3.sum())
    ex = s.set_index("window")["net_pct"].copy()
    if not rt.empty:
        for idx in top3.index:
            ex[rt.at[idx, "window"]] -= rt.at[idx, "pnl"]
    n = int(s["dr_n"].sum())
    mean_d = s["dr_sum"].sum() / n if n else 0.0
    var_d = s["dr_sumsq"].sum() / n - mean_d ** 2 if n > 2 else 0.0
    sharpe = mean_d / math.sqrt(var_d) * math.sqrt(365) if var_d > 0 else 0.0
    bars = float(s["bars"].sum())
    inv = float((s["inv_mean"] * s["bars"]).sum() / bars) if bars else 0.0
    eq = float((s["eq_mean"] * s["bars"]).sum() / bars) if bars else 0.0
    legs = int(s["legs"].sum())
    adds = int(s["adds"].sum())
    return {
        "windows": len(s), "pos": int((s["net_pct"] > 0).sum()),
        "mean_net": s["net_pct"].mean(), "median_net": s["net_pct"].median(), "worst_net": s["net_pct"].min(),
        "best_net": s["net_pct"].max(), "worst_dd": s["max_dd_pct"].max(), "mean_dd": s["max_dd_pct"].mean(),
        "pf": gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0),
        "trades": int(s["trades"].sum()), "rows": int(s["rows"].sum()), "killed": int(s["killed"].sum()),
        "legs": legs, "legs_under_min": int(s["legs_under_min"].sum()),
        "legs_under_min_share": s["legs_under_min"].sum() / legs if legs else 0.0,
        "remainders_under_min": int(s["remainders_under_min"].sum()),
        "adds": adds, "adds_under_min": int(s["adds_under_min"].sum()),
        "adds_under_min_share": s["adds_under_min"].sum() / adds if adds else 0.0,
        "net_usdt": net_total, "top3_usdt": top3_sum,
        "top3_share": top3_sum / net_total if net_total > 0 else float("nan"),
        "mean_net_ex_top3": ex.mean(),
        "leg_pnl": float(rt["legs"].gt(0).sum()) if not rt.empty else 0.0,
        # money-weighted view: compounded equity across the block, pooled daily Sharpe, capital deployed
        "compound": (np.prod(1 + s["net_pct"] / 100) - 1) * 100,
        "sharpe": sharpe,
        "exposure": inv / eq if eq else 0.0,
        "ret_on_deployed": net_total / (inv * len(s)) * 100 if inv > 0 else 0.0,
        "unc_mean_net": s["unc_net_pct"].mean(), "unc_worst_net": s["unc_net_pct"].min(),
        "unc_worst_dd": s["unc_max_dd_pct"].max(),
        "robust": s["net_pct"].mean() + 0.5 * s["net_pct"].min() - 0.3 * s["max_dd_pct"].max(),
    }


def load_rt(family: str, variant: str) -> pd.DataFrame:
    tpath = OUT / "trades" / f"{family}_{variant}.csv"
    if not tpath.exists() or tpath.stat().st_size <= 1:
        return pd.DataFrame()
    return round_trips(pd.read_csv(tpath))


def summarize() -> pd.DataFrame:
    rows = []
    for path in sorted(OUT.glob("*_*.csv")):
        if path.name.startswith(("mfe_", "summary")):
            continue
        family, variant = path.stem.split("_", 1)
        if family not in FAMILIES:
            continue
        s = pd.read_csv(path)
        rt = load_rt(family, variant)
        for block, ids in BLOCKS.items():
            sb = s[s["window"].isin(ids)]
            if len(sb) != len(ids):
                continue
            rb = rt[rt["window"].isin(ids)] if not rt.empty else rt
            rows.append({"family": family, "variant": variant, "block": block, **block_stats(sb, rb)})
    table = pd.DataFrame(rows)
    table.to_csv(OUT / "summary.csv", index=False)
    return table


SHOW = ["variant", "pos", "mean_net", "median_net", "worst_net", "worst_dd", "pf", "trades", "legs", "adds",
        "top3_share", "mean_net_ex_top3", "compound", "sharpe", "exposure", "ret_on_deployed", "robust"]


def print_summary(table: pd.DataFrame, names: list[str] | None = None) -> None:
    for family in FAMILIES:
        for block in ("all", "is", "oos"):
            sub = table[(table["family"] == family) & (table["block"] == block)]
            if names:
                sub = sub[sub["variant"].isin(names)]
            if sub.empty:
                continue
            base = sub[sub["variant"] == "base"]
            order = list(base.index) + list(sub.drop(base.index).sort_values("robust", ascending=False).index)
            print(f"\n== {family} / {block} ({sub['windows'].iloc[0]} windows) ==", flush=True)
            print(sub.loc[order, SHOW].to_string(index=False, float_format=lambda v: f"{v:.2f}"), flush=True)
    flagged = table[(table["block"] == "all") & ((table["legs_under_min_share"] > 0) | (table["adds_under_min"] > 0))]
    if not flagged.empty:
        print("\n== exchange minimum (5 USDT per leg / add at 100 USDT start capital) ==", flush=True)
        cols = ["family", "variant", "legs", "legs_under_min", "legs_under_min_share", "remainders_under_min",
                "adds", "adds_under_min", "adds_under_min_share"]
        print(flagged[cols].to_string(index=False, float_format=lambda v: f"{v:.2f}"), flush=True)


def per_window(names: list[str], markdown: bool = False) -> None:
    for family in FAMILIES:
        frames = []
        for name in names:
            path = OUT / f"{family}_{name}.csv"
            if not path.exists():
                continue
            s = pd.read_csv(path).set_index("window")
            frames.append(s.apply(
                lambda r: f"{int(r['trades'])} / {r['net_pct']:+.1f} / {r['max_dd_pct']:.1f} / "
                          f"{min(r['profit_factor'], 99):.2f}{' K' if r['killed'] else ''}", axis=1).rename(name))
        if not frames:
            continue
        table = pd.concat(frames, axis=1)
        if markdown:
            print(f"\n**{family} per window** (trades / net % / max DD % / PF; K = kill switch fired)\n")
            print("| window | " + " | ".join(table.columns) + " |")
            print("|---|" + "---|" * len(table.columns))
            for wid, row in table.iterrows():
                print(f"| {wid} | " + " | ".join(row) + " |")
        else:
            print(f"\n== {family} per window (trades / net % / max DD % / PF; K = kill switch) ==", flush=True)
            print(table.to_string(), flush=True)


MD_COLS = [
    ("pos", "pos", "{:d}"), ("mean_net", "mean", "{:+.2f}"), ("median_net", "median", "{:+.2f}"),
    ("worst_net", "worst", "{:+.2f}"), ("worst_dd", "worst DD", "{:.2f}"), ("pf", "PF", "{:.2f}"),
    ("trades", "trades", "{:d}"), ("legs", "legs", "{:d}"), ("adds", "adds", "{:d}"),
    ("top3_share", "top-3 share", "{:.2f}"), ("mean_net_ex_top3", "ex-top-3", "{:+.2f}"),
    ("compound", "compound", "{:+.1f}"), ("sharpe", "Sharpe", "{:.2f}"), ("exposure", "exposure", "{:.2f}"),
    ("ret_on_deployed", "ret/deployed", "{:.1f}"), ("robust", "robust", "{:.2f}"),
]
CONTROLS = ["base", "base_r67", "base_r50"]


def markdown_tables(table: pd.DataFrame, names: list[str] | None = None) -> None:
    """Summary tables as markdown: base and the size controls first, then by the robust score."""
    for family in FAMILIES:
        for block, title in (("all", "12 windows (2021-2026)"), ("oos", "out-of-sample w07-w12 (Dec 2023 - Sep 2026)"),
                             ("is", "in-sample w01-w06 (2021-2023)")):
            sub = table[(table["family"] == family) & (table["block"] == block)]
            if names:
                sub = sub[sub["variant"].isin(names)]
            if sub.empty:
                continue
            first = [i for c in CONTROLS for i in sub.index[sub["variant"] == c]]
            rest = sub.drop(first).sort_values("robust", ascending=False).index
            print(f"\n**{family} / {title}**\n")
            print("| variant | " + " | ".join(h for _, h, _ in MD_COLS) + " |")
            print("|---|" + "---:|" * len(MD_COLS))
            for idx in [*first, *rest]:
                r = sub.loc[idx]
                name = f"**{r['variant']}**" if r["variant"] == "base" else r["variant"]
                cells = []
                for col, _, fmt in MD_COLS:
                    v = r[col]
                    cells.append(fmt.format(int(v)) if fmt == "{:d}" else ("nan" if pd.isna(v) else fmt.format(v)))
                print(f"| {name} | " + " | ".join(cells) + " |")


BUCKETS = [(-1e9, 0, "never positive"), (0, 5, "0-5 %"), (5, 10, "5-10 %"), (10, 20, "10-20 %"),
           (20, 40, "20-40 %"), (40, 1e9, ">= 40 %")]


def dot_view(family: str, variant: str, block: str = "all") -> pd.DataFrame:
    """Matched comparison: for every base round trip (keyed by window/symbol/entry time) the
    realised % of the same entry under `variant`, bucketed by the base trade's peak open gain."""
    mfe = pd.read_csv(OUT / f"mfe_{family}.csv")
    base = load_rt(family, "base").merge(mfe[[*KEY, "mfe_pct"]], on=KEY)
    var = load_rt(family, variant)
    ids = BLOCKS[block]
    base = base[base["window"].isin(ids)]
    var = var[var["window"].isin(ids)]
    m = base.merge(var, on=KEY, how="left", suffixes=("", "_v"))
    matched = m["pnl_v"].notna()
    out = []
    for lo, hi, label in BUCKETS:
        b = m[(m["mfe_pct"] > lo) & (m["mfe_pct"] <= hi)] if lo > -1e9 else m[m["mfe_pct"] <= 0]
        bm = b[b["pnl_v"].notna()]
        out.append({
            "peak bucket": label, "trades": len(b), "matched": len(bm),
            "base mean %": b["pnl_pct"].mean(), "variant mean %": bm["pnl_pct_v"].mean(),
            "base median %": b["pnl_pct"].median(), "variant median %": bm["pnl_pct_v"].median(),
            "base PnL": b["pnl"].sum(), "variant PnL": bm["pnl_v"].sum(),
            "base < 0": int((b["pnl"] < 0).sum()), "variant < 0": int((bm["pnl_v"] < 0).sum()),
        })
    table = pd.DataFrame(out)
    print(f"\n== {family} / {variant} / {block}: matched by base peak bucket "
          f"({int(matched.sum())} of {len(m)} base entries also taken by the variant; "
          f"variant has {len(var)} round trips) ==", flush=True)
    print(table.to_string(index=False, float_format=lambda v: f"{v:.2f}"), flush=True)
    return table


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default=None, help="mom | turtle (default both)")
    parser.add_argument("--variants", default=None, help="comma list; a+b merges two variants")
    parser.add_argument("--combo", action="append", default=[], help="extra a+b variant(s) to run")
    parser.add_argument("--windows", nargs="*", default=None)
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--parity", action="store_true", help="prove base == the published CSVs, then exit")
    parser.add_argument("--summary", action="store_true", help="only rebuild summary.csv from disk")
    parser.add_argument("--show", default=None, help="comma list of variants to print (default all)")
    parser.add_argument("--dot", action="append", default=[], help="variant(s) for the matched peak-bucket view")
    parser.add_argument("--per-window", default=None, help="comma list of variants for the per-window table")
    parser.add_argument("--markdown", action="store_true", help="print the summary (and --per-window) tables as markdown")
    args = parser.parse_args()

    (OUT / "trades").mkdir(parents=True, exist_ok=True)
    if args.parity:
        raise SystemExit(0 if parity(args.capital) else 1)
    if args.markdown:
        table = summarize()
        markdown_tables(table, [n.strip() for n in args.show.split(",")] if args.show else None)
        if args.per_window:
            per_window([n.strip() for n in args.per_window.split(",")], markdown=True)
        return
    if not args.summary and not args.dot and not args.per_window:
        families = [args.family] if args.family else list(FAMILIES)
        names = [n.strip() for n in args.variants.split(",")] if args.variants else list(VARIANTS)
        names += [c for c in args.combo if c not in names]
        windows = [WINDOW_BY_ID[w] for w in (args.windows or ALL_WINDOWS)]
        started = time.time()
        for family in families:
            run_family(family, names, windows, args.capital)
        _log(f"runs done in {(time.time() - started) / 60:.1f} min -> {OUT}")
    elif args.summary and args.combo:
        families = [args.family] if args.family else list(FAMILIES)
        windows = [WINDOW_BY_ID[w] for w in (args.windows or ALL_WINDOWS)]
        for family in families:
            run_family(family, list(args.combo), windows, args.capital)
    table = summarize()
    if table.empty:
        _log("nothing on disk")
        return
    if args.dot:
        for name in args.dot:
            for family in ([args.family] if args.family else list(FAMILIES)):
                for block in ("all", "oos"):
                    dot_view(family, name, block)
        return
    if args.per_window:
        per_window([n.strip() for n in args.per_window.split(",")])
        return
    print_summary(table, [n.strip() for n in args.show.split(",")] if args.show else None)


if __name__ == "__main__":
    main()
