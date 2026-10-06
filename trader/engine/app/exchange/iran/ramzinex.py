"""Ramzinex (ramzinex.ir) spot connector - beta.

Written against the official OpenAPI file (https://docs.ramzinex.ir, read
leniently: the published JSON is malformed) and checked against public market
data. Not yet run with real money.

Auth: EXCHANGE_API_KEY + EXCHANGE_API_SECRET are exchanged for a token at
/auth/api_key/getToken; private calls send `Authorization2: Bearer <token>`
(sic) and `x-api-key`. Create the key with read + trade (+ cancel-order)
only - never withdraw.

Units: IRR markets are in **Rial**; the engine's IRT is the Toman, so prices
and Rial amounts are divided by 10 on the way in and multiplied on the way
out. Markets are addressed by numeric pair id.

What Ramzinex does not offer, and how the engine copes:
  * no stop orders -> `supports_stop_orders = False`: the engine's tick
    watchdog sells at market when a stop is reached (keep the engine running);
  * no client order ids (see below);
  * a market buy is sized in quote currency -> market orders are sent as
    marketable limit orders through the best price (a sell widens 1 % ->
    2.5 % -> 5 % until it fills, so a stop exit is not left behind); whatever
    has not filled after a few seconds is cancelled;
  * after an ambiguous placement the recent orders are searched for the same
    side and amount instead of sending the order again.

Candles: 30m buckets carry a +1 s offset (observed) and are floored; 1h and
4h bars are built from them because the exchange's own 4h is Tehran-aligned.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from decimal import Decimal
from typing import Any

from app.exchange.base import ExchangeError, Market
from app.exchange.iran import common
from app.exchange.iran.common import RestExchange, Throttle, fmt, num

log = logging.getLogger(__name__)

RIAL_PER_TOMAN = 10
PUBLIC = "https://publicapi.ramzinex.ir/exchange/api/v1.0/exchange"
PRIVATE = "https://api.ramzinex.ir/exchange/api/v1.0/exchange"
PAIRS = "https://api.ramzinex.ir/exchange/api/v2.0/exchange/pairs"
# Documented minimum: 100,000 Rial = 10,000 Toman. USDT side: conservative.
MIN_NOTIONAL = {"IRT": 10_000.0, "USDT": 5.0}

_STATES = {1: "NEW", 2: "CANCELED", 3: "FILLED", 4: "PARTIALLY_FILLED"}


class RamzinexClient(RestExchange):
    name = "ramzinex"
    title = "Ramzinex"
    title_fa = "رمزینکس"
    base_url = PRIVATE
    supports_stop_orders = False

    native_intervals = {"1m": "1", "5m": "5", "15m": "15", "30m": "30"}
    built_from = {"1h": "30m", "4h": "30m"}
    candles_per_request = 500

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.public_throttle = Throttle(60)
        self._token = ""
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        self._currencies: dict[int, str] = {}
        # ids this process placed recently: an ambiguous-placement lookup
        # must never adopt one of them as the order it is looking for
        self._placed: list[str] = []

    # ---------------- auth ----------------
    async def _get_token(self) -> str:
        if self._token and self._token_exp - time.time() > 60:
            return self._token
        async with self._token_lock:
            if self._token and self._token_exp - time.time() > 60:
                return self._token
            if not self.api_secret:
                raise self._error(401, None, "Ramzinex needs EXCHANGE_API_KEY and EXCHANGE_API_SECRET", "auth")
            answer = await self._request("POST", f"{PRIVATE}/auth/api_key/getToken",
                                         json={"api_key": self.api_key, "secret": self.api_secret}, attempts=2)
            token = ((answer or {}).get("data") or {}).get("token") or ""
            if not token:
                raise self._error(401, None, "Ramzinex returned no token", "auth")
            self._token = token
            try:
                payload = token.split(".")[1]
                self._token_exp = float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
            except Exception:  # noqa: BLE001 - undocumented lifetime: renew often
                self._token_exp = time.time() + 900
            return token

    async def _auth_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        return {"Authorization2": f"Bearer {await self._get_token()}", "x-api-key": self.api_key}

    def _is_error_body(self, body: Any) -> bool:
        return isinstance(body, dict) and isinstance(body.get("status"), int) and body["status"] < 0

    def _classify(self, status: int, body: Any, text: str) -> tuple[Any, str, str | None]:
        code, message, kind = super()._classify(status, body, text)
        if isinstance(body, dict):
            code = body.get("status", code)
            desc = body.get("description")
            if isinstance(desc, dict):
                message = str(desc.get("en") or desc.get("fa") or message)
            if code == -3:
                kind = "unknown_order"   # "the order is not open"
        return code, message, kind

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        try:
            return await super()._request(method, path, **kwargs)
        except ExchangeError as exc:
            if exc.status == 401 and kwargs.get("signed"):
                self._token, self._token_exp = "", 0.0
                return await super()._request(method, path, **kwargs)
            raise

    # ---------------- symbols and units ----------------
    async def _fetch_markets(self) -> dict[str, dict]:
        body = await self._request("GET", PAIRS)
        data = (body or {}).get("data")
        pairs = data.get("pairs") if isinstance(data, dict) else data
        markets: dict[str, dict] = {}
        for p in pairs or []:
            base_cur, quote_cur = p.get("base_currency") or {}, p.get("quote_currency") or {}
            base = str((base_cur.get("symbol") or {}).get("en", "")).upper()
            raw_quote = str((quote_cur.get("symbol") or {}).get("en", "")).upper()
            quote = {"IRR": "IRT", "USDT": "USDT"}.get(raw_quote)
            if not base or not quote:
                continue
            self._currencies[int(base_cur.get("id", -1))] = base
            self._currencies[int(quote_cur.get("id", -1))] = quote
            tick = num(common.step_from_precision(p.get("price_precision")))
            markets[base + quote] = {
                "base": base,
                "quote": quote,
                # A Rial tick of 1 is a Toman tick of 0.1.
                "tick": tick / RIAL_PER_TOMAN if quote == "IRT" else tick,
                "step": common.step_from_precision(base_cur.get("precision")),
                "min_qty": common.step_from_precision(base_cur.get("precision")),
                "min_notional": MIN_NOTIONAL[quote],
                "status": "BREAK" if p.get("is_temporarily_suspended") else "TRADING",
                "pair_id": int(p["id"]),
                "chart": str(((p.get("trading_chart_settings") or {}).get("ramzinex")) or "").upper(),
                "base_id": int(base_cur.get("id", -1)),
                "quote_id": int(quote_cur.get("id", -1)),
            }
        return markets

    @staticmethod
    def _to_toman(value: Any, quote: str) -> float:
        v = num(value)
        return v / RIAL_PER_TOMAN if quote == "IRT" else v

    @staticmethod
    def _from_toman(value: float, quote: str) -> float:
        return value * RIAL_PER_TOMAN if quote == "IRT" else value

    # ---------------- market data ----------------
    async def _fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        m = await self.market(symbol)
        body = await self._request(
            "GET", f"{PUBLIC}/chart/tv/history",
            params={"symbol": m["chart"] or symbol.replace("IRT", "IRR"),
                    "resolution": self.native_intervals[interval],
                    "from": start_ms // 1000, "to": end_ms // 1000},
            throttle=self.public_throttle,
        )
        if not isinstance(body, dict) or body.get("s") != "ok":
            return []
        step = common.interval_ms(interval)
        q = m["quote"]
        rows = []
        for t, o, h, l, c, v in zip(body["t"], body["o"], body["h"], body["l"], body["c"], body["v"]):
            open_ms = int(t) * 1000
            open_ms -= open_ms % step   # observed: +1 s past the boundary
            rows.append(common.kline_row(
                open_ms, interval, fmt(self._to_toman(o, q), 8), fmt(self._to_toman(h, q), 8),
                fmt(self._to_toman(l, q), 8), fmt(self._to_toman(c, q), 8), v,
            ))
        return rows

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        m = await self.market(symbol)
        body = await self._request("GET", f"{PUBLIC}/orderbooks/{m['pair_id']}/buys_sells",
                                   throttle=self.public_throttle)
        data = (body or {}).get("data") or {}
        q = m["quote"]

        def side(rows: list, best_high: bool) -> list[list[str]]:
            out = [[fmt(self._to_toman(r[0], q)), fmt(r[1])] for r in rows or [] if len(r) >= 2]
            out.sort(key=lambda r: num(r[0]), reverse=best_high)
            return out[:limit]

        return {"bids": side(data.get("buys"), True), "asks": side(data.get("sells"), False)}

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        if symbol is None:
            raise self._unsupported("all-symbol ticker")
        book = await self.order_book(symbol, limit=1)
        if not book["bids"] and not book["asks"]:
            raise self._error(404, None, f"no price for {symbol}", "invalid_symbol")
        prices = [num(book[s][0][0]) for s in ("bids", "asks") if book[s]]
        return {"symbol": symbol.upper(), "price": fmt(sum(prices) / len(prices))}

    # ---------------- account ----------------
    async def _balances(self) -> dict[str, tuple[float, float]]:
        await self.load_markets()
        body = await self._request("GET", "/users/me/funds/summaryDesktop", signed=True)
        data = (body or {}).get("data")
        if not isinstance(data, list):
            # An unreadable wallet is unknown, never zero: zero would orphan
            # every position and read as a withdrawal.
            raise self._error(502, None, "balance summary without data")
        out: dict[str, tuple[float, float]] = {}
        for row in data:
            asset = self._currencies.get(int(row.get("currency_id", -1)))
            if not asset:
                continue
            if "total_nr" not in row:
                raise self._error(502, None, f"balance row without total_nr: {sorted(row)}")
            total, in_orders = num(row.get("total_nr")), num(row.get("in_order_nr"))
            if asset == "IRT":
                total, in_orders = total / RIAL_PER_TOMAN, in_orders / RIAL_PER_TOMAN
            out[asset] = (max(0.0, total - in_orders), in_orders)
        return out

    # ---------------- orders ----------------
    async def _normalize(self, o: dict) -> dict:
        markets = await self.load_markets()
        pair_id = int(o.get("pair_id") or (o.get("pair_obj") or {}).get("pair_id") or -1)
        m = next((v for v in markets.values() if v["pair_id"] == pair_id), None)
        symbol = (m["base"] + m["quote"]) if m else str(pair_id)
        quote = m["quote"] if m else "USDT"
        base = m["base"] if m else ""
        side = "BUY" if str(o.get("type_en", "")).lower() == "buy" else "SELL"
        executed = num(o.get("filled_nr"))
        status = _STATES.get(int(o.get("status_id") or (o.get("status") or {}).get("id") or 1), "NEW")
        if status == "NEW" and executed > 0:
            status = "PARTIALLY_FILLED"
        fee = num(o.get("commission_nr"))
        fee_asset = self._currencies.get(int(o.get("commission_currency_id") or -1), "")
        if fee_asset == "IRT":
            fee /= RIAL_PER_TOMAN
        created = num(o.get("created_at_ms"))
        created_ms = int(created * 1000) if created and created < 1e11 else int(created)
        kind = str(((o.get("type_v2_all_lang") or {}).get("en")) or "limit").lower()
        return common.order(
            symbol=symbol,
            order_id=o.get("id"),
            client_order_id=None,
            side=side,
            order_type="MARKET" if kind == "market" else "LIMIT",
            status=status,
            price=self._to_toman(o.get("order_price_nr"), quote),
            orig_qty=o.get("amount_nr"),
            executed_qty=executed,
            avg_price=self._to_toman(o.get("average_price_nr"), quote),
            fee=fee,
            fee_asset=fee_asset or (base if side == "BUY" else quote),
            created_ms=created_ms or None,
            raw={k: v for k, v in o.items() if k != "pair_obj"},
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
        kind = order_type.upper()
        if kind not in ("MARKET", "LIMIT"):
            raise self._unsupported(f"order type {order_type} (no stop orders on Ramzinex)")
        if quantity is None:
            raise self._unsupported("quote-amount orders")
        symbol = symbol.upper()
        if kind == "MARKET":
            return await self._marketable_order(symbol, side, quantity, client_order_id)
        if price is None:
            raise self._unsupported("a limit order without a price")
        m = await self.market(symbol)
        buy = side.upper() == "BUY"
        order_id = await self._place_limit(
            symbol, side, common.snap(quantity, m["step"]), common.snap(price, m["tick"], up=buy), None
        )
        try:
            return await self.query_order(symbol, order_id)
        except ExchangeError:
            return {"symbol": symbol, "orderId": str(order_id), "status": "NEW", "executedQty": "0",
                    "cummulativeQuoteQty": "0", "type": "LIMIT", "side": side.upper(), "fills": []}

    async def _place_limit(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal, client_order_id: str | None
    ) -> str:
        m = await self.market(symbol)
        buy = side.upper() == "BUY"
        rial_price = price * RIAL_PER_TOMAN if m["quote"] == "IRT" else price
        body = {
            "pair_id": m["pair_id"],
            "amount": common.jnum(quantity),
            "price": common.jnum(rial_price),
            "type": "buy" if buy else "sell",
        }
        sent_at = time.time()
        try:
            answer = await self._request("POST", "/users/me/orders/limit", json=body, signed=True, attempts=1)
        except ExchangeError as exc:
            if exc.status == 0 or exc.status >= 500:
                # No client ids on Ramzinex: find the order we may have placed
                # instead of letting the engine send it a second time.
                found = await self._find_recent(m["pair_id"], buy, float(quantity), sent_at)
                if found is not None:
                    self._placed = (self._placed + [found])[-50:]
                    return found
            raise
        order_id = ((answer or {}).get("data") or {}).get("order_id")
        if order_id is None:
            found = await self._find_recent(m["pair_id"], buy, float(quantity), sent_at)
            if found is None:
                raise self._error(502, None, "order answer without an id")
            order_id = found
        self._placed = (self._placed + [str(order_id)])[-50:]
        return str(order_id)

    async def _find_recent(self, pair_id: int, buy: bool, amount: float, since: float) -> str | None:
        """The id of an order on `pair_id` with this side and amount placed
        since `since` (seconds), or None. Used after an ambiguous placement."""
        try:
            answer = await self._request(
                "POST", "/users/me/orders3",
                json={"offset": 0, "limit": 20, "pairs": [pair_id], "states": [1, 2, 3, 4]},
                signed=True,
            )
        except ExchangeError as exc:
            log.warning("ramzinex: could not look up a just-placed order: %s", exc)
            return None
        best: tuple[float, str] | None = None
        for o in (answer or {}).get("data") or []:
            created = num(o.get("created_at_ms"))
            created_s = created / 1000 if created > 1e11 else created
            state = int(o.get("status_id") or (o.get("status") or {}).get("id") or 0)
            if (
                str(o.get("id")) not in self._placed
                and state != 2                                  # not a cancelled attempt
                and str(o.get("type_en", "")).lower() == ("buy" if buy else "sell")
                and abs(num(o.get("amount_nr")) - amount) <= max(1e-12, amount * 1e-6)
                and created_s >= since - 10
                and (best is None or created_s > best[0])
            ):
                best = (created_s, str(o.get("id")))
        return best[1] if best else None

    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict:
        if order_id is None:
            raise self._error(404, None, "Ramzinex has no client order ids", "unknown_order")
        answer = await self._request("GET", f"/users/me/orders2/{order_id}", signed=True)
        data = (answer or {}).get("data")
        if not data:
            raise self._error(404, None, f"order {order_id} not found", "unknown_order")
        return await self._normalize(data)

    async def cancel_order(self, symbol: str, order_id: str | int, market: Market = "spot") -> dict:
        await self._request("POST", f"/users/me/orders/{order_id}/cancel", json={}, signed=True)
        return {"symbol": symbol.upper(), "orderId": str(order_id), "status": "CANCELED"}

    async def open_orders(self, symbol: str | None = None, market: Market = "spot") -> list:
        body: dict[str, Any] = {"offset": 0, "limit": 100, "states": [1, 4]}
        if symbol:
            body["pairs"] = [(await self.market(symbol.upper()))["pair_id"]]
        answer = await self._request("POST", "/users/me/orders3", json=body, signed=True)
        return [await self._normalize(o) for o in (answer or {}).get("data") or []]
