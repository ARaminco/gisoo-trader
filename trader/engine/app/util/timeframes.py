"""Timeframe helpers shared by data ingest, strategies and backtests."""
from __future__ import annotations

TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
}


def tf_seconds(tf: str) -> int:
    try:
        return TF_SECONDS[tf]
    except KeyError as exc:  # pragma: no cover - configuration error
        raise ValueError(f"unsupported timeframe: {tf}") from exc


def tf_minutes(tf: str) -> float:
    return tf_seconds(tf) / 60.0


def bars_between(start_ms: int, end_ms: int, tf: str) -> int:
    return max(0, int((end_ms - start_ms) / (tf_seconds(tf) * 1000)))
