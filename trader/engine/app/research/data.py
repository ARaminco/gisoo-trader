"""Historical candle download with an on-disk cache.

Public Binance endpoints only - no API key needed. Every (window, symbol,
timeframe) is cached as a gzipped CSV under RESEARCH_DIR/data so a re-run is
free and the analysts can open the files directly.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pandas as pd

from app.exchange.binance_client import BinanceClient, BinanceError
from app.research.windows import WARMUP_DAYS, Window
from app.util.timeframes import tf_seconds


def kline_to_row(symbol: str, timeframe: str, k: list) -> dict:
    """Map a REST kline array to a candle row.

    Duplicated from app.data.ingest on purpose: that module pulls in the
    database layer, and research runs must not depend on it.
    """
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "quote_volume": float(k[7]),
        "trades": int(k[8]),
        "taker_buy_base": float(k[9]),
    }

log = logging.getLogger(__name__)

RESEARCH_DIR = Path(os.getenv("RESEARCH_DIR", "/research"))
DATA_DIR = RESEARCH_DIR / "data"
RESULTS_DIR = RESEARCH_DIR / "results"

COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "quote_volume", "trades", "taker_buy_base",
]


def cache_path(window: Window, symbol: str, timeframe: str) -> Path:
    return DATA_DIR / window.id / f"{symbol}_{timeframe}.csv.gz"


async def fetch_range(
    client: BinanceClient, symbol: str, timeframe: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """Page forward through /klines until end_ms. Returns an empty frame if unlisted."""
    step_ms = tf_seconds(timeframe) * 1000
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            klines = await client.klines(
                symbol, timeframe, limit=1000, start_time=cursor, end_time=end_ms - 1
            )
        except BinanceError as exc:
            if exc.code == -1121:  # invalid symbol
                return pd.DataFrame(columns=COLUMNS)
            raise
        if not klines:
            break
        rows.extend(kline_to_row(symbol, timeframe, k) for k in klines)
        last_open = int(klines[-1][0])
        cursor = last_open + step_ms
        if len(klines) < 1000:
            break
        await asyncio.sleep(0.12)  # ~500 requests/min, well under the weight limit

    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows)[COLUMNS]
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    # drop the still-forming last candle if the range ends "now"
    return df


async def load_or_fetch(
    client: BinanceClient, window: Window, symbol: str, timeframe: str
) -> pd.DataFrame:
    path = cache_path(window, symbol, timeframe)
    if path.exists():
        df = pd.read_csv(path)
    else:
        start = window.start_ms - WARMUP_DAYS * 86_400_000
        df = await fetch_range(client, symbol, timeframe, start, window.end_ms)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, compression="gzip")
        log.info("%s %s %s: %s candles cached", window.id, symbol, timeframe, len(df))
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


async def fetch_all(
    windows: list[Window], symbols: list[str], timeframes: list[str]
) -> dict[tuple[str, str, str], int]:
    """Populate the cache. Returns candle counts per key."""
    counts: dict[tuple[str, str, str], int] = {}
    async with BinanceClient(api_key="", api_secret="", testnet=False) as client:
        for window in windows:
            for timeframe in timeframes:
                for symbol in symbols:
                    try:
                        df = await load_or_fetch(client, window, symbol, timeframe)
                        counts[(window.id, symbol, timeframe)] = len(df)
                    except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                        log.error("fetch failed %s %s %s: %s", window.id, symbol, timeframe, exc)
                        counts[(window.id, symbol, timeframe)] = -1
            done = sum(1 for k, v in counts.items() if k[0] == window.id and v >= 0)
            print(f"[fetch] {window.id}: {done} series cached", flush=True)
    return counts


def load_cached(window: Window, symbol: str, timeframe: str) -> pd.DataFrame | None:
    path = cache_path(window, symbol, timeframe)
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df
