"""Market data: REST backfill plus a combined websocket kline stream, or REST
polling on exchanges that have no kline websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.exchange.base import ExchangeClient
from app.util.timeframes import tf_seconds

log = logging.getLogger(__name__)

CandleCallback = Callable[[str, str, dict, bool], Awaitable[None]]


def kline_to_row(symbol: str, timeframe: str, k: list) -> dict:
    """Map a REST kline array to a candle row."""
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


def _closed_only(klines: list[list], now_ms: int | None = None) -> list[list]:
    """Drop the kline that is still forming (close time in the future).

    REST klines always end with the current, open candle. Storing it would
    leave a frozen partial bar as the last row of the higher-timeframe frame
    for up to one HTF period, and the HTF bias would be computed from it.
    """
    now_ms = now_ms or int(time.time() * 1000)
    return [k for k in klines if int(k[6]) < now_ms]


def ws_kline_to_row(symbol: str, timeframe: str, k: dict) -> dict:
    """Map a websocket kline payload to a candle row."""
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time": int(k["t"]),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "quote_volume": float(k["q"]),
        "trades": int(k["n"]),
        "taker_buy_base": float(k["V"]),
    }


async def backfill(
    client: ExchangeClient, symbol: str, timeframe: str, target: int
) -> int:
    """Fetch history until the table holds `target` candles for this pair."""
    async with session_scope() as session:
        newest = await repo.last_candle_time(session, symbol, timeframe)

    fetched = 0
    end_time: int | None = None
    remaining = target

    while remaining > 0:
        batch = min(remaining, 1000)
        klines = await client.klines(
            symbol, timeframe, limit=batch, end_time=end_time
        )
        if not klines:
            break

        rows = [kline_to_row(symbol, timeframe, k) for k in _closed_only(klines)]
        async with session_scope() as session:
            fetched += await repo.upsert_candles(session, rows, overwrite=False)

        oldest = int(klines[0][0])
        end_time = oldest - 1
        remaining -= len(klines)
        if len(klines) < batch:
            break
        await asyncio.sleep(0.25)  # stay well under the weight limit

    async with session_scope() as session:
        await repo.prune_candles(session, symbol, timeframe, settings.candle_history * 2)

    log.info("backfilled %s %s: %s candles (had up to %s)", symbol, timeframe, fetched, newest)
    return fetched


async def backfill_universe(
    client: ExchangeClient, symbols: list[str], timeframes: list[str], target: int
) -> None:
    for timeframe in timeframes:
        for symbol in symbols:
            try:
                await backfill(client, symbol, timeframe, target)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop startup
                log.error("backfill failed for %s %s: %s", symbol, timeframe, exc)


class KlineStream:
    """One combined websocket for every symbol/timeframe pair we follow."""

    def __init__(
        self,
        client: ExchangeClient,
        symbols: list[str],
        timeframes: list[str],
        on_candle: CandleCallback,
        market: str = "spot",
    ):
        self.client = client
        self.symbols = symbols
        self.timeframes = timeframes
        self.on_candle = on_candle
        self.market = market
        self._stop = asyncio.Event()
        self.connected = False

    def _streams(self) -> list[str]:
        return [
            f"{symbol.lower()}@kline_{tf}"
            for symbol in self.symbols
            for tf in self.timeframes
        ]

    async def run(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            streams = "/".join(self._streams())
            url = f"{self.client.ws_url(self.market)}?streams={streams}"
            try:
                log.info("kline stream connecting: %s streams", len(self._streams()))
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, max_queue=512
                ) as ws:
                    self.connected = True
                    backoff = 2
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - always reconnect
                self.connected = False
                log.warning("kline stream dropped: %s (retry in %ss)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self.connected = False

    async def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        payload = message.get("data", message)
        if payload.get("e") != "kline":
            return

        k = payload["k"]
        symbol = payload["s"]
        timeframe = k["i"]
        closed = bool(k["x"])
        row = ws_kline_to_row(symbol, timeframe, k)
        try:
            await self.on_candle(symbol, timeframe, row, closed)
        except Exception as exc:  # noqa: BLE001
            log.exception("candle handler failed for %s: %s", symbol, exc)

    def stop(self) -> None:
        self._stop.set()


class PollingKlineStream:
    """KlineStream's REST twin, for exchanges without a kline websocket.

    Every EXCHANGE_POLL_SECONDS it reads the last few candles of each pair.
    The forming candle's close becomes the live mark (the tick watchdog runs
    stops on it), and a candle is reported closed exactly once, the first
    poll after its close time plus CLOSE_GRACE_MS (exchanges cache candles
    for a few seconds, and the host clock may run slightly ahead).
    """

    #: a bar counts as closed only this long after its close time
    CLOSE_GRACE_MS = 30_000

    #: candles read per request: the forming one, the one that just closed,
    #: and one more in case a poll was skipped across a boundary
    WINDOW = 3

    def __init__(
        self,
        client: ExchangeClient,
        symbols: list[str],
        timeframes: list[str],
        on_candle: CandleCallback,
        market: str = "spot",
    ):
        self.client = client
        self.symbols = symbols
        self.timeframes = timeframes
        self.on_candle = on_candle
        self.market = market
        self.interval = max(5, settings.exchange_poll_seconds)
        self._stop = asyncio.Event()
        self._last_closed: dict[tuple[str, str], int] = {}
        self.connected = False

    async def run(self) -> None:
        pairs = [(s, tf) for tf in self.timeframes for s in self.symbols]
        # Spread the requests over the interval instead of bursting them.
        spacing = min(1.0, self.interval / max(1, len(pairs)))
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            ok = 0
            for symbol, timeframe in pairs:
                if self._stop.is_set():
                    break
                try:
                    await self._poll(symbol, timeframe)
                    ok += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad pair must not stop the rest
                    log.warning("poll failed for %s %s: %s", symbol, timeframe, exc)
                await asyncio.sleep(spacing)
            self.connected = ok > 0
            failures = 0 if ok else failures + 1
            # Back off while the exchange is unreachable, up to five minutes.
            wait = self.interval * (2 ** min(failures, 3)) if failures else self.interval
            remaining = min(wait, 300) - (time.monotonic() - started)
            if remaining > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
        self.connected = False

    async def _poll(self, symbol: str, timeframe: str) -> None:
        klines = await self.client.klines(symbol, timeframe, limit=self.WINDOW, market=self.market)
        if not klines:
            return
        now_ms = int(time.time() * 1000)
        key = (symbol, timeframe)
        closed = _closed_only(klines, now_ms - self.CLOSE_GRACE_MS)
        last_seen = self._last_closed.get(key)
        if last_seen is None and closed:
            # First poll after start: the backfill just stored these bars and
            # the trader catches up on any it has not evaluated; announcing
            # them again would only repeat work. Start counting from here.
            self._last_closed[key] = int(closed[-1][0])
            closed = []
        for k in closed:
            open_time = int(k[0])
            if last_seen is not None and open_time <= last_seen:
                continue
            await self._emit(symbol, timeframe, kline_to_row(symbol, timeframe, k), True)
            self._last_closed[key] = open_time
            last_seen = open_time
        forming = klines[-1]
        if int(forming[6]) >= now_ms - self.CLOSE_GRACE_MS:
            await self._emit(symbol, timeframe, kline_to_row(symbol, timeframe, forming), False)

    async def _emit(self, symbol: str, timeframe: str, row: dict, closed: bool) -> None:
        try:
            await self.on_candle(symbol, timeframe, row, closed)
        except Exception as exc:  # noqa: BLE001
            log.exception("candle handler failed for %s: %s", symbol, exc)

    def stop(self) -> None:
        self._stop.set()


async def gap_check(
    client: ExchangeClient, symbol: str, timeframe: str, max_missing: int = 5
) -> int:
    """Detect and repair holes left by a disconnect. Returns candles repaired."""
    async with session_scope() as session:
        newest = await repo.last_candle_time(session, symbol, timeframe)
    if newest is None:
        return 0

    now_ms = int(time.time() * 1000)
    step = tf_seconds(timeframe) * 1000
    missing = int((now_ms - newest) / step) - 1
    if missing <= 0:
        return 0
    if missing > max_missing:
        log.info("%s %s: %s candles missing, refetching", symbol, timeframe, missing)

    klines = await client.klines(
        symbol, timeframe, limit=min(missing + 2, 1000), start_time=newest
    )
    if not klines:
        return 0
    rows = [kline_to_row(symbol, timeframe, k) for k in _closed_only(klines, now_ms)]
    if not rows:
        return 0
    async with session_scope() as session:
        return await repo.upsert_candles(session, rows)
