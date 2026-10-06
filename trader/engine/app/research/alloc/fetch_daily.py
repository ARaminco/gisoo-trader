"""Daily candles for the allocation study.

Fetches 1d klines from 2020-01-01 to the last completed UTC day for PAXGUSDT,
BTCUSDT, ETHUSDT and the twelve-symbol research universe, and caches them as
gzipped CSV under RESEARCH_DIR/data/daily/<SYMBOL>_1d.csv.gz - independent of
the regime windows. A re-run only appends the missing tail.

    python -m app.research.alloc.fetch_daily [--refresh] [--symbols PAXGUSDT,BTCUSDT]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

import pandas as pd

from app.exchange.binance_client import BinanceClient
from app.research.data import DATA_DIR, fetch_range
from app.research.windows import SYMBOLS

DAILY_DIR = DATA_DIR / "daily"
START_DATE = "2020-01-01"
EXTRA_SYMBOLS = ["PAXGUSDT", "BTCUSDT", "ETHUSDT"]
DAY_MS = 86_400_000


def daily_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol}_1d.csv.gz"


def _utc_ms(date: str) -> int:
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def last_completed_day_ms() -> int:
    """Open time of today's (still forming) daily candle; every candle before it is closed."""
    now = dt.datetime.now(dt.timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def load_daily(symbol: str) -> pd.DataFrame | None:
    path = daily_path(symbol)
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
    return df


async def fetch_symbol(client: BinanceClient, symbol: str, refresh: bool = False) -> pd.DataFrame:
    path = daily_path(symbol)
    end_ms = last_completed_day_ms()
    cached: pd.DataFrame | None = None
    if not refresh and path.exists():
        cached = pd.read_csv(path)
        if cached.empty:
            cached = None
    start_ms = int(cached["open_time"].max()) + DAY_MS if cached is not None else _utc_ms(START_DATE)
    if start_ms >= end_ms:
        return cached if cached is not None else pd.DataFrame()
    fresh = await fetch_range(client, symbol, "1d", start_ms, end_ms)
    fresh = fresh[fresh["open_time"] < end_ms]
    df = pd.concat([cached, fresh]) if cached is not None else fresh
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")
    return df


async def main(symbols: list[str], refresh: bool) -> None:
    async with BinanceClient(api_key="", api_secret="", testnet=False) as client:
        for symbol in symbols:
            try:
                df = await fetch_symbol(client, symbol, refresh)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                print(f"[daily] {symbol}: FAILED {exc}", flush=True)
                continue
            if df.empty:
                print(f"[daily] {symbol}: no data (unlisted?)", flush=True)
                continue
            first = pd.to_datetime(df["open_time"].min(), unit="ms").date()
            last = pd.to_datetime(df["open_time"].max(), unit="ms").date()
            print(f"[daily] {symbol}: {len(df)} candles {first} -> {last}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-download instead of appending")
    parser.add_argument("--symbols", default="", help="comma separated subset")
    args = parser.parse_args()
    wanted = [s for s in args.symbols.split(",") if s] or EXTRA_SYMBOLS + [s for s in SYMBOLS if s not in EXTRA_SYMBOLS]
    asyncio.run(main(wanted, args.refresh))
