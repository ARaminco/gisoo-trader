"""Allocation study: the validated 4h trend sleeve next to PAXG (gold), BTC and USDT cash.

Trend sleeve = the two live families (xs_momentum_regime_exit_4h, turtle_regime_risk1_4h),
approximated from their published uncensored per-window equity curves as
CAUTION x mean(daily return of the two families) inside each regime window and
flat between windows ("avg" mode). "sum" mode (CAUTION x sum of the two) is the
upper bound for one book that runs both families at full risk on the same equity.
The approximation is checked against the scaling study's exact base_r67 runs.

Outputs (RESEARCH_DIR/results/alloc):
  paxg_stats.csv, paxg_years.csv  gold 2020-2026: return, vol, drawdown, worst 60d, correlations
  paxg_windows.csv                PAXG / BTC / ETH / trend sleeve inside the twelve windows
  sleeve_check.csv                approximation vs base_r67, deployment per window
  grid.csv                        every allocation x yield over 2021-01-01 -> 2026-09-01
  grid_windows.csv                per-window returns of every allocation (yield 3%)
  idle_cash.csv                   question 4: the never-deployed 55% in gold / cash / BTC
  recommended_windows.csv         recommended mix vs bot-only per window and per year
  study.log                       every table printed by the run

    python -m app.research.alloc.study [--caution 0.67] [--mode avg|sum]
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

from app.research.alloc.fetch_daily import load_daily
from app.research.data import RESULTS_DIR
from app.research.windows import WINDOWS, Window

OUT_DIR = RESULTS_DIR / "alloc"
CURVES = RESULTS_DIR / "alt" / "curves"
SCALING = RESULTS_DIR / "alt" / "scaling"
CAUTION = 0.67
SPAN_START = pd.Timestamp("2021-01-01", tz="UTC")
SPAN_END = pd.Timestamp("2026-09-01", tz="UTC")  # exclusive: the end of the last window
DAY = pd.Timedelta(days=1)
FEE = {"T": 0.0, "G": 0.002, "B": 0.001, "C": 0.0}  # PAXG 0.1% fee + ~0.1% spread; BTC 0.1%; USDT<->USDT free
FAMILIES = {"mom": "xs_momentum_regime_exit_4h", "turtle": "turtle_regime_risk1_4h"}
KEYS = ("T", "G", "B", "C")
GRID_T = [100, 80, 70, 60, 50]
GRID_G = [0, 15, 20, 30, 40]
GRID_B = [0, 10, 20]
GRID_Y = [0.0, 0.03, 0.05]
BASE_APY = 0.03

_log_lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    _log_lines.append(text)


def ts(date: str) -> pd.Timestamp:
    return pd.Timestamp(date, tz="UTC")


def md(df: pd.DataFrame, cols: list[tuple[str, str, str]]) -> str:
    head = "| " + " | ".join(h for _, h, _ in cols) + " |"
    sep = "|" + "|".join("---" if f == "{}" else "---:" for _, _, f in cols) + "|"
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c, _, f in cols:
            v = r[c]
            cells.append("" if isinstance(v, float) and math.isnan(v) else f.format(v))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


# ----------------------------------------------------------------- data

def close_series(symbol: str) -> pd.Series:
    df = load_daily(symbol)
    if df is None:
        raise SystemExit(f"no daily data for {symbol}: run python -m app.research.alloc.fetch_daily")
    s = df.set_index("date")["close"].astype(float)
    return s[~s.index.duplicated()].sort_index()


def curve_path(family: str, window: Window):
    tag = "_oos" if window.id >= "w07" else ""
    return CURVES / f"{FAMILIES[family]}_{window.id}{tag}_unc.csv"


def family_daily_returns(family: str, window: Window) -> pd.Series:
    c = pd.read_csv(curve_path(family, window))
    c["date"] = pd.to_datetime(c["open_time"], unit="ms", utc=True).dt.normalize()
    eq = c.groupby("date")["equity"].last()
    eq = eq[(eq.index >= ts(window.start)) & (eq.index < ts(window.end))]
    prev = pd.concat([pd.Series([100.0], index=[eq.index[0] - DAY]), eq])
    return prev.pct_change().iloc[1:]


def build_sleeve(caution: float, mode: str) -> pd.DataFrame:
    idx = pd.date_range(SPAN_START, SPAN_END - DAY, freq="D")
    df = pd.DataFrame({"mom": 0.0, "turtle": 0.0, "in_window": False, "window": ""}, index=idx)
    for w in WINDOWS:
        for fam in FAMILIES:
            r = family_daily_returns(fam, w)
            df.loc[r.index, fam] = r.values
        mask = (df.index >= ts(w.start)) & (df.index < ts(w.end))
        df.loc[mask, "in_window"] = True
        df.loc[mask, "window"] = w.id
    scale = caution * (0.5 if mode == "avg" else 1.0)
    df["ret"] = scale * (df["mom"] + df["turtle"])
    df["flat"] = (df["mom"] == 0.0) & (df["turtle"] == 0.0)
    return df


# ----------------------------------------------------------------- simulation

def simulate(
    w: dict[str, float], apy: float, sleeve: pd.DataFrame, rets: dict[str, np.ndarray],
    trigger: str = "monthly", band: float = 0.05, t_yield: bool = True,
) -> tuple[pd.Series, dict]:
    """Daily mark-to-market of a target-weight book.

    T = USDT reserved for the bot (earns the bot's return; the cash yield only on days the bot
    has no position when t_yield), G = PAXG, B = BTC, C = USDT in Simple Earn at `apy`.
    trigger: monthly (every month end), quarterly, monthly_band (month end, only if a weight is
    off by more than `band`), daily_band (any day a weight is off by more than `band`), never.
    Rebalancing legs pay FEE per asset on the traded notional.
    """
    idx = sleeve.index
    n = len(idx)
    yd = (1 + apy) ** (1 / 365) - 1
    rT, flat = sleeve["ret"].to_numpy(), sleeve["flat"].to_numpy()
    rG, rB = rets["G"], rets["B"]
    months = idx.month.to_numpy()
    vals = {k: 100.0 * w.get(k, 0.0) for k in KEYS}
    out = np.empty(n)
    fees = 0.0
    nreb = 0
    for i in range(n):
        vals["T"] *= (1 + rT[i]) * ((1 + yd) if (t_yield and flat[i]) else 1.0)
        vals["G"] *= 1 + rG[i]
        vals["B"] *= 1 + rB[i]
        vals["C"] *= 1 + yd
        V = sum(vals.values())
        month_end = i == n - 1 or months[i + 1] != months[i]
        off = max(abs(vals[k] / V - w.get(k, 0.0)) for k in KEYS)
        if trigger == "monthly":
            do = month_end
        elif trigger == "quarterly":
            do = month_end and months[i] in (3, 6, 9, 12)
        elif trigger == "monthly_band":
            do = month_end and off > band
        elif trigger == "daily_band":
            do = off > band
        else:
            do = False
        if do and i < n - 1 and off > 1e-6:
            cost = sum(abs(w.get(k, 0.0) * V - vals[k]) * FEE[k] for k in KEYS)
            V -= cost
            fees += cost
            nreb += 1
            for k in KEYS:
                vals[k] = w.get(k, 0.0) * V
        out[i] = V
    return pd.Series(out, index=idx), {"fees": fees, "rebalances": nreb}


def simulate_overlay(
    gold_w: float, btc_w: float, apy: float, sleeve: pd.DataFrame, rets: dict[str, np.ndarray],
) -> tuple[pd.Series, dict]:
    """Bot sized on the WHOLE account while gold_w / btc_w of the account physically sit in PAXG / BTC.

    The bot's daily P&L (sleeve return x total value) lands in the USDT part, which also earns the
    cash yield on the bot's flat days. PAXG/BTC are rebalanced to their share at every month end.
    Upper bound: it assumes the bot is never short of free USDT (see the deployment table).
    """
    idx = sleeve.index
    n = len(idx)
    yd = (1 + apy) ** (1 / 365) - 1
    rT, flat = sleeve["ret"].to_numpy(), sleeve["flat"].to_numpy()
    rG, rB = rets["G"], rets["B"]
    months = idx.month.to_numpy()
    usdt, gold, btc = 100.0 * (1 - gold_w - btc_w), 100.0 * gold_w, 100.0 * btc_w
    out = np.empty(n)
    min_usdt_share = 1.0
    fees = 0.0
    for i in range(n):
        V_prev = usdt + gold + btc
        usdt += V_prev * rT[i]
        if flat[i]:
            usdt *= 1 + yd
        gold *= 1 + rG[i]
        btc *= 1 + rB[i]
        V = usdt + gold + btc
        min_usdt_share = min(min_usdt_share, usdt / V)
        if (i == n - 1 or months[i + 1] != months[i]) and i < n - 1:
            cost = abs(gold_w * V - gold) * FEE["G"] + abs(btc_w * V - btc) * FEE["B"]
            V -= cost
            fees += cost
            gold, btc = gold_w * V, btc_w * V
            usdt = V - gold - btc
        out[i] = V
    return pd.Series(out, index=idx), {"fees": fees, "min_usdt_share": min_usdt_share}


# ----------------------------------------------------------------- metrics

def window_stats(V: pd.Series, w: Window) -> tuple[float, float]:
    """(return, max drawdown) of a value series inside a window, measured from the prior close."""
    seg = V[(V.index >= ts(w.start)) & (V.index < ts(w.end))]
    prev = V[V.index < ts(w.start)]
    v0 = float(prev.iloc[-1]) if len(prev) else 100.0
    path = np.r_[v0, seg.to_numpy()]
    dd = (path / np.maximum.accumulate(path) - 1).min()
    return float(seg.iloc[-1] / v0 - 1), float(dd)


def metrics(V: pd.Series, sleeve: pd.DataFrame | None = None) -> dict:
    r = V.pct_change().dropna()
    days = (V.index[-1] - V.index[0]).days + 1
    cagr = (V.iloc[-1] / V.iloc[0]) ** (365.25 / days) - 1
    vol = r.std() * math.sqrt(365)
    sharpe = r.mean() / r.std() * math.sqrt(365) if r.std() > 0 else float("nan")
    dd = float((V / V.cummax() - 1).min())
    roll90 = (V / V.shift(90) - 1).dropna()
    roll180 = (V / V.shift(180) - 1).dropna()
    roll365 = (V / V.shift(365) - 1).dropna()
    wins = [window_stats(V, w) for w in WINDOWS]
    wr = np.array([x[0] for x in wins])
    worst_i = int(wr.argmin())
    out = {
        "total": V.iloc[-1] / V.iloc[0] - 1, "cagr": cagr, "vol": vol, "sharpe": sharpe,
        "maxdd": dd, "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
        "worst_win": wr.min(), "worst_win_id": WINDOWS[worst_i].id, "best_win": wr.max(),
        "win_pos": int((wr > 0).sum()), "win_mean": wr.mean(),
        "win_compound": float(np.prod(1 + wr) - 1),
        "worst_win_dd": min(x[1] for x in wins),
        "roll90_pos": float((roll90 > 0).mean()), "roll180_pos": float((roll180 > 0).mean()),
        "roll365_pos": float((roll365 > 0).mean()), "worst_90d": float(roll90.min()),
    }
    if sleeve is not None:
        rw = r[sleeve["in_window"].reindex(r.index).fillna(False).to_numpy()]
        out["sharpe_win"] = rw.mean() / rw.std() * math.sqrt(365) if rw.std() > 0 else float("nan")
    return out


def year_returns(V: pd.Series) -> dict[str, float]:
    out = {}
    for y, seg in V.groupby(V.index.year):
        prev = V[V.index < seg.index[0]]
        v0 = float(prev.iloc[-1]) if len(prev) else 100.0
        out[str(y)] = float(seg.iloc[-1] / v0 - 1)
    return out


# ----------------------------------------------------------------- sections

def paxg_section(closes: dict[str, pd.Series], sleeve: pd.DataFrame) -> None:
    rets = {k: s.pct_change().dropna() for k, s in closes.items()}
    g = rets["PAXGUSDT"]
    V = closes["PAXGUSDT"] / closes["PAXGUSDT"].iloc[0] * 100
    days = (V.index[-1] - V.index[0]).days + 1
    roll60 = (V / V.shift(60) - 1).dropna()
    m = {
        "first": str(V.index[0].date()), "last": str(V.index[-1].date()), "days": int(len(V)),
        "total": V.iloc[-1] / V.iloc[0] - 1,
        "cagr": (V.iloc[-1] / V.iloc[0]) ** (365.25 / days) - 1,
        "vol": g.std() * math.sqrt(365),
        "sharpe": g.mean() / g.std() * math.sqrt(365),
        "maxdd": float((V / V.cummax() - 1).min()),
        "worst_60d": float(roll60.min()), "worst_60d_end": str(roll60.idxmin().date()),
        "best_60d": float(roll60.max()),
        "roll90_pos": float(((V / V.shift(90) - 1).dropna() > 0).mean()),
        "roll365_pos": float(((V / V.shift(365) - 1).dropna() > 0).mean()),
        "worst_day": float(g.min()), "worst_day_date": str(g.idxmin().date()),
    }
    # the drawdown's dates
    dd = V / V.cummax() - 1
    trough = dd.idxmin()
    peak = V[:trough].idxmax()
    rec = V[trough:][V[trough:] >= V[peak]]
    m["dd_peak"] = str(peak.date())
    m["dd_trough"] = str(trough.date())
    m["dd_recovered"] = str(rec.index[0].date()) if len(rec) else "not yet"
    # correlations
    b, e = rets["BTCUSDT"], rets["ETHUSDT"]
    common = g.index.intersection(b.index)
    m["corr_btc_daily"] = float(g[common].corr(b[common]))
    m["corr_eth_daily"] = float(g[common].corr(e[common]))
    gw = (1 + g).resample("W").prod() - 1
    bw = (1 + b).resample("W").prod() - 1
    m["corr_btc_weekly"] = float(gw.corr(bw))
    gm = (1 + g).resample("ME").prod() - 1
    bm = (1 + b).resample("ME").prod() - 1
    m["corr_btc_monthly"] = float(gm.corr(bm))
    # vs the trend sleeve: only days inside windows (outside, the sleeve is flat by assumption)
    sl = sleeve["ret"]
    inwin = sleeve.index[sleeve["in_window"]]
    ci = inwin.intersection(g.index)
    m["corr_sleeve_daily_windows"] = float(g[ci].corr(sl[ci]))
    active = sleeve.index[sleeve["in_window"] & ~sleeve["flat"]].intersection(g.index)
    m["corr_sleeve_daily_active"] = float(g[active].corr(sl[active]))
    m["sleeve_active_days"] = int(len(active))
    # conditional behaviour
    worst_btc = b[common].nsmallest(int(0.02 * len(common)))
    m["btc_worst2pct_n"] = int(len(worst_btc))
    m["btc_worst2pct_btc_mean"] = float(worst_btc.mean())
    m["btc_worst2pct_paxg_mean"] = float(g[worst_btc.index].mean())
    m["btc_worst2pct_paxg_up"] = float((g[worst_btc.index] > 0).mean())
    worst_sl = sl[active].nsmallest(20)
    m["sleeve_worst20_sleeve_mean"] = float(worst_sl.mean())
    m["sleeve_worst20_paxg_mean"] = float(g[worst_sl.index].mean())
    m["sleeve_worst20_paxg_up"] = float((g[worst_sl.index] > 0).mean())
    # calendar years
    years = pd.DataFrame({"PAXG": year_returns(V)})
    for sym in ("BTCUSDT", "ETHUSDT"):
        Vs = closes[sym] / closes[sym].iloc[0] * 100
        years[sym[:3]] = pd.Series(year_returns(Vs))
    gy = g.groupby(g.index.year)
    years["PAXG_vol"] = gy.std() * math.sqrt(365)
    years["PAXG_maxdd"] = V.groupby(V.index.year).apply(lambda s: float((s / s.cummax() - 1).min()))
    years.index.name = "year"
    pd.DataFrame([m]).to_csv(OUT_DIR / "paxg_stats.csv", index=False)
    years.to_csv(OUT_DIR / "paxg_years.csv")

    log("## PAXG (tokenised gold) on Binance, daily closes")
    log(f"data {m['first']} -> {m['last']} ({m['days']} days)")
    log(f"total {m['total']:+.1%}, CAGR {m['cagr']:+.1%}, vol {m['vol']:.1%}, Sharpe {m['sharpe']:.2f}, "
        f"max DD {m['maxdd']:.1%} ({m['dd_peak']} -> {m['dd_trough']}, recovered {m['dd_recovered']})")
    log(f"worst 60-day {m['worst_60d']:+.1%} (ending {m['worst_60d_end']}), best 60-day {m['best_60d']:+.1%}, "
        f"worst day {m['worst_day']:+.1%} ({m['worst_day_date']})")
    log(f"rolling 90-day positive {m['roll90_pos']:.0%}, rolling 365-day positive {m['roll365_pos']:.0%}")
    log(f"corr(PAXG, BTC): daily {m['corr_btc_daily']:.2f}, weekly {m['corr_btc_weekly']:.2f}, monthly {m['corr_btc_monthly']:.2f}; "
        f"corr(PAXG, ETH) daily {m['corr_eth_daily']:.2f}")
    log(f"corr(PAXG, trend sleeve) daily inside windows {m['corr_sleeve_daily_windows']:.2f}, "
        f"on the sleeve's {m['sleeve_active_days']} active days {m['corr_sleeve_daily_active']:.2f}")
    log(f"BTC's worst 2% days (n={m['btc_worst2pct_n']}, BTC mean {m['btc_worst2pct_btc_mean']:+.1%}): "
        f"PAXG mean {m['btc_worst2pct_paxg_mean']:+.2%}, up {m['btc_worst2pct_paxg_up']:.0%} of them")
    log(f"sleeve's worst 20 days (sleeve mean {m['sleeve_worst20_sleeve_mean']:+.2%}): "
        f"PAXG mean {m['sleeve_worst20_paxg_mean']:+.2%}, up {m['sleeve_worst20_paxg_up']:.0%} of them")
    log()
    log("### by calendar year")
    yr = years.reset_index()
    log(md(yr, [("year", "year", "{}"), ("PAXG", "PAXG", "{:+.1%}"), ("PAXG_vol", "PAXG vol", "{:.0%}"),
                ("PAXG_maxdd", "PAXG max DD", "{:.1%}"), ("BTC", "BTC", "{:+.1%}"), ("ETH", "ETH", "{:+.1%}")]))
    log()


def windows_section(closes: dict[str, pd.Series], sleeve: pd.DataFrame, mode: str) -> pd.DataFrame:
    rows = []
    idx = sleeve.index
    V_sl = 100 * (1 + sleeve["ret"]).cumprod()
    for w in WINDOWS:
        row = {"window": w.id, "label": w.label, "regime": w.regime, "days": w.days}
        for sym, key in (("PAXGUSDT", "paxg"), ("BTCUSDT", "btc"), ("ETHUSDT", "eth")):
            V = closes[sym] / closes[sym].iloc[0] * 100
            ret, dd = window_stats(V, w)
            row[f"{key}_ret"], row[f"{key}_dd"] = ret, dd
        ret, dd = window_stats(V_sl, w)
        row["sleeve_ret"], row["sleeve_dd"] = ret, dd
        mask = sleeve["window"] == w.id
        row["sleeve_flat_days"] = float(sleeve.loc[mask, "flat"].mean())
        rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "paxg_windows.csv", index=False)
    log(f"## Inside the twelve regime windows (sleeve = {CAUTION} x {mode} of the two families)")
    log(md(df, [("window", "window", "{}"), ("regime", "regime", "{}"), ("days", "days", "{}"),
                ("sleeve_ret", "sleeve", "{:+.1%}"), ("sleeve_dd", "sleeve DD", "{:.1%}"),
                ("sleeve_flat_days", "sleeve flat days", "{:.0%}"),
                ("paxg_ret", "PAXG", "{:+.1%}"), ("paxg_dd", "PAXG DD", "{:.1%}"),
                ("btc_ret", "BTC", "{:+.1%}"), ("btc_dd", "BTC DD", "{:.1%}"), ("eth_ret", "ETH", "{:+.1%}")]))
    bear = df[df["sleeve_ret"] <= 0.005]
    log(f"windows where the sleeve made <= +0.5%: {len(bear)}; PAXG mean there {bear['paxg_ret'].mean():+.1%}, "
        f"positive in {(bear['paxg_ret'] > 0).sum()} of {len(bear)}; BTC mean there {bear['btc_ret'].mean():+.1%}")
    log()
    return df


def sleeve_check_section(sleeve: pd.DataFrame, caution: float) -> None:
    rows = []
    r67 = {f: pd.read_csv(SCALING / f"{f}_base_r67.csv").set_index("window") for f in ("mom", "turtle")}
    base_files = {"mom": "xs_momentum_regime_exit_4h", "turtle": "turtle_regime_risk1_4h"}
    base = {}
    for f, name in base_files.items():
        a = pd.read_csv(RESULTS_DIR / "alt" / f"{name}.csv")
        b = pd.read_csv(RESULTS_DIR / "alt" / f"{name}_oos.csv")
        base[f] = pd.concat([a, b]).set_index("window")
    for w in WINDOWS:
        mom = sleeve.loc[sleeve["window"] == w.id, "mom"]
        tur = sleeve.loc[sleeve["window"] == w.id, "turtle"]
        avg = float(np.prod(1 + caution * 0.5 * (mom + tur)) - 1) * 100
        sm = float(np.prod(1 + caution * (mom + tur)) - 1) * 100
        rm, rt = r67["mom"].loc[w.id], r67["turtle"].loc[w.id]
        rows.append({
            "window": w.id, "regime": w.regime,
            "mom_base": base["mom"].loc[w.id, "net_pct"], "turtle_base": base["turtle"].loc[w.id, "net_pct"],
            "mom_unc": float(np.prod(1 + mom) - 1) * 100, "turtle_unc": float(np.prod(1 + tur) - 1) * 100,
            "mom_r67": rm["net_pct"], "turtle_r67": rt["net_pct"],
            "approx_avg": avg, "r67_avg": (rm["net_pct"] + rt["net_pct"]) / 2,
            "approx_sum": sm, "r67_sum": rm["net_pct"] + rt["net_pct"],
            "dep_mom": rm["inv_mean"] / rm["eq_mean"] if rm["eq_mean"] else 0.0,
            "dep_turtle": rt["inv_mean"] / rt["eq_mean"] if rt["eq_mean"] else 0.0,
            "dd_r67_mom": rm["max_dd_pct"], "dd_r67_turtle": rt["max_dd_pct"],
        })
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "sleeve_check.csv", index=False)
    log("## Trend sleeve approximation check (net % per window)")
    log("base = published censored result at full risk; r67 = the scaling study's exact 2/3-risk run (censored);")
    log("approx = this study's daily-return scaling of the uncensored curves; dep = average invested / average equity at 2/3 risk")
    log(md(df, [("window", "window", "{}"), ("mom_base", "mom base", "{:+.1f}"), ("mom_r67", "mom r67", "{:+.1f}"),
                ("turtle_base", "turtle base", "{:+.1f}"), ("turtle_r67", "turtle r67", "{:+.1f}"),
                ("approx_avg", "approx avg", "{:+.1f}"), ("r67_avg", "r67 avg", "{:+.1f}"),
                ("approx_sum", "approx sum", "{:+.1f}"), ("r67_sum", "r67 sum", "{:+.1f}"),
                ("dep_mom", "dep mom", "{:.0%}"), ("dep_turtle", "dep turtle", "{:.0%}")]))
    s = df[["approx_avg", "r67_avg", "approx_sum", "r67_sum"]].mean()
    log(f"12-window mean: approx avg {s['approx_avg']:+.2f} vs r67 avg {s['r67_avg']:+.2f}; "
        f"approx sum {s['approx_sum']:+.2f} vs r67 sum {s['r67_sum']:+.2f}")
    bull = df[df["regime"] == "bull"]
    log(f"deployment at 2/3 risk - bull windows: mom {bull['dep_mom'].mean():.0%}, turtle {bull['dep_turtle'].mean():.0%}; "
        f"all windows: mom {df['dep_mom'].mean():.0%}, turtle {df['dep_turtle'].mean():.0%}; "
        f"max per window: mom {df['dep_mom'].max():.0%}, turtle {df['dep_turtle'].max():.0%}")
    log(f"share of span days inside a window: {sleeve['in_window'].mean():.0%}; sleeve flat on {sleeve['flat'].mean():.0%} of all days "
        f"and on {sleeve.loc[sleeve['in_window'], 'flat'].mean():.0%} of window days")
    log()


def deployment_section(caution: float) -> None:
    """How much of the sleeve's USDT is actually in the market - from the published trade lists.

    Deployed notional (at cost) of the open trades per 4h bar, divided by the curve's equity,
    scaled by the caution factor (position size is linear in risk). avg = one book split 50/50
    between the families; sum = one book running both families at full risk.
    """
    rows = []
    for w in WINDOWS:
        dep = {}
        for fam, name in FAMILIES.items():
            c = pd.read_csv(curve_path(fam, w))
            times, eq = c["open_time"].to_numpy(), c["equity"].to_numpy()
            notional = np.zeros(len(times))
            tag = "_oos" if w.id >= "w07" else ""
            p = RESULTS_DIR / "alt" / "trades" / f"{name}_{w.id}{tag}_unc.csv"
            if p.exists():
                for _, tr in pd.read_csv(p).iterrows():
                    notional[(times >= tr["entry_time"]) & (times < tr["exit_time"])] += tr["notional"]
            dep[fam] = caution * notional / eq
        avg, sm = 0.5 * (dep["mom"] + dep["turtle"]), dep["mom"] + dep["turtle"]
        rows.append({
            "window": w.id, "regime": w.regime,
            "mom_mean": dep["mom"].mean(), "mom_peak": dep["mom"].max(),
            "turtle_mean": dep["turtle"].mean(), "turtle_peak": dep["turtle"].max(),
            "avg_mean": avg.mean(), "avg_p95": float(np.percentile(avg, 95)), "avg_peak": avg.max(),
            "sum_mean": sm.mean(), "sum_p95": float(np.percentile(sm, 95)), "sum_peak": sm.max(),
            "bars_in_market": float((sm > 0).mean()),
        })
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "deployment.csv", index=False)
    log(f"## Deployment of the sleeve's USDT at caution {caution} (notional at cost / equity, per 4h bar)")
    log(md(df, [("window", "window", "{}"), ("regime", "regime", "{}"),
                ("mom_mean", "mom mean", "{:.0%}"), ("mom_peak", "mom peak", "{:.0%}"),
                ("turtle_mean", "turtle mean", "{:.0%}"), ("turtle_peak", "turtle peak", "{:.0%}"),
                ("avg_mean", "avg mean", "{:.0%}"), ("avg_p95", "avg p95", "{:.0%}"), ("avg_peak", "avg peak", "{:.0%}"),
                ("sum_mean", "sum mean", "{:.0%}"), ("sum_p95", "sum p95", "{:.0%}"), ("sum_peak", "sum peak", "{:.0%}"),
                ("bars_in_market", "bars with a position", "{:.0%}")]))
    bull = df[df["regime"] == "bull"]
    log(f"all windows: avg-mode mean {df['avg_mean'].mean():.0%}, peak {df['avg_peak'].max():.0%}; "
        f"sum-mode mean {df['sum_mean'].mean():.0%}, peak {df['sum_peak'].max():.0%}; "
        f"bull windows: avg mean {bull['avg_mean'].mean():.0%}, sum mean {bull['sum_mean'].mean():.0%}")
    log()


GRID_COLS = [
    ("name", "mix", "{}"), ("apy", "USDT yield", "{:.0%}"), ("cagr", "CAGR", "{:+.1%}"), ("vol", "vol", "{:.1%}"),
    ("sharpe", "Sharpe", "{:.2f}"), ("maxdd", "max DD", "{:.1%}"), ("calmar", "Calmar", "{:.2f}"),
    ("worst_win", "worst window", "{:+.1%}"), ("worst_win_id", "which", "{}"), ("win_pos", "windows +", "{}"),
    ("win_mean", "mean/window", "{:+.1%}"), ("roll90_pos", "90d +", "{:.0%}"), ("roll365_pos", "365d +", "{:.0%}"),
    ("worst_90d", "worst 90d", "{:+.1%}"), ("fees", "fees", "{:.2f}"),
]


def mix_name(w: dict[str, float]) -> str:
    return "/".join(f"{k}{int(round(100 * w.get(k, 0.0)))}" for k in KEYS if w.get(k, 0.0) > 0)


def grid_section(sleeve: pd.DataFrame, rets: dict[str, np.ndarray]) -> pd.DataFrame:
    mixes: list[dict[str, float]] = []
    for T in GRID_T:
        for G in GRID_G:
            for B in GRID_B:
                C = 100 - T - G - B
                if C < 0:
                    continue
                mixes.append({"T": T / 100, "G": G / 100, "B": B / 100, "C": C / 100})
    refs = [{"G": 1.0}, {"B": 1.0}, {"C": 1.0}, {"G": 0.5, "C": 0.5}, {"G": 0.6, "B": 0.4}]
    rows = []
    win_rows = []
    for apy in GRID_Y:
        for w in mixes + refs:
            V, info = simulate(w, apy, sleeve, rets)
            m = metrics(V, sleeve)
            rows.append({"name": mix_name(w), **{k: w.get(k, 0.0) for k in KEYS}, "apy": apy, **m, **info})
            if apy == BASE_APY:
                wr = {"name": mix_name(w)}
                for win in WINDOWS:
                    wr[win.id] = window_stats(V, win)[0]
                win_rows.append(wr)
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "grid.csv", index=False)
    gw = pd.DataFrame(win_rows)
    gw.to_csv(OUT_DIR / "grid_windows.csv", index=False)

    log("## Allocation grid, monthly rebalance to target, 2021-01-01 -> 2026-09-01 (T = bot sleeve, G = PAXG, B = BTC, C = USDT yield)")
    for apy in GRID_Y:
        sub = df[(df["apy"] == apy) & (df["T"] > 0)].sort_values("sharpe", ascending=False)
        log(f"### USDT yield {apy:.0%}, sorted by Sharpe")
        log(md(sub, GRID_COLS))
        log()
    ref = df[df["T"] == 0].sort_values(["name", "apy"])
    log("### reference books without the bot")
    log(md(ref, GRID_COLS))
    log()
    log(f"### per-window returns (yield {BASE_APY:.0%})")
    cols = [("name", "mix", "{}")] + [(w.id, w.id[:3], "{:+.1%}") for w in WINDOWS]
    log(md(gw, cols))
    log()
    return df


def idle_section(sleeve: pd.DataFrame, rets: dict[str, np.ndarray]) -> None:
    rows = []
    cases = [
        ("bot only (T100)", {"T": 1.0}, BASE_APY),
        ("T45 + C55 @0%", {"T": 0.45, "C": 0.55}, 0.0),
        ("T45 + C55 @3%", {"T": 0.45, "C": 0.55}, BASE_APY),
        ("T45 + C55 @5%", {"T": 0.45, "C": 0.55}, 0.05),
        ("T45 + G55", {"T": 0.45, "G": 0.55}, BASE_APY),
        ("T45 + B55", {"T": 0.45, "B": 0.55}, BASE_APY),
        ("T45 + G30 + C25 @3%", {"T": 0.45, "G": 0.30, "C": 0.25}, BASE_APY),
        ("T45 + G25 + B10 + C20 @3%", {"T": 0.45, "G": 0.25, "B": 0.10, "C": 0.20}, BASE_APY),
    ]
    for name, w, apy in cases:
        V, info = simulate(w, apy, sleeve, rets)
        rows.append({"name": name, **metrics(V, sleeve), **info, "min_usdt_share": float("nan")})
    overlays = [
        ("overlay: bot on 100%, 55% held in PAXG", 0.55, 0.0),
        ("overlay: bot on 100%, 30% held in PAXG", 0.30, 0.0),
        ("overlay: bot on 100%, 20% held in PAXG", 0.20, 0.0),
        ("overlay: bot on 100%, 55% held in BTC", 0.0, 0.55),
        ("overlay: bot on 100%, 20% PAXG + 10% BTC", 0.20, 0.10),
    ]
    for name, gw, bw in overlays:
        V, info = simulate_overlay(gw, bw, BASE_APY, sleeve, rets)
        rows.append({"name": name, **metrics(V, sleeve), "fees": info["fees"], "rebalances": float("nan"),
                     "min_usdt_share": info["min_usdt_share"]})
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "idle_cash.csv", index=False)
    log("## Question 4: the ~55% the bot never deploys - gold vs cash vs BTC (monthly rebalance)")
    log("T45 rows: the bot's capital base is 45% of the account (its P&L scales down with it).")
    log("overlay rows: the bot keeps sizing on the whole account; the stated share physically sits in PAXG/BTC.")
    log("min USDT = lowest share of the account that was free USDT along the path (must stay above the bot's need).")
    log(md(df, GRID_COLS[:1] + GRID_COLS[2:] + [("min_usdt_share", "min USDT", "{:.0%}")]))
    log()


def recommended_section(sleeve: pd.DataFrame, rets: dict[str, np.ndarray], rec: dict[str, float], apy: float) -> None:
    variants = [
        ("bot only", {"T": 1.0}, apy, "monthly", 0.05, True),
        ("bot only, T earns no yield", {"T": 1.0}, apy, "monthly", 0.05, False),
        ("recommended, monthly", rec, apy, "monthly", 0.05, True),
        ("recommended, monthly check, trade if off > 5pp", rec, apy, "monthly_band", 0.05, True),
        ("recommended, any day off > 5pp", rec, apy, "daily_band", 0.05, True),
        ("recommended, quarterly", rec, apy, "quarterly", 0.05, True),
        ("recommended, never rebalanced", rec, apy, "never", 0.05, True),
        ("recommended, yield 0%", rec, 0.0, "monthly_band", 0.05, True),
        ("recommended, yield 5%", rec, 0.05, "monthly_band", 0.05, True),
        ("recommended, T earns no yield", rec, apy, "monthly_band", 0.05, False),
    ]
    rows = []
    series = {}
    for name, w, y, trig, band, ty in variants:
        V, info = simulate(w, y, sleeve, rets, trigger=trig, band=band, t_yield=ty)
        rows.append({"name": name, "apy": y, **metrics(V, sleeve), **info})
        series[name] = V
    df = pd.DataFrame(rows)
    log(f"## Recommended mix {mix_name(rec)} vs bot only - rebalancing rules and yield sensitivity")
    log(md(df, GRID_COLS + [("rebalances", "rebalances", "{:.0f}"), ("sharpe_win", "Sharpe (windows)", "{:.2f}"),
                            ("win_compound", "12-window compound", "{:+.1%}")]))
    log()
    bot, mix = series["bot only"], series["recommended, monthly check, trade if off > 5pp"]
    V_g = 100 * (1 + pd.Series(rets["G"], index=sleeve.index)).cumprod()
    V_b = 100 * (1 + pd.Series(rets["B"], index=sleeve.index)).cumprod()
    win_rows = []
    for w in WINDOWS:
        br, bd = window_stats(bot, w)
        mr, mdd = window_stats(mix, w)
        gr, _ = window_stats(V_g, w)
        btr, _ = window_stats(V_b, w)
        win_rows.append({"window": w.id, "regime": w.regime, "days": w.days, "bot_ret": br, "bot_dd": bd,
                         "mix_ret": mr, "mix_dd": mdd, "diff": mr - br, "paxg_ret": gr, "btc_ret": btr})
    wdf = pd.DataFrame(win_rows)
    years = pd.DataFrame({"bot": year_returns(bot), "mix": year_returns(mix), "PAXG": year_returns(V_g), "BTC": year_returns(V_b)})
    years.index.name = "year"
    wdf.to_csv(OUT_DIR / "recommended_windows.csv", index=False)
    years.to_csv(OUT_DIR / "recommended_years.csv")
    log("### per window: recommended (monthly check, 5pp band) vs bot only")
    log(md(wdf, [("window", "window", "{}"), ("regime", "regime", "{}"), ("days", "days", "{}"),
                 ("bot_ret", "bot only", "{:+.1%}"), ("bot_dd", "bot DD", "{:.1%}"),
                 ("mix_ret", "recommended", "{:+.1%}"), ("mix_dd", "mix DD", "{:.1%}"), ("diff", "diff", "{:+.1%}"),
                 ("paxg_ret", "PAXG", "{:+.1%}"), ("btc_ret", "BTC", "{:+.1%}")]))
    log(f"windows positive: bot {(wdf['bot_ret'] > 0).sum()}/12, mix {(wdf['mix_ret'] > 0).sum()}/12; "
        f"mean per window: bot {wdf['bot_ret'].mean():+.1%}, mix {wdf['mix_ret'].mean():+.1%}; "
        f"worst: bot {wdf['bot_ret'].min():+.1%}, mix {wdf['mix_ret'].min():+.1%}")
    log()
    log("### per calendar year")
    log(md(years.reset_index(), [("year", "year", "{}"), ("bot", "bot only", "{:+.1%}"), ("mix", "recommended", "{:+.1%}"),
                                 ("PAXG", "PAXG", "{:+.1%}"), ("BTC", "BTC", "{:+.1%}")]))
    log()


# ----------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--caution", type=float, default=CAUTION)
    parser.add_argument("--mode", choices=["avg", "sum"], default="avg")
    parser.add_argument("--rec", default="T70,G20,C10", help="recommended mix, e.g. T70,G20,C10")
    args = parser.parse_args()
    global OUT_DIR
    if args.mode != "avg":
        OUT_DIR = OUT_DIR / args.mode
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    closes = {s: close_series(s) for s in ("PAXGUSDT", "BTCUSDT", "ETHUSDT")}
    sleeve = build_sleeve(args.caution, args.mode)
    idx = sleeve.index
    rets = {}
    for key, sym in (("G", "PAXGUSDT"), ("B", "BTCUSDT")):
        r = closes[sym].pct_change().reindex(idx)
        missing = int(r.isna().sum())
        if missing:
            log(f"note: {sym} has {missing} missing days in the span, filled with 0")
        rets[key] = r.fillna(0.0).to_numpy()

    log(f"# Allocation study - caution {args.caution}, sleeve mode {args.mode}, span {SPAN_START.date()} -> {SPAN_END.date()}")
    log()
    paxg_section(closes, sleeve)
    windows_section(closes, sleeve, args.mode)
    sleeve_check_section(sleeve, args.caution)
    deployment_section(args.caution)
    grid_section(sleeve, rets)
    idle_section(sleeve, rets)
    rec = {p[0]: int(p[1:]) / 100 for p in args.rec.split(",")}
    recommended_section(sleeve, rets, rec, BASE_APY)
    (OUT_DIR / "study.log").write_text("\n".join(_log_lines), encoding="utf-8")


if __name__ == "__main__":
    main()
