"""Wallex (wallex.ir) spot connector - beta.

Written against the official docs and Swagger (https://developers.wallex.ir)
and checked against public market data. Not yet run with real money.

Auth: header `x-api-key: EXCHANGE_API_KEY`, no signing (EXCHANGE_API_SECRET
unused). Create the key in the Wallex panel with Read + Trade only - never
Withdraw.

Units: Toman markets are quoted `TMN`, mapped to the engine's IRT (Toman, no
conversion). Numbers come back as strings.

Orders: Wallex has no numeric order id; the client id is the only handle, so
the engine's client id becomes the order id. Client ids may contain A-Z, 0-9
and `_` only: they are sent upper-case and lower-cased on the way back.

Candles: resolutions above 1h return 1h candles (observed), and 1h candles
are UTC-aligned (observed), so 4h bars are built from 1h.

Unverified in the docs, handled conservatively: whether `value` in the
balance includes `locked` (treated as the total), the full status list, and
the role of `price` on a MARKET order (sent as the last price, as the docs
mark it required).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.exchange.base import ExchangeError, Market
from app.exchange.iran import common
from app.exchange.iran.common import RestExchange, Throttle, fmt, num

_QUOTES = {"TMN": "IRT", "USDT": "USDT"}
_STATUS = {
    "NEW": "NEW", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
    "CANCELED": "CANCELED", "CANCELLED": "CANCELED", "REJECTED": "REJECTED", "EXPIRED": "EXPIRED",
}
_TYPES_OUT = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP_LOSS_LIMIT": "STOP_LIMIT", "STOP_LOSS": "STOP_MARKET"}
_TYPES_IN = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP_LIMIT": "STOP_LOSS_LIMIT", "STOP_MARKET": "STOP_LOSS"}


class WallexClient(RestExchange):
    name = "wallex"
    title = "Wallex"
    title_fa = "والکس"
    base_url = "https://api.wallex.ir"
    supports_stop_orders = True

    native_intervals = {"1m": "1", "1h": "60"}
    built_from = {"4h": "1h"}
    candles_per_request = 500

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.public_throttle = Throttle(60)
        self._order_throttle = Throttle(100)  # 20 per 10 s documented

    async def _auth_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        return {"x-api-key": self.api_key}

    def _is_error_body(self, body: Any) -> bool:
        return isinstance(body, dict) and body.get("success") is False

    def _classify(self, status: int, body: Any, text: str) -> tuple[Any, str, str | None]:
        code, message, kind = super()._classify(status, body, text)
        lowered = message.lower()
        if "insufficient" in lowered or "balance" in lowered:
            kind = "insufficient_balance"
        elif status == 404 or "not found" in lowered:
            kind = "unknown_order"
        return code, message, kind

    # ---------------- symbols ----------------
    @staticmethod
    def _wallex_symbol(symbol: str) -> str:
        return symbol[:-3] + "TMN" if symbol.endswith("IRT") else symbol

    async def _fetch_markets(self) -> dict[str, dict]:
        body = await self._request("GET", "/v1/markets")
        symbols = ((body or {}).get("result") or {}).get("symbols") or {}
        markets: dict[str, dict] = {}
        for raw_symbol, m in symbols.items():
            quote = _QUOTES.get(str(m.get("quoteAsset", "")).upper())
            base = str(m.get("baseAsset", "")).upper()
            if not quote or not base:
                continue
            markets[base + quote] = {
                "base": base,
                "quote": quote,
                "tick": common.step_from_precision(m.get("tickSize")),
                "step": common.step_from_precision(m.get("stepSize")),
                "min_qty": m.get("minQty"),
                "min_notional": m.get("minNotional"),
                "status": "TRADING",
                "wallex_symbol": raw_symbol,
                "last": ((m.get("stats") or {}).get("lastPrice")),
            }
        return markets

    # ---------------- market data ----------------
    async def _fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        body = await self._request(
            "GET", "/v1/udf/history",
            params={"symbol": self._wallex_symbol(symbol), "resolution": self.native_intervals[interval],
                    "from": start_ms // 1000, "to": end_ms // 1000},
            throttle=self.public_throttle,
        )
        if not isinstance(body, dict) or body.get("s") != "ok":
            return []
        step = common.interval_ms(interval)
        rows = []
        for t, o, h, l, c, v in zip(body["t"], body["o"], body["h"], body["l"], body["c"], body["v"]):
            open_ms = int(t) * 1000
            if open_ms % step:
                # Never trust the requested resolution: skip off-grid bars.
                continue
            rows.append(common.kline_row(open_ms, interval, o, h, l, c, v))
        return rows

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        body = await self._request("GET", "/v1/depth", params={"symbol": self._wallex_symbol(symbol.upper())},
                                   throttle=self.public_throttle)
        result = (body or {}).get("result") or {}

        def side(rows: list) -> list[list[str]]:
            return [[fmt(r.get("price")), fmt(r.get("quantity"))] for r in (rows or [])]

        bids, asks = side(result.get("bid")), side(result.get("ask"))
        # Observed: the live API labels the sides from the taker's point of
        # view (its "ask" list sits below the last price). Trust the prices.
        if bids and asks and num(bids[0][0]) > num(asks[0][0]):
            bids, asks = asks, bids
        bids.sort(key=lambda r: num(r[0]), reverse=True)
        asks.sort(key=lambda r: num(r[0]))
        return {"bids": bids[:limit], "asks": asks[:limit]}

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        if symbol is None:
            raise self._unsupported("all-symbol ticker")
        body = await self._request("GET", "/v1/trades", params={"symbol": self._wallex_symbol(symbol.upper())},
                                   throttle=self.public_throttle)
        trades = ((body or {}).get("result") or {}).get("latestTrades") or []
        if not trades:
            raise self._error(404, None, f"no price for {symbol}", "invalid_symbol")
        return {"symbol": symbol.upper(), "price": fmt(trades[0].get("price"))}

    # ---------------- account ----------------
    async def _balances(self) -> dict[str, tuple[float, float]]:
        body = await self._request("GET", "/v1/account/balances", signed=True)
        balances = ((body or {}).get("result") or {}).get("balances")
        if not isinstance(balances, dict):
            raise self._error(502, None, "balances missing from the answer")
        out: dict[str, tuple[float, float]] = {}
        for asset, b in balances.items():
            if "value" not in b:
                raise self._error(502, None, f"balance of {asset} without value")
            total = num(b.get("value"))
            locked = num(b.get("locked"))
            canonical = _QUOTES.get(asset.upper(), asset.upper())
            out[canonical] = (max(0.0, total - locked), locked)
        return out

    # ---------------- orders ----------------
    def _normalize(self, o: dict) -> dict:
        raw_symbol = str(o.get("symbol", "")).upper()
        symbol = raw_symbol[:-3] + "IRT" if raw_symbol.endswith("TMN") else raw_symbol
        quote = "IRT" if symbol.endswith("IRT") else "USDT"
        base = symbol[: -len(quote)]
        side = str(o.get("side", "")).upper()
        executed = num(o.get("executedQty"))
        status = _STATUS.get(str(o.get("status", "")).upper())
        if status is None:  # FINISHED and other undocumented states
            status = "FILLED" if executed and executed >= num(o.get("origQty")) else (
                "NEW" if o.get("active") else "CANCELED")
        if status == "NEW" and executed > 0:
            status = "PARTIALLY_FILLED"
        handle = str(o.get("clientOrderId") or "")
        fee = num(o.get("fee"))
        fee_asset = ""
        fills = o.get("fills") or []
        if fills:
            fee_asset = _QUOTES.get(str(fills[0].get("feeAsset", "")).upper(), str(fills[0].get("feeAsset", "")).upper())
            fee = sum(num(f.get("fee")) for f in fills) or fee
        return common.order(
            symbol=symbol,
            order_id=handle,
            client_order_id=handle.lower(),
            side=side,
            order_type=_TYPES_IN.get(str(o.get("type", "")).upper(), "LIMIT"),
            status=status,
            price=o.get("price"),
            stop_price=o.get("stopPrice"),
            orig_qty=o.get("origQty"),
            executed_qty=executed,
            quote_qty=o.get("executedSum"),
            avg_price=o.get("executedPrice"),
            fee=fee,
            fee_asset=fee_asset or (base if side == "BUY" else quote),
            created_ms=_iso_ms(o.get("created_at")),
            updated_ms=_iso_ms(o.get("updated_at")),
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
        kind = _TYPES_OUT.get(order_type.upper())
        if kind is None:
            raise self._unsupported(f"order type {order_type}")
        if quantity is None:
            raise self._unsupported("quote-amount orders")
        if price is None:
            # The docs mark price required on every type; a market order gets
            # the last price, which the matching engine does not bind it to.
            price = num((await self.ticker_price(symbol))["price"])
        body: dict[str, Any] = {
            "symbol": self._wallex_symbol(symbol),
            "side": side.upper(),
            "type": kind,
            "price": fmt(price),
            "quantity": fmt(quantity),
        }
        if stop_price is not None and kind.startswith("STOP"):
            body["stop_Price"] = fmt(stop_price)   # sic: Wallex's casing
        if client_order_id:
            body["client_id"] = client_order_id.upper().replace("-", "_")
        try:
            answer = await self._request("POST", "/v1/account/orders", json=body, signed=True,
                                         attempts=1, throttle=self._order_throttle)
        except ExchangeError as exc:
            if exc.status == 0 or exc.status >= 500:
                return await self._lookup_after_ambiguous(symbol, client_order_id, exc)
            raise
        if not ((answer or {}).get("result") or {}).get("clientOrderId"):
            return await self._lookup_after_ambiguous(
                symbol, client_order_id, self._error(502, None, "order answer without a client id"))
        return self._normalize(answer["result"])

    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict:
        handle = str(order_id or client_order_id or "").upper().replace("-", "_")
        answer = await self._request("GET", f"/v1/account/orders/{handle}", signed=True)
        return self._normalize((answer or {}).get("result") or {})

    async def cancel_order(self, symbol: str, order_id: str | int, market: Market = "spot") -> dict:
        handle = str(order_id).upper().replace("-", "_")
        try:
            answer = await self._request("DELETE", f"/v1/account/orders/{handle}", signed=True)
        except ExchangeError as exc:
            try:
                current = await self.query_order(symbol, handle)
            except ExchangeError:
                raise exc from None
            if current["status"] in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                raise self._error(400, exc.code, f"order {handle} is {current['status']}", "unknown_order") from exc
            raise
        result = (answer or {}).get("result") or {"clientOrderId": handle, "status": "CANCELED", "symbol": symbol}
        return self._normalize(result)

    async def open_orders(self, symbol: str | None = None, market: Market = "spot") -> list:
        params: dict[str, Any] = {"per_page": 100}
        if symbol:
            params["symbol"] = self._wallex_symbol(symbol.upper())
        answer = await self._request("GET", "/v1/account/openOrders", params=params, signed=True)
        result = (answer or {}).get("result")
        orders: list[dict] = []
        # Documented as an object of arrays; tolerate a flat list too.
        if isinstance(result, dict):
            for value in result.values():
                if isinstance(value, list):
                    orders.extend(o for o in value if isinstance(o, dict))
        elif isinstance(result, list):
            orders = [o for o in result if isinstance(o, dict)]
        return [self._normalize(o) for o in orders]

    async def my_trades(self, symbol: str, order_id: str | int, market: Market = "spot") -> list[dict]:
        order = await self.query_order(symbol, order_id)
        return order.get("fills") or []


def _iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
