"""Technical indicators, implemented on pandas/numpy.

Deliberately dependency-free (no TA-Lib): the C library is a recurring source of
Docker build failures, and these are the only indicators the system needs.
Every function takes and returns aligned pandas objects.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# moving averages
# --------------------------------------------------------------------------


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    return series.rolling(period).apply(
        lambda w: np.dot(w, weights) / weights.sum(), raw=True
    )


# --------------------------------------------------------------------------
# momentum
# --------------------------------------------------------------------------


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": macd_line - signal_line,
        }
    )


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k: int = 14, d: int = 3
) -> pd.DataFrame:
    lowest = low.rolling(k, min_periods=k).min()
    highest = high.rolling(k, min_periods=k).max()
    span = (highest - lowest).replace(0.0, np.nan)
    percent_k = 100 * (close - lowest) / span
    percent_k = percent_k.fillna(50.0)
    return pd.DataFrame(
        {"stoch_k": percent_k, "stoch_d": percent_k.rolling(d, min_periods=d).mean()}
    )


def roc(series: pd.Series, period: int = 10) -> pd.Series:
    return series.pct_change(period) * 100


# --------------------------------------------------------------------------
# volatility
# --------------------------------------------------------------------------


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(series: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid.replace(0.0, np.nan)
    position = (series - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width,
            "bb_pos": position.clip(-0.5, 1.5),
        }
    )


def keltner(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20, mult: float = 1.5
) -> pd.DataFrame:
    mid = ema(close, period)
    rng = atr(high, low, close, period)
    return pd.DataFrame({"kc_mid": mid, "kc_upper": mid + mult * rng, "kc_lower": mid - mult * rng})


# --------------------------------------------------------------------------
# trend strength
# --------------------------------------------------------------------------


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(high, low, close)
    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * (
        pd.Series(plus_dm, index=high.index).ewm(
            alpha=1 / period, adjust=False, min_periods=period
        ).mean()
        / atr_.replace(0.0, np.nan)
    )
    minus_di = 100 * (
        pd.Series(minus_dm, index=high.index).ewm(
            alpha=1 / period, adjust=False, min_periods=period
        ).mean()
        / atr_.replace(0.0, np.nan)
    )
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return pd.DataFrame(
        {"adx": adx_.fillna(0.0), "plus_di": plus_di.fillna(0.0), "minus_di": minus_di.fillna(0.0)}
    )


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, mult: float = 3.0
) -> pd.DataFrame:
    hl2 = (high + low) / 2
    rng = atr(high, low, close, period)
    upper = hl2 + mult * rng
    lower = hl2 - mult * rng

    direction = pd.Series(1, index=close.index, dtype=float)
    trend = pd.Series(np.nan, index=close.index, dtype=float)

    upper_v = upper.to_numpy()
    lower_v = lower.to_numpy()
    close_v = close.to_numpy()
    dir_v = np.ones(len(close_v))
    trend_v = np.full(len(close_v), np.nan)

    for i in range(1, len(close_v)):
        if np.isnan(upper_v[i]) or np.isnan(lower_v[i]):
            continue
        prev_trend = trend_v[i - 1] if not np.isnan(trend_v[i - 1]) else lower_v[i]
        if close_v[i] > prev_trend:
            dir_v[i] = 1
        elif close_v[i] < prev_trend:
            dir_v[i] = -1
        else:
            dir_v[i] = dir_v[i - 1]

        if dir_v[i] == 1:
            trend_v[i] = max(lower_v[i], prev_trend) if dir_v[i - 1] == 1 else lower_v[i]
        else:
            trend_v[i] = min(upper_v[i], prev_trend) if dir_v[i - 1] == -1 else upper_v[i]

    direction[:] = dir_v
    trend[:] = trend_v
    return pd.DataFrame({"st_line": trend, "st_dir": direction})


# --------------------------------------------------------------------------
# volume / price-volume
# --------------------------------------------------------------------------


def vwap(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 20
) -> pd.Series:
    typical = (high + low + close) / 3
    pv = (typical * volume).rolling(period, min_periods=period).sum()
    vol = volume.rolling(period, min_periods=period).sum().replace(0.0, np.nan)
    return pv / vol


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def volume_zscore(volume: pd.Series, period: int = 30) -> pd.Series:
    mean = volume.rolling(period, min_periods=period).mean()
    std = volume.rolling(period, min_periods=period).std(ddof=0).replace(0.0, np.nan)
    return ((volume - mean) / std).fillna(0.0)


def money_flow_index(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14
) -> pd.Series:
    typical = (high + low + close) / 3
    raw_flow = typical * volume
    delta = typical.diff()
    positive = raw_flow.where(delta > 0, 0.0).rolling(period, min_periods=period).sum()
    negative = raw_flow.where(delta < 0, 0.0).rolling(period, min_periods=period).sum()
    ratio = positive / negative.replace(0.0, np.nan)
    return (100 - 100 / (1 + ratio)).fillna(50.0)


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def swing_levels(
    high: pd.Series, low: pd.Series, left: int = 3, right: int = 3
) -> pd.DataFrame:
    """Confirmed pivot highs/lows, forward-filled so they act as S/R levels."""
    window = left + right + 1
    pivot_high = high.rolling(window, center=True).max() == high
    pivot_low = low.rolling(window, center=True).min() == low
    # a pivot is only known `right` bars later - shift to avoid look-ahead
    last_high = high.where(pivot_high).shift(right).ffill()
    last_low = low.where(pivot_low).shift(right).ffill()
    return pd.DataFrame({"swing_high": last_high, "swing_low": last_low})


def donchian(high: pd.Series, low: pd.Series, period: int = 20) -> pd.DataFrame:
    upper = high.rolling(period, min_periods=period).max()
    lower = low.rolling(period, min_periods=period).min()
    return pd.DataFrame(
        {"dc_upper": upper, "dc_lower": lower, "dc_mid": (upper + lower) / 2}
    )
