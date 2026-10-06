"""Binance user data stream - real-time account synchronisation.

REST polling tells you what happened up to 30 seconds ago. This stream tells you
the moment an order fills, a balance moves, or a futures position changes, which
is what keeps the local database and the exchange from drifting apart.

Flow:
  1. POST /api/v3/userDataStream  -> listenKey
  2. connect to wss://.../ws/<listenKey>
  3. PUT the same endpoint every 30 minutes or the key expires
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import websockets

from app.config import settings
from app.exchange.binance_client import BinanceClient, BinanceError  # Binance only

log = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 1500  # 25 minutes, key lives for 60

WS_BASE = {
    ("spot", False): "wss://stream.binance.com:9443/ws",
    ("spot", True): "wss://stream.testnet.binance.vision/ws",
    ("futures", False): "wss://fstream.binance.com/ws",
    ("futures", True): "wss://stream.binancefuture.com/ws",
}

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class UserDataStream:
    """Keeps one authenticated stream alive, reconnecting on any failure."""

    def __init__(
        self,
        client: BinanceClient,
        handler: EventHandler,
        market: str = "spot",
        on_state: Callable[[bool, str | None], Awaitable[None]] | None = None,
    ):
        self.client = client
        self.handler = handler
        self.market = market
        self.on_state = on_state
        self.listen_key: str | None = None
        self._stop = asyncio.Event()
        self._ws: Any = None

    # ---------------- listen key lifecycle ----------------

    async def _create_listen_key(self) -> str:
        path = "/api/v3/userDataStream" if self.market == "spot" else "/fapi/v1/listenKey"
        # This endpoint is key-authenticated but not signed.
        response = await self.client._client.post(  # noqa: SLF001 - deliberate reuse
            f"{self.client.base_url(self.market)}{path}",
            headers={"X-MBX-APIKEY": self.client.api_key},
        )
        if response.status_code >= 400:
            raise BinanceError(response.status_code, None, response.text[:300])
        return response.json()["listenKey"]

    async def _keepalive(self) -> None:
        path = "/api/v3/userDataStream" if self.market == "spot" else "/fapi/v1/listenKey"
        params = {"listenKey": self.listen_key} if self.market == "spot" else {}
        response = await self.client._client.put(  # noqa: SLF001
            f"{self.client.base_url(self.market)}{path}",
            headers={"X-MBX-APIKEY": self.client.api_key},
            params=params,
        )
        if response.status_code >= 400:
            raise BinanceError(response.status_code, None, response.text[:300])

    async def _close_listen_key(self) -> None:
        if not self.listen_key:
            return
        path = "/api/v3/userDataStream" if self.market == "spot" else "/fapi/v1/listenKey"
        try:
            await self.client._client.delete(  # noqa: SLF001
                f"{self.client.base_url(self.market)}{path}",
                headers={"X-MBX-APIKEY": self.client.api_key},
                params={"listenKey": self.listen_key},
            )
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass
        self.listen_key = None

    # ---------------- main loop ----------------

    async def run(self) -> None:
        if not self.client.api_key:
            log.warning("user data stream disabled: no API key configured")
            return

        backoff = 2
        while not self._stop.is_set():
            keepalive_task: asyncio.Task | None = None
            connected_at: float | None = None
            try:
                self.listen_key = await self._create_listen_key()
                url = f"{WS_BASE[(self.market, self.client.testnet)]}/{self.listen_key}"
                log.info("user data stream connecting (%s)", self.market)

                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5
                ) as ws:
                    self._ws = ws
                    connected_at = time.monotonic()
                    if self.on_state:
                        await self.on_state(True, None)
                    keepalive_task = asyncio.create_task(self._keepalive_loop())

                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        try:
                            await self.handler(event)
                        except Exception as exc:  # noqa: BLE001
                            log.exception("user stream handler failed: %s", exc)

                # A clean server-side close ends the loop without an exception.
                # Still back off, or a refused stream hammers POST /userDataStream.
                if not self._stop.is_set():
                    log.warning("user data stream closed by the server (retry in %ss)", backoff)
                    if self.on_state:
                        await self.on_state(False, "closed by the server")
                    await asyncio.sleep(backoff)

            except asyncio.CancelledError:
                raise
            except BinanceError as exc:
                if exc.status == 410:
                    # Binance retired the REST listenKey endpoint (410 Gone).
                    # Its replacement is the WebSocket API subscription, which
                    # needs Ed25519 keys - HMAC keys cannot use it. Stop here
                    # instead of hitting a dead endpoint every minute; the REST
                    # reconciliation sweep remains the source of truth.
                    log.warning(
                        "user data stream unavailable: listenKey API retired by "
                        "Binance (410). Falling back to REST reconciliation only."
                    )
                    if self.on_state:
                        await self.on_state(
                            False, "listenKey endpoint retired by Binance (410) - REST sync only"
                        )
                    return
                log.warning("user data stream dropped: %s (retry in %ss)", exc, backoff)
                if self.on_state:
                    await self.on_state(False, str(exc)[:300])
                await asyncio.sleep(backoff)
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                log.warning("user data stream dropped: %s (retry in %ss)", exc, backoff)
                if self.on_state:
                    await self.on_state(False, str(exc)[:300])
                await asyncio.sleep(backoff)
            finally:
                self._ws = None
                if keepalive_task:
                    keepalive_task.cancel()
                await self._close_listen_key()
                # A connection that lived a while earns a fresh backoff; one that
                # died at once doubles it.
                lived = connected_at is not None and time.monotonic() - connected_at > 60
                backoff = 2 if lived else min(backoff * 2, 60)

    async def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(KEEPALIVE_SECONDS)
            try:
                await self._keepalive()
                log.debug("listen key refreshed")
            except Exception as exc:  # noqa: BLE001
                log.warning("listen key refresh failed: %s", exc)
                # The key will expire at 60 minutes. Drop the socket now so
                # run() reconnects on a fresh key instead of going deaf later.
                await self._close_socket(code=1012, reason="listenKey keepalive failed")
                return

    async def _close_socket(self, code: int, reason: str) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already closing
            pass

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        """Stop and drop the socket so run() returns promptly on shutdown."""
        self._stop.set()
        await self._close_socket(code=1001, reason="shutdown")
