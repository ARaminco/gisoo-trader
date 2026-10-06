"""Precompute the ensemble's per-bar decision for a (window, symbol, timeframe).

The expensive part of a backtest is running the ensemble on every bar. It is
deterministic given the candles, so it is done once and cached; the portfolio
simulator then applies risk gates and parameters on top, which makes parameter
sweeps cheap.

Higher-timeframe features are aligned without look-ahead: a 4h candle is only
visible to a 30m bar once the 4h candle has closed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from app.features.build import build_features
from app.research.data import RESULTS_DIR, load_cached
from app.research.windows import Window
from app.strategies.ensemble import Ensemble
from app.util.timeframes import tf_seconds

log = logging.getLogger(__name__)

WARMUP_BARS = 210
LOOKBACK_SLICE = 300  # strategies only read the last handful of rows

STRATEGY_COLUMNS = [
    "ema_trend", "rsi_reversion", "squeeze_breakout", "vwap_pullback",
    "momentum_burst", "structure", "htf_bias",
]

OUT_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "side", "score", "regime", "atr", "atr_pct", "atr_ratio", "adx", "rsi",
    "in_window", *STRATEGY_COLUMNS,
]


def decisions_path(window: Window, symbol: str, timeframe: str, htf: str) -> Path:
    return RESULTS_DIR / "decisions" / f"{timeframe}_{htf}" / window.id / f"{symbol}.csv.gz"


def _align_htf(base: pd.DataFrame, htf: pd.DataFrame, htf_tf: str) -> np.ndarray:
    """For each base bar, index of the last *closed* HTF candle (or -1)."""
    htf_close = htf["open_time"].to_numpy() + tf_seconds(htf_tf) * 1000
    base_open = base["open_time"].to_numpy()
    # HTF candle j is usable at base bar i if htf_close[j] <= base_open[i]
    idx = np.searchsorted(htf_close, base_open, side="right") - 1
    return idx


def compute_decisions(
    window: Window, symbol: str, timeframe: str, htf: str
) -> pd.DataFrame | None:
    base = load_cached(window, symbol, timeframe)
    htf_df = load_cached(window, symbol, htf)
    if base is None or len(base) < WARMUP_BARS + 50:
        return None

    features = build_features(base)
    htf_features = build_features(htf_df) if htf_df is not None and len(htf_df) >= 60 else None
    htf_idx = _align_htf(features, htf_features, htf) if htf_features is not None else None

    ensemble = Ensemble()
    records: list[dict] = []
    n = len(features)
    open_times = features["open_time"].to_numpy()

    for i in range(WARMUP_BARS, n):
        lo = max(0, i - LOOKBACK_SLICE)
        window_df = features.iloc[lo : i + 1]
        htf_slice = None
        if htf_features is not None and htf_idx[i] >= 2:
            j = int(htf_idx[i])
            htf_slice = htf_features.iloc[max(0, j - 5) : j + 1]

        decision = ensemble.evaluate(symbol, timeframe, window_df, htf_slice, None)
        row = features.iloc[i]
        contributions = decision.contributions or {}
        record = {
            "open_time": int(open_times[i]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "side": decision.side,
            "score": float(decision.score),
            "regime": decision.regime,
            "atr": float(decision.atr or 0.0),
            "atr_pct": float(row.get("atr_pct", 0.0) or 0.0),
            "atr_ratio": float(row.get("atr_ratio", 1.0) or 1.0),
            "adx": float(row.get("adx", 0.0) or 0.0),
            "rsi": float(row.get("rsi", 50.0) or 50.0),
            "in_window": bool(open_times[i] >= window.start_ms),
        }
        for name in STRATEGY_COLUMNS:
            record[name] = float(contributions.get(name, {}).get("score", 0.0) or 0.0)
        records.append(record)

    out = pd.DataFrame(records, columns=OUT_COLUMNS)
    return out


def load_or_compute(
    window: Window, symbol: str, timeframe: str, htf: str, force: bool = False
) -> pd.DataFrame | None:
    path = decisions_path(window, symbol, timeframe, htf)
    if path.exists() and not force:
        return pd.read_csv(path)
    df = compute_decisions(window, symbol, timeframe, htf)
    if df is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")
    return df
