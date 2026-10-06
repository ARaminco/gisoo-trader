"""Market data worker.

Responsibilities:
  * backfill history on startup so strategies have enough candles immediately
  * keep a websocket kline stream open for every symbol/timeframe (or poll
    REST candles on exchanges without one)
  * persist closed candles and publish them so the trader wakes up
  * repair gaps left by a disconnection

What is streamed follows the strategy core (settings.data_symbols /
data_timeframes): the 4h portfolio core needs 4h bars for the whole
configured universe plus BTC/quote for its market gate; the ensemble core
needs its decision and higher timeframes for the tradable symbols.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal

from app.config import settings
from app.data.ingest import KlineStream, PollingKlineStream, backfill_universe, gap_check
from app.db import repo
from app.db.session import init_db, session_scope
from app.exchange.registry import make_client
from app.exchange.symbols import SymbolRegistry
from app.util.logging_setup import setup_logging
from app.workers import bus

log = setup_logging("marketdata")


class MarketDataWorker:
    def __init__(self) -> None:
        self.client = make_client()
        self.registry = SymbolRegistry(self.client)
        self.symbols: list[str] = []
        self.timeframes = sorted(settings.data_timeframes)
        self.stream: KlineStream | PollingKlineStream | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await init_db()
        await self.client.sync_time()

        ok, rejected = await self.registry.validate_universe(settings.data_symbols)
        self.symbols = ok
        if rejected:
            async with session_scope() as session:
                await repo.log_event(
                    session,
                    f"Symbols skipped: {', '.join(rejected)}",
                    level="warn",
                    source="marketdata",
                )
        if not self.symbols:
            raise RuntimeError("no tradable symbols in SYMBOLS - check the configuration")

        log.info(
            "universe: %s | timeframes: %s | core: %s",
            ", ".join(self.symbols), self.timeframes, settings.strategy_core,
        )

        async with session_scope() as session:
            await repo.log_event(
                session,
                f"Market data starting for {len(self.symbols)} symbols",
                source="marketdata",
                data={"symbols": self.symbols, "timeframes": self.timeframes},
            )

        await backfill_universe(
            self.client, self.symbols, self.timeframes, settings.candle_history
        )

        # A websocket where the exchange has one; REST polling everywhere else.
        stream_cls = KlineStream if self.client.supports_kline_stream else PollingKlineStream
        self.stream = stream_cls(
            self.client, self.symbols, self.timeframes, self._on_candle
        )

        await asyncio.gather(
            self.stream.run(),
            self._heartbeat(),
            self._gap_repair_loop(),
        )

    # ------------------------------------------------------------------
    async def _on_candle(
        self, symbol: str, timeframe: str, row: dict, closed: bool
    ) -> None:
        # Always publish the mark so PnL and stops track the live price.
        await bus.set_mark(symbol, row["close"])
        await bus.publish(
            bus.CH_TICKS,
            {"symbol": symbol, "price": row["close"], "timeframe": timeframe},
        )

        if not closed:
            return

        async with session_scope() as session:
            await repo.upsert_candles(session, [row])

        await bus.publish(
            bus.CH_CANDLES,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": row["open_time"],
                "close": row["close"],
            },
        )

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            await bus.set_status(
                "marketdata",
                {
                    "connected": bool(self.stream and self.stream.connected),
                    "symbols": self.symbols,
                    "timeframes": self.timeframes,
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
            )
            await asyncio.sleep(15)

    async def _gap_repair_loop(self) -> None:
        """Every few minutes, make sure nothing was missed while disconnected."""
        while not self._stop.is_set():
            await asyncio.sleep(300)
            for timeframe in self.timeframes:
                for symbol in self.symbols:
                    try:
                        repaired = await gap_check(self.client, symbol, timeframe)
                        if repaired:
                            log.info("repaired %s candles for %s %s", repaired, symbol, timeframe)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("gap check failed for %s %s: %s", symbol, timeframe, exc)
                    await asyncio.sleep(0.2)

    async def shutdown(self) -> None:
        self._stop.set()
        if self.stream:
            self.stream.stop()
        await self.client.aclose()
        await bus.close_redis()


async def main() -> None:
    worker = MarketDataWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.shutdown()))
        except NotImplementedError:  # Windows
            pass
    try:
        await worker.start()
    finally:
        await worker.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
