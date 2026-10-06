"""Feature frame shared by the rule strategies, the ML model and the backtester.

One definition, used everywhere: if the live path and the training path ever
computed features differently, the model would be silently wrong.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators import core as ta

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

# Columns fed to the ML model. Order matters and is persisted with the model.
ML_FEATURES: list[str] = [
    "ret_1", "ret_3", "ret_6", "ret_12", "ret_24",
    "ema_fast_dist", "ema_slow_dist", "ema_spread",
    "rsi", "rsi_slope",
    "macd_hist_norm", "macd_cross",
    "stoch_k", "stoch_d",
    "bb_pos", "bb_width", "bb_width_pct",
    "atr_pct", "atr_ratio",
    "adx", "di_spread",
    "vol_z", "mfi", "obv_slope",
    "vwap_dist", "dc_pos",
    "st_dir", "body_ratio", "upper_wick", "lower_wick",
    "range_pct", "close_to_high", "close_to_low",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.replace(0.0, np.nan)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add every indicator and derived feature to an OHLCV frame.

    The input must be sorted oldest-first and contain `REQUIRED_COLUMNS`.
    Returns a new frame; the caller keeps the original.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing OHLCV columns: {missing}")
    if len(df) < 60:
        raise ValueError(f"need at least 60 candles, got {len(df)}")

    out = df.copy()
    o, h, l, c, v = out["open"], out["high"], out["low"], out["close"], out["volume"]

    # ---- returns ----
    for n in (1, 3, 6, 12, 24):
        out[f"ret_{n}"] = c.pct_change(n) * 100

    # ---- trend ----
    out["ema_9"] = ta.ema(c, 9)
    out["ema_21"] = ta.ema(c, 21)
    out["ema_50"] = ta.ema(c, 50)
    out["ema_200"] = ta.ema(c, 200) if len(out) >= 200 else ta.ema(c, min(len(out) - 1, 100))
    out["ema_fast_dist"] = _safe_div(c - out["ema_9"], c) * 100
    out["ema_slow_dist"] = _safe_div(c - out["ema_50"], c) * 100
    out["ema_spread"] = _safe_div(out["ema_9"] - out["ema_21"], c) * 100
    out["above_ema200"] = (c > out["ema_200"]).astype(int)

    st = ta.supertrend(h, l, c, period=10, mult=3.0)
    out["st_line"] = st["st_line"]
    out["st_dir"] = st["st_dir"]

    # ---- momentum ----
    out["rsi"] = ta.rsi(c, 14)
    out["rsi_slope"] = out["rsi"].diff(3)
    macd_df = ta.macd(c)
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["macd_signal"]
    out["macd_hist"] = macd_df["macd_hist"]
    out["macd_hist_norm"] = _safe_div(out["macd_hist"], c) * 100
    out["macd_cross"] = np.sign(out["macd_hist"]).diff().fillna(0.0)

    stoch = ta.stochastic(h, l, c)
    out["stoch_k"] = stoch["stoch_k"]
    out["stoch_d"] = stoch["stoch_d"]

    # ---- volatility ----
    out["atr"] = ta.atr(h, l, c, 14)
    out["atr_pct"] = _safe_div(out["atr"], c) * 100
    out["atr_ratio"] = _safe_div(out["atr"], out["atr"].rolling(50, min_periods=20).mean())

    bb = ta.bollinger(c, 20, 2.0)
    for col in bb.columns:
        out[col] = bb[col]
    out["bb_width_pct"] = out["bb_width"].rolling(50, min_periods=20).rank(pct=True)

    kc = ta.keltner(h, l, c, 20, 1.5)
    out["squeeze"] = ((out["bb_upper"] < kc["kc_upper"]) & (out["bb_lower"] > kc["kc_lower"])).astype(int)

    # ---- trend strength ----
    adx_df = ta.adx(h, l, c, 14)
    out["adx"] = adx_df["adx"]
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]
    out["di_spread"] = out["plus_di"] - out["minus_di"]

    # ---- volume ----
    out["vol_z"] = ta.volume_zscore(v, 30)
    out["mfi"] = ta.money_flow_index(h, l, c, v, 14)
    out["obv"] = ta.obv(c, v)
    out["obv_slope"] = out["obv"].diff(6) / out["obv"].rolling(30, min_periods=10).std(ddof=0).replace(0.0, np.nan)
    out["vwap"] = ta.vwap(h, l, c, v, 20)
    out["vwap_dist"] = _safe_div(c - out["vwap"], c) * 100

    # ---- structure ----
    dc = ta.donchian(h, l, 20)
    for col in dc.columns:
        out[col] = dc[col]
    out["dc_pos"] = _safe_div(c - out["dc_lower"], out["dc_upper"] - out["dc_lower"])
    swings = ta.swing_levels(h, l, 3, 3)
    out["swing_high"] = swings["swing_high"]
    out["swing_low"] = swings["swing_low"]

    # ---- candle anatomy ----
    candle_range = (h - l).replace(0.0, np.nan)
    out["body_ratio"] = (c - o).abs() / candle_range
    out["upper_wick"] = (h - np.maximum(o, c)) / candle_range
    out["lower_wick"] = (np.minimum(o, c) - l) / candle_range
    out["range_pct"] = _safe_div(h - l, c) * 100
    out["close_to_high"] = _safe_div(h - c, candle_range)
    out["close_to_low"] = _safe_div(c - l, candle_range)
    out["direction"] = np.sign(c - o)

    # ---- session seasonality ----
    if "timestamp" in out.columns:
        ts = pd.to_datetime(out["timestamp"], utc=True)
    else:
        ts = pd.to_datetime(out["open_time"], unit="ms", utc=True)
    hour = ts.dt.hour + ts.dt.minute / 60
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    dow = ts.dt.dayofweek
    out["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    out["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    return out.replace([np.inf, -np.inf], np.nan)


def classify_regime(row: pd.Series) -> str:
    """Coarse market regime used to route between trend and mean-reversion logic."""
    adx_value = float(row.get("adx", 0) or 0)
    atr_ratio = float(row.get("atr_ratio", 1) or 1)
    di_spread = float(row.get("di_spread", 0) or 0)

    if atr_ratio > 2.0:
        return "volatile"
    if adx_value >= 25 and di_spread > 0:
        return "trend_up"
    if adx_value >= 25 and di_spread < 0:
        return "trend_down"
    if adx_value < 18:
        return "range"
    return "transition"


def feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Just the ML columns, NaNs dropped. Index is preserved for alignment."""
    available = [c for c in ML_FEATURES if c in df.columns]
    return df[available].replace([np.inf, -np.inf], np.nan)


def latest_snapshot(df: pd.DataFrame) -> dict:
    """Compact, JSON-safe view of the last candle - used by the LLM and the panel."""
    if df.empty:
        return {}
    row = df.iloc[-1]
    keys = [
        "close", "rsi", "adx", "atr_pct", "bb_pos", "bb_width", "macd_hist_norm",
        "vol_z", "mfi", "vwap_dist", "dc_pos", "st_dir", "ret_12", "ret_24",
        "ema_spread", "di_spread", "squeeze", "above_ema200",
    ]
    snapshot: dict[str, float | str] = {}
    for key in keys:
        value = row.get(key)
        if value is None or (isinstance(value, float) and np.isnan(value)):
            continue
        snapshot[key] = round(float(value), 5)
    snapshot["regime"] = classify_regime(row)
    return snapshot
