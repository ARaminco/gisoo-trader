"""OK-EX (ok-ex.io, Iranian exchange - not OKX) spot connector - beta.

Written against the official docs (https://docs.ok-ex.io/docs/api) and
checked against public market data. Not yet run with real money.

Auth: headers x-api-key, x-timestamp (ms) and x-signature = hex
HMAC-SHA256(EXCHANGE_API_SECRET, "METHOD\\npath?query\\ntimestamp" +
"\\n" + base64(body) when there is a body). OK-EX requires at least one
allowed IP per key.

Markets: almost everything is USDT-quoted (`BTC-USDT`); the only Toman pair
is USDT-IRT. Candles are UTC-aligned, newest first.

Market orders: the docs size MARKET orders in USDT and leave the unit of the
answer open, so the engine's market orders are sent as marketable limit
orders through the best price (a sell widens until it fills), each cancelled
after a few seconds if it rests. Fees are read from the fills.

Unverified in the docs: fetching one order by id (looked up in the open and
history lists instead), the full status list, whether `balance` excludes
`frozen` (treated as the free amount) and the rate limits.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any

from app.exchange.base import ExchangeError, Market
from app.exchange.iran import common
from app.exchange.iran.common import RestExchange, Throttle, fmt, num

_STATUS = {
    "CREATED": "NEW", "NEW": "NEW", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "PARTIAL_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED", "DONE": "FILLED", "CANCELED": "CANCELED", "CANCELLED": "CANCELED",
    "REJECTED": "REJECTED", "EXPIRED": "EXPIRED",
}
_TYPES_OUT = {"LIMIT": "LIMIT", "STOP_LOSS_LIMIT": "STOP_LIMIT", "STOP_LOSS": "STOP_MARKET"}
_TYPES_IN = {"LIMIT": "LIMIT", "MARKET": "MARKET", "STOP_LIMIT": "STOP_LOSS_LIMIT", "STOP_MARKET": "STOP_LOSS"}


def _unwrap(body: Any) -> Any:
    """Some answers come as {"code": 100, "msg", "data"}, some bare."""
    if isinstance(body, dict) and "data" in body and ("code" in body or "msg" in body):
        return body["data"]
    return body


class OkexClient(RestExchange):
    name = "okex"
    title = "OK-EX"
    title_fa = "اوکی اکسچنج"
    base_url = "https://sapi.ok-ex.io"
    quotes = ("USDT", "IRT")
    supports_stop_orders = True

    native_intervals = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    candles_per_request = 500

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.public_throttle = Throttle(120)

    # ---------------- auth ----------------
    async def _auth_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000) + self.time_offset_ms)
        message = f"{method.upper()}\n{target}\n{timestamp}"
        if body:
            message += "\n" + base64.b64encode(body).decode()
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {"x-api-key": self.api_key, "x-signature": signature, "x-timestamp": timestamp}

    def _is_error_body(self, body: Any) -> bool:
        return isinstance(body, dict) and "code" in body and body.get("code") not in (100, 200, 0, "100", None)

    def _classify(self, status: int, body: Any, text: str) -> tuple[Any, str, str | None]:
        code, message, kind = super()._classify(status, body, text)
        if isinstance(body, dict):
            message = str(body.get("msg") or body.get("error") or message)[:300]
        lowered = message.lower()
        if "balance" in lowered:
            kind = "insufficient_balance"
        elif "not found" in lowered or "not exist" in lowered:
            kind = "unknown_order"
        return code, message, kind

    # ---------------- symbols ----------------
    @staticmethod
    def _okex_symbol(symbol: str) -> str:
        for quote in ("USDT", "IRT"):
            if symbol.endswith(quote):
                return f"{symbol[:-len(quote)]}-{quote}"
        raise ExchangeError(400, None, f"{symbol}: quote must be USDT or IRT", exchange="okex",
                            kind="invalid_symbol")

    async def _fetch_markets(self) -> dict[str, dict]:
        rows = _unwrap(await self._request("GET", "/api/v1/spot/public/pairs", throttle=self.public_throttle))
        markets: dict[str, dict] = {}
        for m in rows or []:
            base, quote = str(m.get("base", "")).upper(), str(m.get("quote", "")).upper()
            if quote not in ("USDT", "IRT") or not base:
                continue
            markets[base + quote] = {
                "base": base,
                "quote": quote,
                "tick": m.get("priceTick"),
                "step": m.get("quantityTick"),
                "min_qty": m.get("quantityTick"),
                "min_notional": m.get("minNotional"),
                "status": "TRADING" if m.get("isTrade", True) else "BREAK",
            }
        return markets

    # ---------------- market data ----------------
    async def _fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        body = await self._request(
            "GET", "/api/v1/spot/public/candles",
            params={"symbol": self._okex_symbol(symbol), "interval": self.native_intervals[interval],
                    "startTime": start_ms, "endTime": end_ms, "limit": self.candles_per_request},
            throttle=self.public_throttle,
        )
        rows = body.get("res") if isinstance(body, dict) and "res" in body else _unwrap(body)
        step = common.interval_ms(interval)
        out = []
        for c in rows or []:
            open_ms = int(num(c.get("time")))
            if open_ms % step:
                continue
            out.append(common.kline_row(open_ms, interval, c.get("open"), c.get("high"), c.get("low"),
                                        c.get("close"), c.get("volume")))
        out.sort(key=lambda r: r[0])   # newest first on the wire
        return out

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        if symbol is None:
            raise self._unsupported("all-symbol ticker")
        body = _unwrap(await self._request("GET", f"/api/v1/spot/public/ticker/{self._okex_symbol(symbol.upper())}",
                                           throttle=self.public_throttle))
        if not isinstance(body, dict) or body.get("lastPrice") in (None, ""):
            raise self._error(404, None, f"no price for {symbol}", "invalid_symbol")
        return {"symbol": symbol.upper(), "price": fmt(body["lastPrice"])}

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        size = next((n for n in (5, 10, 20, 50, 100) if n >= limit), 100)
        body = _unwrap(await self._request("GET", "/api/v1/spot/public/books",
                                           params={"symbol": self._okex_symbol(symbol.upper()), "limit": size},
                                           throttle=self.public_throttle))
        return {"bids": [[fmt(p), fmt(q)] for p, q in (body.get("bids") or [])[:limit]],
                "asks": [[fmt(p), fmt(q)] for p, q in (body.get("asks") or [])[:limit]]}

    # ---------------- account ----------------
    async def _balances(self) -> dict[str, tuple[float, float]]:
        body = _unwrap(await self._request("GET", "/hapi/v1/spot/wallet", signed=True))
        wallets = body.get("wallets") if isinstance(body, dict) else body
        if not isinstance(wallets, list):
            raise self._error(502, None, "wallets missing from the answer")
        out: dict[str, tuple[float, float]] = {}
        for w in wallets:
            asset = str(w.get("asset", "")).upper()
            if asset:
                out[asset] = (num(w.get("balance")), num(w.get("frozen")))
        return out

    # ---------------- orders ----------------
    def _normalize(self, o: dict) -> dict:
        raw_symbol = str(o.get("symbol", ""))
        base, _, quote = raw_symbol.partition("-")
        side = str(o.get("side", "")).upper()
        executed = num(o.get("filled"))
        status = _STATUS.get(str(o.get("status", "")).upper(), "NEW")
        if status == "NEW" and executed > 0:
            status = "PARTIALLY_FILLED"
        return common.order(
            symbol=base.upper() + quote.upper(),
            order_id=o.get("id") or o.get("orderId"),
            client_order_id=o.get("clientOrderId"),
            side=side,
            order_type=_TYPES_IN.get(str(o.get("type", "")).upper(), "LIMIT"),
            status=status,
            price=o.get("price"),
            stop_price=o.get("stopPrice"),
            orig_qty=o.get("quantity"),
            executed_qty=executed,
            avg_price=o.get("avgPrice"),
            fee=o.get("fee"),
            fee_asset=str(o.get("feeAsset") or o.get("fee_asset") or "").upper() or None,
            created_ms=o.get("createdAt"),
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
        kind = order_type.upper()
        if quantity is None:
            raise self._unsupported("quote-amount orders")
        if kind == "MARKET":
            # The docs size MARKET orders in USDT and leave the unit of the
            # answer open: marketable limits keep everything in coins.
            return await self._marketable_order(symbol, side, quantity, client_order_id)
        mapped = _TYPES_OUT.get(kind)
        if mapped is None:
            raise self._unsupported(f"order type {order_type}")
        m = await self.market(symbol)
        body: dict[str, Any] = {"symbol": self._okex_symbol(symbol), "side": side.upper(), "type": mapped,
                                "qt": common.jnum(common.snap(quantity, m["step"]))}
        if price is not None:
            body["price"] = common.jnum(common.snap(price, m["tick"]))
        if stop_price is not None:
            body["stopPrice"] = common.jnum(common.snap(stop_price, m["tick"]))
        if client_order_id:
            body["clientOrderId"] = client_order_id
        try:
            answer = _unwrap(await self._request("POST", "/hapi/v1/spot/order", json=body, signed=True, attempts=1))
        except ExchangeError as exc:
            if exc.status == 0 or exc.status >= 500:
                return await self._lookup_after_ambiguous(symbol, client_order_id, exc)
            raise
        if not isinstance(answer, dict) or not (answer.get("id") or answer.get("orderId")):
            return await self._lookup_after_ambiguous(
                symbol, client_order_id, self._error(502, None, "order answer without an id"))
        return self._normalize(answer)

    async def _place_limit(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal, client_order_id: str | None
    ) -> str:
        body: dict[str, Any] = {"symbol": self._okex_symbol(symbol), "side": side.upper(), "type": "LIMIT",
                                "qt": common.jnum(quantity), "price": common.jnum(price)}
        if client_order_id:
            body["clientOrderId"] = client_order_id
        try:
            answer = _unwrap(await self._request("POST", "/hapi/v1/spot/order", json=body, signed=True, attempts=1))
        except ExchangeError as exc:
            if exc.status == 0 or exc.status >= 500:
                order = await self._lookup_after_ambiguous(symbol, client_order_id, exc)
                return str(order["orderId"])
            raise
        order_id = (answer or {}).get("id") or (answer or {}).get("orderId") if isinstance(answer, dict) else None
        if not order_id:
            if client_order_id:
                order = await self._lookup_after_ambiguous(
                    symbol, client_order_id, self._error(502, None, "order answer without an id"))
                return str(order["orderId"])
            raise self._error(502, None, "order answer without an id")
        return str(order_id)

    async def _enrich(self, symbol: str, order: dict) -> dict:
        """Order answers carry no fee: read it from the fills behind the order."""
        if common.num(order.get("executedQty")) <= 0 or not order.get("orderId"):
            return order
        fills = await self.my_trades(symbol, order["orderId"])
        if fills:
            order = {**order, "fills": fills}
        return order

    async def _history(self, symbol: str, limit: int = 100) -> list[dict]:
        body = _unwrap(await self._request("GET", "/hapi/v1/spot/order/history",
                                           params={"symbol": self._okex_symbol(symbol.upper()), "limit": limit},
                                           signed=True))
        rows = body.get("orders") if isinstance(body, dict) else body
        return [o for o in rows or [] if isinstance(o, dict)]

    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict:
        def match(o: dict) -> bool:
            if order_id is not None:
                return str(o.get("id") or o.get("orderId")) == str(order_id)
            return bool(client_order_id) and o.get("clientOrderId") == client_order_id

        for o in await self.open_orders(symbol, raw=True):
            if match(o):
                return self._normalize(o)
        for o in await self._history(symbol):
            if match(o):
                return self._normalize(o)
        raise self._error(404, None, f"order {order_id or client_order_id} not found", "unknown_order")

    async def cancel_order(self, symbol: str, order_id: str | int, market: Market = "spot") -> dict:
        try:
            await self._request("POST", "/hapi/v1/spot/cancel",
                                json={"orderId": order_id, "symbol": self._okex_symbol(symbol.upper())}, signed=True)
        except ExchangeError as exc:
            try:
                current = await self.query_order(symbol, order_id)
            except ExchangeError:
                raise exc from None
            if current["status"] in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                raise self._error(400, exc.code, f"order {order_id} is {current['status']}", "unknown_order") from exc
            raise
        return {"symbol": symbol.upper(), "orderId": str(order_id), "status": "CANCELED"}

    async def open_orders(self, symbol: str | None = None, market: Market = "spot", raw: bool = False) -> list:
        params = {"symbol": self._okex_symbol(symbol.upper())} if symbol else {}
        body = _unwrap(await self._request("GET", "/hapi/v1/spot/order/open", params=params, signed=True))
        rows = body.get("orders") if isinstance(body, dict) else body
        rows = [o for o in rows or [] if isinstance(o, dict)]
        return rows if raw else [self._normalize(o) for o in rows]

    async def my_trades(self, symbol: str, order_id: str | int, market: Market = "spot") -> list[dict]:
        body = _unwrap(await self._request("GET", "/hapi/v1/spot/order/trades",
                                           params={"symbol": self._okex_symbol(symbol.upper()),
                                                   "order_id": order_id, "limit": 100},
                                           signed=True))
        rows = body.get("trades") if isinstance(body, dict) else body
        return [
            {"price": fmt(t.get("price")), "qty": fmt(t.get("qty")), "commission": fmt(t.get("fee")),
             "commissionAsset": str(t.get("fee_asset") or "").upper()}
            for t in rows or [] if isinstance(t, dict) and str(t.get("order_id")) == str(order_id)
        ]
