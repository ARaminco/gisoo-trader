"""Feature loading, higher-timeframe alignment and decision-frame assembly.

Everything reuses `app.features.build.build_features` (EMA/RSI/ATR/ADX/
Donchian/Bollinger/VWAP/swings). The only additions are a few indicators the
candidates need that the shared frame does not carry (EMA20, RSI(2), ATR(20),
55-bar Donchian, 100-bar Bollinger-width rank) - those live in generators.py.

Features are cached in memory per (window, symbol, timeframe): several
candidates share the same 4h context and the runner evaluates them in one
process.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.features.build import build_features
from app.indicators import core as ta
from app.research.data import load_cached
from app.research.windows import Window
from app.util.timeframes import tf_seconds

HTF = "4h"
MIN_ROWS = 260

_CACHE: dict[tuple[str, str, str], pd.DataFrame | None] = {}

# Decision-frame schema consumed by app.research.portfolio.simulate plus the
# three optional hook columns understood by app.research.alt.sim.simulate_ext.
FRAME_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "side", "score", "regime", "atr", "atr_ratio", "adx", "htf_bias", "in_window",
    "stop_dist", "target_dist", "exit",
]


def features(window: Window, symbol: str, tf: str) -> pd.DataFrame | None:
    """build_features() over the cached candles (45 days of warmup included)."""
    key = (window.id, symbol, tf)
    if key not in _CACHE:
        raw = load_cached(window, symbol, tf)
        if raw is None or len(raw) < MIN_ROWS:
            _CACHE[key] = None
        else:
            f = build_features(raw)
            f["ema_20"] = ta.ema(f["close"], 20)
            _CACHE[key] = f
    return _CACHE[key]


def align_index(base_open: np.ndarray, other_open: np.ndarray, other_tf: str, same_tf: bool) -> np.ndarray:
    """Index into `other` of the last candle usable at each base bar (-1 if none).

    Same timeframe: the candle with the same open time - it has closed when
    the base bar closes, which is when the decision is taken.
    Different timeframe: the last candle that CLOSED before the base bar
    opened - the same conservative no-look-ahead rule as research.decisions.
    """
    if same_tf:
        return np.searchsorted(other_open, base_open, side="right") - 1
    other_close = other_open + tf_seconds(other_tf) * 1000
    return np.searchsorted(other_close, base_open, side="right") - 1


def take(other: pd.DataFrame, col: str, idx: np.ndarray, index: pd.Index) -> pd.Series:
    values = other[col].to_numpy(dtype=float)
    out = np.where(idx >= 0, values[np.clip(idx, 0, len(values) - 1)], np.nan)
    return pd.Series(out, index=index)


def context(
    window: Window, symbol: str, base: pd.DataFrame, base_tf: str,
    ctx_symbol: str | None = None, ctx_tf: str = HTF,
) -> pd.DataFrame | None:
    """Context candle features aligned to the base bars.

    Columns: close, ema20, ema50, ema200, adx, rsi, atr, ema200_rising (1/0,
    EMA200 above its value 6 context bars = 24h earlier on 4h), ema50_rising,
    bias (-1..1: half from close vs EMA200, half from EMA20 vs EMA50).
    """
    other = features(window, ctx_symbol or symbol, ctx_tf)
    if other is None:
        return None
    other = other.assign(
        ema200_rising=(other["ema_200"] > other["ema_200"].shift(6)).astype(float),
        ema50_rising=(other["ema_50"] > other["ema_50"].shift(6)).astype(float),
    )
    idx = align_index(
        base["open_time"].to_numpy(), other["open_time"].to_numpy(), ctx_tf, same_tf=(base_tf == ctx_tf)
    )
    cols = {
        "close": "close", "ema20": "ema_20", "ema50": "ema_50", "ema200": "ema_200",
        "adx": "adx", "rsi": "rsi", "atr": "atr",
        "ema200_rising": "ema200_rising", "ema50_rising": "ema50_rising",
    }
    ctx = pd.DataFrame({name: take(other, src, idx, base.index) for name, src in cols.items()})
    bias = 0.5 * np.sign(ctx["close"] - ctx["ema200"]) + 0.5 * np.sign(ctx["ema20"] - ctx["ema50"])
    ctx["bias"] = bias.fillna(0.0)
    return ctx


def regime_column(f: pd.DataFrame) -> np.ndarray:
    """Vectorised app.features.build.classify_regime (same thresholds, same priority)."""
    adx = f["adx"].fillna(0.0).to_numpy()
    ratio = f["atr_ratio"].fillna(1.0).to_numpy()
    di = f["di_spread"].fillna(0.0).to_numpy()
    out = np.full(len(f), "transition", dtype=object)
    out[adx < 18] = "range"
    out[(adx >= 25) & (di < 0)] = "trend_down"
    out[(adx >= 25) & (di > 0)] = "trend_up"
    out[ratio > 2.0] = "volatile"
    return out


def _series(values, index: pd.Index, default: float) -> pd.Series:
    if values is None:
        return pd.Series(default, index=index, dtype=float)
    return pd.Series(np.asarray(values, dtype=float), index=index).fillna(default)


def assemble(
    window: Window,
    f: pd.DataFrame,
    side,
    score,
    htf_bias,
    stop_dist=None,
    target_dist=None,
    exit_signal=None,
) -> pd.DataFrame:
    """Decision frame for a research window (`in_window` from the window start)."""
    return build_frame(f, side, score, htf_bias, window.start_ms, stop_dist, target_dist, exit_signal)


def build_frame(
    f: pd.DataFrame,
    side,
    score,
    htf_bias,
    start_ms: int,
    stop_dist=None,
    target_dist=None,
    exit_signal=None,
) -> pd.DataFrame:
    """Turn per-bar signal arrays into the decision-frame schema.

    Rows without a valid ATR are forced flat; score is clipped to 0..1 and
    zeroed on flat bars. stop_dist / target_dist are absolute price distances
    (0 = use the SimParams ATR multiple); exit is 1 on bars where an open
    position must be closed at the close. `in_window` marks bars from
    `start_ms` on - the live backtest uses it the same way a research window
    does.
    """
    index = f.index
    side_s = pd.Series(np.asarray(side, dtype=object), index=index).fillna("flat").astype(str)
    score_s = pd.Series(np.asarray(score, dtype=float), index=index).clip(0.0, 1.0)
    atr = f["atr"].fillna(0.0).astype(float)
    valid = (atr > 0) & score_s.notna()
    side_s = side_s.where(valid, "flat")
    score_s = score_s.fillna(0.0).where(side_s != "flat", 0.0)

    exit_s = pd.Series(0.0, index=index) if exit_signal is None else (
        pd.Series(np.asarray(exit_signal), index=index).fillna(False).astype(bool).astype(float)
    )
    out = pd.DataFrame({
        "open_time": f["open_time"].astype("int64"),
        "open": f["open"].astype(float),
        "high": f["high"].astype(float),
        "low": f["low"].astype(float),
        "close": f["close"].astype(float),
        "volume": f["volume"].astype(float),
        "side": side_s,
        "score": score_s,
        "regime": regime_column(f),
        "atr": atr,
        "atr_ratio": f["atr_ratio"].fillna(1.0).astype(float),
        "adx": f["adx"].fillna(0.0).astype(float),
        "htf_bias": _series(htf_bias, index, 0.0),
        "in_window": f["open_time"] >= start_ms,
        "stop_dist": _series(stop_dist, index, 0.0).where(side_s != "flat", 0.0),
        "target_dist": _series(target_dist, index, 0.0).where(side_s != "flat", 0.0),
        "exit": exit_s,
    }, columns=FRAME_COLUMNS)
    return out.reset_index(drop=True)
