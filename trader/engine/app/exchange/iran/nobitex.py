"""Nobitex (nobitex.ir) spot connector - beta.

Written against the official OpenAPI files (https://apidocs.nobitex.ir,
market_data.yaml / spot_trade.yaml / user_data.yaml / api_key.yaml) and
checked against public market data. Not yet run with real money.

Auth, two schemes (every private endpoint accepts either):
  * API key (recommended): EXCHANGE_API_KEY = the public key, EXCHANGE_API_SECRET
    = the Ed25519 private key (URL-safe base64), signing
    `timestamp + METHOD + path?query + raw_body` into Nobitex-Signature.
    Create it with READ,TRADE only (never WITHDRAW) and an IP whitelist;
    Nobitex does not let you change permissions afterwards.
  * Token: EXCHANGE_API_KEY = a login token, EXCHANGE_API_SECRET empty.

Units: orders, order book and stats of Rial markets are in **Rial**
(`dstCurrency=rls`, symbol label IRT); UDF candles of those markets come back
in **Toman**. The engine's IRT is the Toman, so prices are multiplied by 10
on the way out and divided on the way in - except candles.

Candles: Nobitex aligns 1h and 4h buckets to Tehran time (UTC+3:30); its 30m
buckets are UTC-aligned, so 1h and 4h bars are built from those. 30m history
only reaches back about a week (observed), so older 4h bars come from
Nobitex's own 4h candles shifted 30 minutes onto the UTC grid (a bucket that
opens at 00:30 UTC is stored as the 00:00 bar). The recent bars the engine
decides on are always the exact 30m-built ones.

Failures arrive as HTTP 200 with {"status": "failed", "code", "message"}.
The FAQ asks for requests from an Iranian IP.
"""
from __future__ import annotations

import base64
import time
from decimal import Decimal
from typing import Any

from app.exchange.base import ExchangeError, Market
from app.exchange.iran import common
from app.exchange.iran.common import RestExchange, Throttle, fmt, num

RIAL_PER_TOMAN = 10
# 30m UDF history is kept for about seven days; older bars come from 4h.
FINE_HISTORY_MS = 6 * 24 * 3600 * 1000
TEHRAN_SHIFT_MS = 30 * 60 * 1000

_STATUS = {"new": "NEW", "active": "NEW", "inactive": "NEW", "done": "FILLED", "canceled": "CANCELED"}
_EXECUTION_OUT = {"MARKET": "market", "LIMIT": "limit", "STOP_LOSS_LIMIT": "stop_limit", "STOP_LOSS": "stop_market"}
_EXECUTION_IN = {"market": "MARKET", "limit": "LIMIT", "stoplimit": "STOP_LOSS_LIMIT", "stopmarket": "STOP_LOSS"}
_KINDS = {
    "SmallOrder": "invalid_order",
    "InvalidMarketPair": "invalid_symbol",
    "MarketClosed": "invalid_symbol",
    "OverValueOrder": "insufficient_balance",
    "InsufficientBalance": "insufficient_balance",
    "NotFound": "unknown_order",
    "TooManyRequests": "rate_limit",
}


class NobitexClient(RestExchange):
    name = "nobitex"
    title = "Nobitex"
    title_fa = "نوبیتکس"
    base_url = "https://apiv2.nobitex.ir"
    supports_stop_orders = True

    native_intervals = {"1m": "1", "5m": "5", "15m": "15", "30m": "30"}
    built_from = {"1h": "30m", "4h": "30m"}
    candles_per_request = 480

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Nobitex asks bots to identify themselves this way.
        self._client.headers["User-Agent"] = "TraderBot/GisooTrader-0.1"
        self.public_throttle = Throttle(55)     # UDF history: 60/min
        self._book_throttle = Throttle(240)     # order book: 300/min
        self._private_throttle = Throttle(50)
        self._signer = None
        if self.api_secret:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            seed = base64.urlsafe_b64decode(self.api_secret + "=" * (-len(self.api_secret) % 4))
            self._signer = Ed25519PrivateKey.from_private_bytes(seed)

    # ---------------- auth / errors ----------------
    async def _auth_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        if self._signer is None:
            return {"Authorization": f"Token {self.api_key}"}
        timestamp = str(int(time.time() + self.time_offset_ms / 1000))
        message = (timestamp + method.upper() + target).encode() + body
        signature = base64.urlsafe_b64encode(self._signer.sign(message)).decode()
        return {"Nobitex-Key": self.api_key, "Nobitex-Signature": signature, "Nobitex-Timestamp": timestamp}

    def _is_error_body(self, body: Any) -> bool:
        return isinstance(body, dict) and str(body.get("status", "")).lower() == "failed"

    def _classify(self, status: int, body: Any, text: str) -> tuple[Any, str, str | None]:
        code, message, kind = super()._classify(status, body, text)
        if isinstance(body, dict):
            code = body.get("code") or code
            kind = _KINDS.get(str(code), kind)
            if status == 404 and kind == "unknown_order" and "order" not in text.lower():
                kind = None
        return code, message, kind

    async def sync_time(self, market: Market = "spot") -> int:
        """Offset from the server's Date header: API-key signatures must be
        within 30 s of Nobitex's clock."""
        try:
            response = await self._client.get("/v3/orderbook/BTCUSDT")
            from email.utils import parsedate_to_datetime

            server = parsedate_to_datetime(response.headers["date"]).timestamp()
            self.time_offset_ms = int((server - time.time()) * 1000)
        except Exception:  # noqa: BLE001 - keep the last offset
            pass
        return self.time_offset_ms

    # ---------------- symbols and units ----------------
    @staticmethod
    def _split(symbol: str) -> tuple[str, str]:
        for quote in ("USDT", "IRT"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return symbol[: -len(quote)], quote
        raise ExchangeError(400, None, f"{symbol}: quote must be USDT or IRT", exchange="nobitex",
                            kind="invalid_symbol")

    @staticmethod
    def _dst(quote: str) -> str:
        return "rls" if quote == "IRT" else quote.lower()

    @staticmethod
    def _price_out(value: float, quote: str) -> str:
        d = Decimal(str(value))
        return fmt(d * RIAL_PER_TOMAN if quote == "IRT" else d)

    @staticmethod
    def _price_in(value: Any, quote: str) -> float:
        v = num(value)
        return v / RIAL_PER_TOMAN if quote == "IRT" else v

    async def _fetch_markets(self) -> dict[str, dict]:
        body = await self._request("GET", "/v2/options")
        options = (body or {}).get("nobitex") or {}
        amounts = options.get("amountPrecisions") or {}
        prices = options.get("pricePrecisions") or {}
        minimums = options.get("minOrders") or {}
        markets: dict[str, dict] = {}
        for symbol, step in amounts.items():
            try:
                base, quote = self._split(symbol)
            except ExchangeError:
                continue
            tick = num(prices.get(symbol), 0.0)
            min_value = num(minimums.get(self._dst(quote)), 0.0)
            markets[symbol] = {
                "base": base,
                "quote": quote,
                "tick": tick / RIAL_PER_TOMAN if quote == "IRT" else tick,
                "step": num(step),
                "min_qty": num(step),
                "min_notional": min_value / RIAL_PER_TOMAN if quote == "IRT" else min_value,
                "status": "TRADING",
            }
        return markets

    # ---------------- market data ----------------
    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
        market: Market = "spot",
    ) -> list[list]:
        if interval != "4h":
            return await super().klines(symbol, interval, limit, start_time, end_time, market)
        start, end = common.window(interval, limit, start_time, end_time)
        boundary = int(time.time() * 1000) - FINE_HISTORY_MS
        boundary -= boundary % common.interval_ms("4h")
        rows: list[list] = []
        if start < boundary:
            rows.extend(await self._coarse_4h(symbol.upper(), start, min(end, boundary - 1)))
        if end >= boundary:
            fine: list[list] = []
            for a, b in common.chunks(max(start, boundary), end, common.interval_ms("30m"), self.candles_per_request):
                fine.extend(await self._fetch_candles(symbol.upper(), "30m", a, b))
            rows.extend(common.aggregate(fine, "4h"))
        return common.trim(rows, limit, start_time, end_time)

    async def _coarse_4h(self, symbol: str, start_ms: int, end_ms: int) -> list[list]:
        """Nobitex's Tehran-aligned 4h candles, shifted onto the UTC 4h grid."""
        step = common.interval_ms("4h")
        rows = []
        for a, b in common.chunks(start_ms, end_ms + TEHRAN_SHIFT_MS, step, self.candles_per_request):
            body = await self._udf(symbol, "240", a, b)
            for t, o, h, l, c, v in zip(body["t"], body["o"], body["h"], body["l"], body["c"], body["v"]):
                open_ms = int(t) * 1000 - TEHRAN_SHIFT_MS
                if open_ms % step or open_ms < start_ms or open_ms > end_ms:
                    continue
                rows.append(common.kline_row(open_ms, "4h", o, h, l, c, v))
        return rows

    async def _udf(self, symbol: str, resolution: str, start_ms: int, end_ms: int) -> dict:
        body = await self._request(
            "GET", "/market/udf/history",
            params={"symbol": symbol, "resolution": resolution,
                    "from": start_ms // 1000, "to": end_ms // 1000},
            throttle=self.public_throttle,
        )
        if not isinstance(body, dict) or body.get("s") != "ok":
            return {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        return body

    async def _fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        body = await self._udf(symbol, self.native_intervals[interval], start_ms, end_ms)
        step = common.interval_ms(interval)
        rows = []
        # UDF candles are already in Toman for IRT markets (observed); no conversion.
        for t, o, h, l, c, v in zip(body["t"], body["o"], body["h"], body["l"], body["c"], body["v"]):
            open_ms = int(t) * 1000
            rows.append(common.kline_row(open_ms - open_ms % step, interval, o, h, l, c, v))
        return rows

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        base, quote = self._split(symbol.upper())
        body = await self._request("GET", f"/v3/orderbook/{symbol.upper()}", throttle=self._book_throttle)

        def side(rows: list) -> list[list[str]]:
            return [[fmt(self._price_in(p, quote)), str(a)] for p, a in (rows or [])[:limit]]

        return {"bids": side(body.get("bids")), "asks": side(body.get("asks")),
                "lastPrice": fmt(self._price_in(body.get("lastTradePrice"), quote))}

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        if symbol is None:
            raise self._unsupported("all-symbol ticker")
        book = await self.order_book(symbol, limit=1)
        return {"symbol": symbol.upper(), "price": book["lastPrice"]}

    # ---------------- account ----------------
    async def _balances(self) -> dict[str, tuple[float, float]]:
        body = await self._request("POST", "/users/wallets/list", json={"type": "spot"},
                                   signed=True, throttle=self._private_throttle)
        wallets = (body or {}).get("wallets")
        if not isinstance(wallets, list):
            raise self._error(502, None, "wallets missing from the answer")
        out: dict[str, tuple[float, float]] = {}
        for w in wallets:
            currency = str(w.get("currency", "")).lower()
            if not currency:
                continue
            total = num(w.get("balance"))
            locked = num(w.get("blockedBalance"))
            free = num(w.get("activeBalance"), max(0.0, total - locked))
            if currency == "rls":
                out["IRT"] = (free / RIAL_PER_TOMAN, locked / RIAL_PER_TOMAN)
            else:
                out[currency.upper()] = (free, locked)
        return out

    # ---------------- orders ----------------
    def _normalize(self, o: dict, symbol_hint: str | None = None) -> dict:
        market = str(o.get("market") or "")
        if "-" in market:
            base, dst = market.split("-", 1)
            quote = "IRT" if dst.upper() in ("RLS", "IRT") else dst.upper()
            symbol = base.upper() + quote
        else:
            symbol = symbol_hint or ""
            base, quote = self._split(symbol) if symbol else ("", "")
        side = "BUY" if str(o.get("type", "")).lower() == "buy" else "SELL"
        execution = str(o.get("execution", "")).replace("_", "").lower()
        executed = num(o.get("matchedAmount"))
        status = _STATUS.get(str(o.get("status", "")).lower(), "NEW")
        if status == "NEW" and executed > 0:
            status = "PARTIALLY_FILLED"
        price = o.get("price")
        price = 0.0 if price in (None, "market") else self._price_in(price, quote)
        avg = self._price_in(o.get("averagePrice"), quote)
        # Nobitex takes the fee from what the user receives: base on a buy,
        # quote (Rial on IRT markets) on a sell.
        fee = num(o.get("fee"))
        if side == "SELL" and quote == "IRT":
            fee /= RIAL_PER_TOMAN
        created = o.get("created_at")
        return common.order(
            symbol=symbol,
            order_id=o.get("id"),
            client_order_id=o.get("clientOrderId"),
            side=side,
            order_type=_EXECUTION_IN.get(execution, "LIMIT"),
            status=status,
            price=price,
            stop_price=self._price_in(o.get("param1"), quote) if o.get("param1") else None,
            orig_qty=o.get("amount"),
            executed_qty=executed,
            quote_qty=self._price_in(o.get("totalPrice"), quote) if o.get("totalPrice") not in (None, "") else None,
            avg_price=avg,
            fee=fee,
            fee_asset=base if side == "BUY" else quote,
            created_ms=_iso_ms(created),
            raw=o,
        )

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
        self._spot_only(market)
        symbol = symbol.upper()
        base, quote = self._split(symbol)
        execution = _EXECUTION_OUT.get(order_type.upper())
        if execution is None:
            raise self._unsupported(f"order type {order_type}")
        if quantity is None:
            raise self._unsupported("quote-amount orders")
        body: dict[str, Any] = {
            "type": side.lower(),
            "execution": execution,
            "srcCurrency": base.lower(),
            "dstCurrency": self._dst(quote),
            "amount": fmt(quantity),
        }
        if price is not None and execution != "market":
            body["price"] = self._price_out(price, quote)
        if stop_price is not None:
            body["stopPrice"] = self._price_out(stop_price, quote)
        if client_order_id:
            body["clientOrderId"] = client_order_id[:32]
        try:
            answer = await self._request("POST", "/market/orders/add", json=body, signed=True, attempts=1)
        except ExchangeError as exc:
            if exc.status == 0 or exc.status >= 500:
                return await self._lookup_after_ambiguous(symbol, client_order_id, exc)
            raise
        if not (answer or {}).get("order"):
            return await self._lookup_after_ambiguous(
                symbol, client_order_id, self._error(502, None, "order answer without an order"))
        return self._normalize(answer["order"], symbol)

    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict:
        body = {"id": int(order_id)} if order_id is not None else {"clientOrderId": client_order_id}
        try:
            answer = await self._request("POST", "/market/orders/status", json=body, signed=True,
                                         throttle=self._private_throttle)
            return self._normalize(answer.get("order") or {}, symbol.upper())
        except ExchangeError as exc:
            if order_id is not None or not client_order_id:
                raise
            # A client id is only looked up among open orders; a filled
            # market order has to be found in the recent order list.
            base, quote = self._split(symbol.upper())
            listing = await self._request(
                "GET", "/market/orders/list",
                params={"status": "all", "details": 2, "tradeType": "spot", "srcCurrency": base.lower(),
                        "dstCurrency": self._dst(quote), "pageSize": 50},
                signed=True, throttle=self._private_throttle,
            )
            for o in listing.get("orders") or []:
                if o.get("clientOrderId") == client_order_id:
                    return self._normalize(o, symbol.upper())
            raise self._error(404, exc.code, f"order {client_order_id} not found", "unknown_order") from exc

    async def cancel_order(self, symbol: str, order_id: str | int, market: Market = "spot") -> dict:
        try:
            answer = await self._request(
                "POST", "/market/orders/update-status",
                json={"order": int(order_id), "status": "canceled"}, signed=True,
            )
        except ExchangeError as exc:
            # Nobitex refuses to cancel an order that is already done or gone.
            try:
                current = await self.query_order(symbol, order_id)
            except ExchangeError:
                raise exc from None
            if current["status"] in ("FILLED", "CANCELED"):
                raise self._error(400, exc.code, f"order {order_id} is {current['status']}", "unknown_order") from exc
            raise
        return self._normalize(answer.get("order") or {"id": order_id, "status": "Canceled"}, symbol.upper())

    async def open_orders(self, symbol: str | None = None, market: Market = "spot") -> list:
        params: dict[str, Any] = {"status": "open", "details": 2, "tradeType": "spot", "pageSize": 100}
        if symbol:
            base, quote = self._split(symbol.upper())
            params.update(srcCurrency=base.lower(), dstCurrency=self._dst(quote))
        listing = await self._request("GET", "/market/orders/list", params=params, signed=True,
                                      throttle=self._private_throttle)
        return [self._normalize(o, symbol) for o in listing.get("orders") or []]

    async def my_trades(self, symbol: str, order_id: str | int, market: Market = "spot") -> list[dict]:
        base, quote = self._split(symbol.upper())
        listing = await self._request(
            "GET", "/market/trades/list",
            params={"srcCurrency": base.lower(), "dstCurrency": self._dst(quote), "pageSize": 100},
            signed=True, throttle=self._private_throttle,
        )
        fills = []
        for t in listing.get("trades") or []:
            if str(t.get("orderId")) != str(order_id):
                continue
            buy = str(t.get("type", "")).lower() == "buy"
            fee = num(t.get("fee"))
            if not buy and quote == "IRT":
                fee /= RIAL_PER_TOMAN
            fills.append({
                "price": fmt(self._price_in(t.get("price"), quote)),
                "qty": str(t.get("amount")),
                "commission": fmt(fee),
                "commissionAsset": base if buy else quote,
            })
        return fills


def _iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        import datetime as dt

        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
