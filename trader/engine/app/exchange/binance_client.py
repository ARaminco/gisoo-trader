"""Minimal, dependency-light Binance REST client for Spot and USD-M Futures.

Written directly against the public API instead of a wrapper library so that the
signing, rate-limit handling and testnet routing are all visible and auditable.
Binance is the reference connector: the normalised shapes in `base.py` are its
own answers, so nothing here needs translating.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.exchange.base import ExchangeClient, ExchangeError, Market, notify_user

log = logging.getLogger(__name__)

BASE_URLS: dict[tuple[Market, bool], str] = {
    ("spot", False): "https://api.binance.com",
    ("spot", True): "https://testnet.binance.vision",
    ("futures", False): "https://fapi.binance.com",
    ("futures", True): "https://testnet.binancefuture.com",
}

WS_URLS: dict[tuple[Market, bool], str] = {
    ("spot", False): "wss://stream.binance.com:9443/stream",
    ("spot", True): "wss://stream.testnet.binance.vision/stream",
    ("futures", False): "wss://fstream.binance.com/stream",
    ("futures", True): "wss://stream.binancefuture.com/stream",
}

PREFIX = {"spot": "/api/v3", "futures": "/fapi/v1"}

# Local-clock drift against Binance that earns the user a warning.
CLOCK_DRIFT_ALERT_MS = 2000

# Binance error codes the engine cares about, mapped to portable kinds.
_KINDS: dict[int, str] = {
    -2011: "unknown_order",  # cancel: "Unknown order sent."
    -2013: "unknown_order",  # query: "Order does not exist."
    -2010: "insufficient_balance",
    -2014: "auth",
    -2015: "auth",
    -1022: "auth",
    -1121: "invalid_symbol",
}


class BinanceError(ExchangeError):
    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(status, code, message, exchange="binance",
                         kind=_KINDS.get(code) if isinstance(code, int) else None)


class BinanceClient(ExchangeClient):
    """One client instance serves both Spot and Futures endpoints."""

    name = "binance"
    title = "Binance"
    title_fa = "بایننس"
    supports_stop_orders = True
    supports_kline_stream = True
    supports_user_stream = True
    supports_earn = True
    supports_futures = True
    supports_testnet = True
    supports_fee_token = True
    supports_key_permissions = True

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
        proxy: str | None = None,
    ):
        key, secret = settings.binance_keys()
        self.api_key = api_key if api_key is not None else key
        self.api_secret = api_secret if api_secret is not None else secret
        self.testnet = (
            testnet if testnet is not None else settings.trading_mode == "testnet"
        )
        proxy = proxy if proxy is not None else (settings.exchange_http_proxy or None)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            proxy=proxy,
            headers={"User-Agent": "GisooTrader/0.1"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.time_offset_ms = 0
        self._offset_synced_at = 0.0

    # ---------------- infrastructure ----------------

    def base_url(self, market: Market) -> str:
        return BASE_URLS[(market, self.testnet)]

    def ws_url(self, market: Market) -> str:
        return WS_URLS[(market, self.testnet)]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BinanceClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    async def sync_time(self, market: Market = "spot") -> int:
        """Binance rejects requests whose timestamp drifts more than 1s."""
        path = "/api/v3/time" if market == "spot" else "/fapi/v1/time"
        local_before = int(time.time() * 1000)
        data = await self._request("GET", path, market=market, signed=False)
        local_after = int(time.time() * 1000)
        server = int(data["serverTime"])
        self.time_offset_ms = server - (local_before + local_after) // 2
        self._offset_synced_at = time.time()
        log.info("binance time offset: %sms", self.time_offset_ms)
        if abs(self.time_offset_ms) > CLOCK_DRIFT_ALERT_MS:
            # The offset is compensated in every signature, but a drift this
            # size means the host clock is not being synced (see README "Clock")
            # and one more second of drift breaks every signed request.
            notify_user(
                "system", "warn", "ساعت سیستم با بایننس اختلاف دارد",
                f"اختلاف {self.time_offset_ms / 1000:+.1f} ثانیه. ربات فعلاً جبرانش می‌کند، ولی اگر "
                "بیشتر شود سفارش‌ها با خطای -1021 رد می‌شوند. همگام‌سازی خودکار ساعت سیستم را روشن کن.",
            )
        return self.time_offset_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    async def _request(
        self,
        method: str,
        path: str,
        *,
        market: Market = "spot",
        signed: bool = False,
        params: dict[str, Any] | None = None,
        attempts: int = 4,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url(market)}{path}"
        headers: dict[str, str] = {}

        last_error: Exception | None = None
        # -1021 (timestamp outside recvWindow) is rejected before the request
        # is processed, so re-syncing the clock and sending once more is safe
        # even for an order. One extra attempt, whatever `attempts` says.
        resynced = False
        attempt = 0
        while attempt < attempts:
            attempt += 1
            try:
                if signed:
                    if not self.api_key or not self.api_secret:
                        raise BinanceError(401, None, "API key/secret not configured")
                    if time.time() - self._offset_synced_at > 1800:
                        await self.sync_time(market)
                    signed_params = dict(params)
                    signed_params["timestamp"] = self._timestamp()
                    signed_params.setdefault("recvWindow", 5000)
                    query = self._sign(signed_params)
                    headers = {"X-MBX-APIKEY": self.api_key}
                    request_url = f"{url}?{query}"
                    response = await self._client.request(
                        method, request_url, headers=headers
                    )
                else:
                    response = await self._client.request(method, url, params=params)

                if response.status_code >= 400:
                    code, message = None, response.text[:400]
                    try:
                        body = response.json()
                        code, message = body.get("code"), body.get("msg", message)
                    except Exception:  # noqa: BLE001 - non-JSON error body
                        pass
                    error = BinanceError(response.status_code, code, message)
                    if signed and code == -1021 and not resynced:
                        # The host clock moved (sleep/resume, a VM pause) since
                        # the last sync. Nothing was executed: re-sync, resend.
                        resynced = True
                        log.warning(
                            "binance rejected the request timestamp (-1021: %s); "
                            "re-syncing the clock and retrying once", message,
                        )
                        await self.sync_time(market)
                        attempts += 1
                        last_error = error
                        continue
                    if signed and code == -2015:
                        # Key rejected from this IP (or key deleted / permissions
                        # changed). Every signed call fails the same way, so the
                        # notifier's dedupe turns the flood into one alert.
                        notify_user(
                            "exchange", "error", "کلید API از این IP پذیرفته نشد",
                            f"بایننس درخواست امضاشده را رد کرد (-2015: {message}). "
                            "یا IP این سیستم در لیست مجاز کلید نیست، یا کلید حذف/تغییر شده. "
                            "تا درست نشود هیچ سفارشی به صرافی نمی‌رسد؛ صفحه اعلان‌ها IP فعلی را نشان می‌دهد.",
                        )
                    if error.is_retryable and attempt < attempts:
                        wait = min(2 ** attempt, 20)
                        log.warning("binance %s, retrying in %ss", error, wait)
                        await asyncio.sleep(wait)
                        last_error = error
                        continue
                    raise error

                return response.json()

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                wait = min(2 ** attempt, 20)
                log.warning("binance network error %s, retrying in %ss", exc, wait)
                await asyncio.sleep(wait)

        raise BinanceError(0, None, f"request failed after {attempts} attempts: {last_error}")

    # ---------------- public market data ----------------

    async def exchange_info(self, market: Market = "spot") -> dict:
        path = "/api/v3/exchangeInfo" if market == "spot" else "/fapi/v1/exchangeInfo"
        return await self._request("GET", path, market=market)

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
        market: Market = "spot",
    ) -> list[list]:
        path = f"{PREFIX[market]}/klines"
        return await self._request(
            "GET",
            path,
            market=market,
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": min(limit, 1000),
                "startTime": start_time,
                "endTime": end_time,
            },
        )

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        path = f"{PREFIX[market]}/ticker/price"
        return await self._request("GET", path, market=market, params={"symbol": symbol})

    async def ticker_24h(self, symbol: str | None = None, market: Market = "spot") -> Any:
        path = f"{PREFIX[market]}/ticker/24hr"
        return await self._request("GET", path, market=market, params={"symbol": symbol})

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        path = f"{PREFIX[market]}/depth"
        return await self._request(
            "GET", path, market=market, params={"symbol": symbol, "limit": limit}
        )

    async def funding_rate(self, symbol: str, limit: int = 1) -> list[dict]:
        return await self._request(
            "GET",
            "/fapi/v1/fundingRate",
            market="futures",
            params={"symbol": symbol, "limit": limit},
        )

    # ---------------- account ----------------

    async def spot_account(self) -> dict:
        return await self._request("GET", "/api/v3/account", market="spot", signed=True)

    async def futures_account(self) -> dict:
        return await self._request(
            "GET", "/fapi/v2/account", market="futures", signed=True
        )

    async def spot_balances(self) -> dict[str, float]:
        account = await self.spot_account()
        return {
            b["asset"]: float(b["free"]) + float(b["locked"])
            for b in account.get("balances", [])
            if float(b["free"]) + float(b["locked"]) > 0
        }

    async def free_balance(self, asset: str) -> float:
        account = await self.spot_account()
        for b in account.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    async def api_restrictions(self) -> dict:
        """What this API key may do (read-only): `enableWithdrawals`,
        `ipRestrict`, `enableSpotAndMarginTrading`, `enableFutures`,
        `permitsUniversalTransfer`, ... The committee's health check reads it."""
        return await self._request(
            "GET", "/sapi/v1/account/apiRestrictions", market="spot", signed=True
        )

    async def simple_earn_flexible_position(self, asset: str = "USDT") -> list[dict]:
        """Flexible Simple Earn rows for `asset` (read-only): `totalAmount`,
        `autoSubscribe`, `productId`, APR and reward fields. Empty when nothing
        is subscribed. Never subscribes or redeems anything."""
        data = await self._request(
            "GET",
            "/sapi/v1/simple-earn/flexible/position",
            market="spot",
            signed=True,
            params={"asset": asset, "size": 100},
        )
        if isinstance(data, dict):
            return list(data.get("rows") or [])
        return list(data or [])

    async def simple_earn_flexible_all(self) -> dict[str, dict[str, Any]]:
        """Every flexible Simple Earn position, keyed by asset (read-only).

        Binance's auto-subscribe moves idle spot balances into Simple Earn, so
        this is trading capital that simply is not in the spot wallet. It has
        to be read for an account valuation to be true: without it a sweep
        looks exactly like a withdrawal, which collapses the capital base and
        can trip the drawdown kill switch on money that never left.

        `LD<asset>` rows in the spot wallet are Binance's receipt tokens for
        these same positions; they are not priceable and must never be added
        on top of what this returns.
        """
        data = await self._request(
            "GET",
            "/sapi/v1/simple-earn/flexible/position",
            market="spot",
            signed=True,
            params={"size": 100},
        )
        rows = data.get("rows") if isinstance(data, dict) else data
        book: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            asset = row.get("asset")
            try:
                amount = float(row.get("totalAmount") or 0.0)
            except (TypeError, ValueError):
                continue
            if not asset or amount <= 0:
                continue
            current = book.get(asset)
            if current is not None:
                current["amount"] += amount
                continue
            book[asset] = {
                "amount": amount,
                "product_id": row.get("productId") or f"{asset}001",
                "can_redeem": bool(row.get("canRedeem", True)),
                "auto_subscribe": bool(row.get("autoSubscribe", False)),
            }
        return book

    async def simple_earn_redeem(
        self, product_id: str, amount: float, *, fast: bool = True
    ) -> dict:
        """Redeem `amount` from a flexible Simple Earn product back to spot.

        Fast redemption credits the spot wallet immediately (the engine needs
        the quote asset in the same bar it decided to buy); `fast=False` falls
        back to the standard next-day redemption, which the caller must not
        wait on. Redeeming never leaves the user's account.
        """
        return await self._request(
            "POST",
            "/sapi/v1/simple-earn/flexible/redeem",
            market="spot",
            signed=True,
            params={
                "productId": product_id,
                "amount": f"{amount:.8f}",
                "destAccount": "SPOT",
                "type": "FAST" if fast else "NORMAL",
            },
            attempts=2,
        )

    async def set_bnb_fee_discount(self, enabled: bool = True) -> dict:
        return await self._request(
            "POST",
            "/sapi/v1/bnbBurn",
            market="spot",
            signed=True,
            params={"spotBNBBurn": "true" if enabled else "false"},
        )

    # ---------------- orders ----------------

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        market: Market = "spot",
        quantity: float | None = None,
        quote_quantity: float | None = None,
        price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str | None = None,
        reduce_only: bool | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "newClientOrderId": client_order_id,
        }
        if quantity is not None:
            params["quantity"] = quantity
        if quote_quantity is not None and market == "spot":
            params["quoteOrderQty"] = quote_quantity
        if price is not None:
            params["price"] = price
            params["timeInForce"] = time_in_force or "GTC"
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if reduce_only is not None and market == "futures":
            params["reduceOnly"] = "true" if reduce_only else "false"
        # Futures default to an ACK (executedQty=0 even for a filled MARKET
        # order); ask for the result so the caller sees the fill.
        params["newOrderRespType"] = "RESULT" if market == "futures" else "FULL"

        # Placement is sent exactly once. Binance defines a 5xx or a timeout on
        # POST /order as "execution status UNKNOWN": the order may well have
        # filled, and a resend is a second fill. Look it up instead.
        try:
            return await self._request(
                "POST", path, market=market, signed=True, params=params, attempts=1
            )
        except BinanceError as exc:
            if client_order_id and (exc.status == 0 or exc.status >= 500):
                return await self._resolve_unknown_order(
                    symbol, market, client_order_id, exc
                )
            raise

    async def _resolve_unknown_order(
        self, symbol: str, market: Market, client_order_id: str, original: BinanceError
    ) -> dict:
        """After an ambiguous placement failure, find out what the exchange did.

        Returns the order as Binance sees it (with its fills attached so the
        caller can account commissions), or re-raises `original` when Binance
        has never heard of the client id - the order was genuinely not placed.
        """
        log.warning(
            "order %s on %s had an ambiguous result (%s); looking it up",
            client_order_id, symbol, original,
        )
        last_error: Exception = original
        for _ in range(3):
            await asyncio.sleep(1.0)
            try:
                order = await self.query_order(
                    symbol, market=market, client_order_id=client_order_id
                )
            except BinanceError as lookup:
                if lookup.code == -2013:  # "Order does not exist" -> never placed
                    raise original
                last_error = lookup
                continue
            if "fills" not in order and float(order.get("executedQty", 0) or 0) > 0:
                try:
                    trades = await self.my_trades(symbol, order["orderId"], market)
                    order["fills"] = [
                        {
                            "price": t.get("price"),
                            "qty": t.get("qty"),
                            "commission": t.get("commission"),
                            "commissionAsset": t.get("commissionAsset"),
                        }
                        for t in trades
                    ]
                except BinanceError as exc:
                    log.warning("could not fetch fills for %s: %s", order.get("orderId"), exc)
            return order
        raise BinanceError(
            0, None, f"order {client_order_id} unresolved after ambiguous failure: {last_error}"
        )

    async def cancel_order(
        self, symbol: str, order_id: str | int, market: Market = "spot"
    ) -> dict:
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        return await self._request(
            "DELETE",
            path,
            market=market,
            signed=True,
            params={"symbol": symbol, "orderId": order_id},
        )

    async def open_orders(self, symbol: str | None = None, market: Market = "spot") -> list:
        path = "/api/v3/openOrders" if market == "spot" else "/fapi/v1/openOrders"
        return await self._request(
            "GET", path, market=market, signed=True, params={"symbol": symbol}
        )

    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict:
        """Look an order up by exchange id, or by the client id we sent."""
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        return await self._request(
            "GET",
            path,
            market=market,
            signed=True,
            params={
                "symbol": symbol,
                "orderId": order_id,
                "origClientOrderId": client_order_id,
            },
        )

    async def my_trades(
        self, symbol: str, order_id: str | int, market: Market = "spot"
    ) -> list[dict]:
        """The fills behind one order (price, qty, commission, commissionAsset)."""
        path = "/api/v3/myTrades" if market == "spot" else "/fapi/v1/userTrades"
        return await self._request(
            "GET",
            path,
            market=market,
            signed=True,
            params={"symbol": symbol, "orderId": order_id},
        )

    # ---------------- futures configuration ----------------

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request(
            "POST",
            "/fapi/v1/leverage",
            market="futures",
            signed=True,
            params={"symbol": symbol, "leverage": leverage},
        )

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        try:
            return await self._request(
                "POST",
                "/fapi/v1/marginType",
                market="futures",
                signed=True,
                params={"symbol": symbol, "marginType": margin_type},
            )
        except BinanceError as exc:
            if exc.code == -4046:  # already set
                return {"msg": "no change needed"}
            raise

    async def futures_positions(self) -> list[dict]:
        account = await self.futures_account()
        return [
            p for p in account.get("positions", []) if float(p.get("positionAmt", 0)) != 0
        ]
