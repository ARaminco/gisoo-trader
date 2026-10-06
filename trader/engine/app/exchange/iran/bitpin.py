"""Bitpin (bitpin.ir) spot connector - beta.

Written against the official docs (https://docs.bitpin.ir/v1, mirrored at
docs.bitpin.org) and checked against public market data. Not yet run with
real money.

Auth: EXCHANGE_API_KEY + EXCHANGE_API_SECRET are exchanged for JWTs at
POST /api/v1/usr/authenticate/ (access ~15 min, refresh longer). Both token
endpoints are limited to 200 calls a day, so the tokens are shared by every
worker through Redis and refreshed only when needed. Bitpin binds a key to
the IP list given when it was created: the server's IP must be on it.

Units: IRT markets are in Toman (`BTC_IRT`), no conversion.

Candles: `res=4h` buckets are Tehran-aligned (observed); 30m buckets are
UTC-aligned, so 1h and 4h bars are built from 30m.

Stops: `stop_limit` and `oco` exist; there is no stop-market. The engine's
protective stop is a stop-limit with a small buffer, which fits.

Unverified in the docs: minimum order size (a conservative floor is used),
the fiat wallet label (RIAL / IRT, both read as Toman) and key permission
options - create the key without withdrawal rights if the panel offers it.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import time
from typing import Any

from app.exchange.base import ExchangeError, Market
from app.exchange.iran import common
from app.exchange.iran.common import RestExchange, Throttle, fmt, num

log = logging.getLogger(__name__)

TOKEN_KEY = "gtr:bitpin:tokens"
TOKEN_LOCK = "gtr:bitpin:tokens:lock"
# The token endpoints allow 200 calls a day: after a failed authentication
# nothing asks again for this long, and a 401 on a signed call re-authenticates
# at most this often.
AUTH_BACKOFF_SECONDS = 900
AUTH_RETRY_SECONDS = 60       # after a timeout / 5xx / 429 from authenticate
REAUTH_MIN_SECONDS = 300
# Not documented; conservative floors so a too-small order is refused here.
MIN_NOTIONAL = {"IRT": 100_000.0, "USDT": 5.0}

_TYPES_OUT = {"MARKET": "market", "LIMIT": "limit", "STOP_LOSS_LIMIT": "stop_limit"}
_TYPES_IN = {"market": "MARKET", "limit": "LIMIT", "stop_limit": "STOP_LOSS_LIMIT", "oco": "OCO"}


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data.get("exp") or 0)
    except Exception:  # noqa: BLE001 - unknown expiry: assume a short life
        return time.time() + 600


class BitpinClient(RestExchange):
    name = "bitpin"
    title = "Bitpin"
    title_fa = "بیت‌پین"
    base_url = "https://api.bitpin.org"
    supports_stop_orders = True

    native_intervals = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m"}
    built_from = {"1h": "30m", "4h": "30m"}
    candles_per_request = 2000

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.public_throttle = Throttle(240)    # 300/min/IP
        self._private_throttle = Throttle(70)   # orders 80/min
        self._tokens: dict[str, Any] = {}
        self._token_lock = asyncio.Lock()
        self._auth_blocked_until = 0.0
        self._last_forced_reauth = 0.0

    # ---------------- auth ----------------
    async def _shared(self) -> dict[str, Any]:
        try:
            from app.workers import bus

            raw = await bus.get_redis().get(TOKEN_KEY)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001 - no Redis (tests, scripts): memory only
            pass
        return dict(self._tokens)

    async def _store(self, tokens: dict[str, Any]) -> None:
        self._tokens = tokens
        try:
            from app.workers import bus

            ttl = max(60, int(tokens.get("refresh_exp", 0) - time.time()))
            await bus.get_redis().set(TOKEN_KEY, json.dumps(tokens), ex=ttl)
        except Exception:  # noqa: BLE001
            pass

    async def _cross_process_lock(self) -> bool:
        """Best effort: one process at a time refreshes (Redis SET NX)."""
        try:
            from app.workers import bus

            for _ in range(20):
                if await bus.get_redis().set(TOKEN_LOCK, "1", nx=True, ex=30):
                    return True
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001 - no Redis: this process only
            pass
        return False

    async def _release_lock(self, held: bool) -> None:
        if not held:
            return
        try:
            from app.workers import bus

            await bus.get_redis().delete(TOKEN_LOCK)
        except Exception:  # noqa: BLE001
            pass

    async def _access_token(self) -> str:
        tokens = await self._shared()
        if tokens.get("access") and tokens.get("access_exp", 0) - time.time() > 60:
            return tokens["access"]
        if time.time() < self._auth_blocked_until:
            raise self._error(401, None, "Bitpin authentication failed recently; waiting before retrying", "auth")
        async with self._token_lock:
            held = await self._cross_process_lock()
            try:
                return await self._renew()
            finally:
                await self._release_lock(held)

    async def _renew(self) -> str:
        tokens = await self._shared()
        if tokens.get("access") and tokens.get("access_exp", 0) - time.time() > 60:
            return tokens["access"]
        if tokens.get("refresh") and tokens.get("refresh_exp", 0) - time.time() > 120:
            try:
                answer = await self._request("POST", "/api/v1/usr/refresh_token/",
                                             json={"refresh": tokens["refresh"]}, attempts=1)
                access = (answer or {}).get("access")
                if access:
                    tokens = {**tokens, "access": access, "access_exp": _jwt_exp(access)}
                    await self._store(tokens)
                    return access
            except ExchangeError as exc:
                log.warning("bitpin token refresh failed (%s); authenticating again", exc)
        if not self.api_secret:
            raise self._error(401, None, "Bitpin needs EXCHANGE_API_KEY and EXCHANGE_API_SECRET", "auth")
        try:
            answer = await self._request("POST", "/api/v1/usr/authenticate/",
                                         json={"api_key": self.api_key, "secret_key": self.api_secret},
                                         attempts=1)
        except ExchangeError as exc:
            # Rejected credentials wait long (they will not fix themselves and
            # each try costs from a 200-a-day allowance); an outage waits briefly.
            rejected = exc.status in (400, 401, 403)
            self._auth_blocked_until = time.time() + (AUTH_BACKOFF_SECONDS if rejected else AUTH_RETRY_SECONDS)
            raise
        access, refresh = (answer or {}).get("access"), (answer or {}).get("refresh")
        if not access or not refresh:
            self._auth_blocked_until = time.time() + AUTH_BACKOFF_SECONDS
            raise self._error(401, None, "Bitpin authentication answered without tokens", "auth")
        tokens = {
            "access": access, "access_exp": _jwt_exp(access),
            "refresh": refresh, "refresh_exp": _jwt_exp(refresh),
        }
        await self._store(tokens)
        return access

    async def _auth_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token()}"}

    def _classify(self, status: int, body: Any, text: str) -> tuple[Any, str, str | None]:
        code, message, kind = super()._classify(status, body, text)
        lowered = message.lower()
        if status == 406 or "not allowed" in lowered:
            kind = "unknown_order"
        elif "balance" in lowered:
            kind = "insufficient_balance"
        elif "throttled" in lowered:
            kind = "rate_limit"
        return code, message, kind

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        try:
            return await super()._request(method, path, **kwargs)
        except ExchangeError as exc:
            # An access token revoked early: drop it and try once more - but
            # not over and over: a 401 that persists (the IP is not on the
            # key's list) must not burn the daily token allowance.
            if (
                exc.status == 401
                and kwargs.get("signed")
                and time.time() - self._last_forced_reauth > REAUTH_MIN_SECONDS
            ):
                self._last_forced_reauth = time.time()
                await self._store({**(await self._shared()), "access": "", "access_exp": 0})
                return await super()._request(method, path, **kwargs)
            raise

    # ---------------- symbols ----------------
    @staticmethod
    def _bitpin_symbol(symbol: str) -> str:
        for quote in ("USDT", "IRT"):
            if symbol.endswith(quote):
                return f"{symbol[:-len(quote)]}_{quote}"
        raise ExchangeError(400, None, f"{symbol}: quote must be USDT or IRT", exchange="bitpin",
                            kind="invalid_symbol")

    async def _fetch_markets(self) -> dict[str, dict]:
        rows = await self._request("GET", "/api/v1/mkt/markets/", throttle=self.public_throttle)
        markets: dict[str, dict] = {}
        for m in rows or []:
            base, quote = str(m.get("base", "")).upper(), str(m.get("quote", "")).upper()
            if quote not in ("IRT", "USDT") or not base:
                continue
            step = common.step_from_precision(m.get("base_amount_precision"))
            markets[base + quote] = {
                "base": base,
                "quote": quote,
                "tick": common.step_from_precision(m.get("price_precision")),
                "step": step,
                "min_qty": step,
                "min_notional": MIN_NOTIONAL[quote],
                "status": "TRADING" if m.get("tradable", True) and not m.get("suspended") else "BREAK",
            }
        return markets

    # ---------------- market data ----------------
    async def _fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        rows = await self._request(
            "GET", "/api/v1/mkt/candles/",
            params={"symbol": self._bitpin_symbol(symbol), "from": start_ms // 1000,
                    "to": end_ms // 1000, "res": self.native_intervals[interval]},
            throttle=self.public_throttle,
        )
        step = common.interval_ms(interval)
        out = []
        for c in rows or []:
            if c.get("resolution") and c["resolution"] != self.native_intervals[interval]:
                # Unknown resolutions silently fall back to 30m: never trust it.
                continue
            open_ms = int(num(c.get("ts")) * 1000)
            if open_ms % step:
                continue
            out.append(common.kline_row(open_ms, interval, c.get("open"), c.get("high"), c.get("low"),
                                        c.get("close"), c.get("volume")))
        return out

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        if symbol is None:
            raise self._unsupported("all-symbol ticker")
        book = await self.order_book(symbol, limit=1)
        bid, ask = book["bids"][0][0] if book["bids"] else None, book["asks"][0][0] if book["asks"] else None
        if bid is None and ask is None:
            raise self._error(404, None, f"no price for {symbol}", "invalid_symbol")
        price = (num(bid) + num(ask)) / 2 if bid and ask else num(bid or ask)
        return {"symbol": symbol.upper(), "price": fmt(price)}

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        body = await self._request("GET", f"/api/v1/mth/orderbook/{self._bitpin_symbol(symbol.upper())}/",
                                   throttle=self.public_throttle)
        return {"bids": [list(map(str, r)) for r in (body.get("bids") or [])[:limit]],
                "asks": [list(map(str, r)) for r in (body.get("asks") or [])[:limit]]}

    # ---------------- account ----------------
    async def _balances(self) -> dict[str, tuple[float, float]]:
        rows = await self._request("GET", "/api/v1/wlt/wallets/", params={"service": "main", "limit": 200},
                                   signed=True, throttle=self._private_throttle)
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        if not isinstance(rows, list):
            raise self._error(502, None, "wallets missing from the answer")
        out: dict[str, tuple[float, float]] = {}
        for w in rows:
            if "balance" not in w:
                raise self._error(502, None, "wallet row without balance")
            asset = str(w.get("asset", "")).upper()
            if asset in ("RIAL", "TMN", "TOMAN"):
                asset = "IRT"
            if not asset:
                continue
            free, frozen = num(w.get("balance")), num(w.get("frozen"))
            prev = out.get(asset, (0.0, 0.0))
            out[asset] = (prev[0] + free, prev[1] + frozen)
        return out

    # ---------------- orders ----------------
    def _normalize(self, o: dict) -> dict:
        raw_symbol = str(o.get("symbol", ""))
        base, _, quote = raw_symbol.partition("_")
        side = str(o.get("side", "")).upper()
        state = str(o.get("state", "")).lower()
        amount = num(o.get("base_amount"))
        executed = num(o.get("dealed_base_amount"))
        if state == "closed":
            status = "FILLED" if amount and executed >= amount * 0.999999 else "CANCELED"
        elif executed > 0:
            status = "PARTIALLY_FILLED"
        else:
            status = "NEW"
        return common.order(
            symbol=base.upper() + quote.upper(),
            order_id=o.get("id"),
            client_order_id=o.get("identifier"),
            side=side,
            order_type=_TYPES_IN.get(str(o.get("type", "")).lower(), "LIMIT"),
            status=status,
            price=o.get("price"),
            stop_price=o.get("stop_price"),
            orig_qty=amount,
            executed_qty=executed,
            quote_qty=o.get("dealed_quote_amount"),
            fee=o.get("commission"),
            # Fee currency is on the fills; on an order the received asset is the best guess.
            fee_asset=base.upper() if side == "BUY" else quote.upper(),
            created_ms=_iso_ms(o.get("created_at")),
            updated_ms=_iso_ms(o.get("closed_at")),
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
        body: dict[str, Any] = {"symbol": self._bitpin_symbol(symbol), "type": kind, "side": side.lower()}
        if quantity is not None:
            body["base_amount"] = fmt(quantity)
        elif quote_quantity is not None and kind == "market":
            body["quote_amount"] = fmt(quote_quantity)
        else:
            raise self._unsupported("an order without an amount")
        if price is not None and kind != "market":
            body["price"] = fmt(price)
        if stop_price is not None:
            body["stop_price"] = fmt(stop_price)
        if client_order_id:
            body["identifier"] = client_order_id
        try:
            answer = await self._request("POST", "/api/v1/odr/orders/", json=body, signed=True, attempts=1)
        except ExchangeError as exc:
            if exc.status == 0 or exc.status >= 500:
                return await self._lookup_after_ambiguous(symbol, client_order_id, exc)
            raise
        if not (answer or {}).get("id"):
            return await self._lookup_after_ambiguous(
                symbol, client_order_id, self._error(502, None, "order answer without an id"))
        return self._normalize(answer)

    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict:
        if order_id is not None:
            answer = await self._request("GET", f"/api/v1/odr/orders/{order_id}/", signed=True,
                                         throttle=self._private_throttle)
            return self._normalize(answer or {})
        rows = await self._request(
            "GET", "/api/v1/odr/orders/",
            params={"symbol": self._bitpin_symbol(symbol.upper()), "identifier": client_order_id, "limit": 10},
            signed=True, throttle=self._private_throttle,
        )
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        for o in rows or []:
            if o.get("identifier") == client_order_id:
                return self._normalize(o)
        raise self._error(404, None, f"order {client_order_id} not found", "unknown_order")

    async def cancel_order(self, symbol: str, order_id: str | int, market: Market = "spot") -> dict:
        await self._request("DELETE", f"/api/v1/odr/orders/{order_id}/", signed=True)
        return {"symbol": symbol.upper(), "orderId": str(order_id), "status": "CANCELED"}

    async def open_orders(self, symbol: str | None = None, market: Market = "spot") -> list:
        params: dict[str, Any] = {"state": "active", "limit": 100}
        if symbol:
            params["symbol"] = self._bitpin_symbol(symbol.upper())
        rows = await self._request("GET", "/api/v1/odr/orders/", params=params, signed=True,
                                   throttle=self._private_throttle)
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        orders = [self._normalize(o) for o in rows or []]
        # "initial" (a stop not triggered yet) is open too, and the filter
        # above only asks for "active": fetch those as well.
        params["state"] = "initial"
        more = await self._request("GET", "/api/v1/odr/orders/", params=params, signed=True,
                                   throttle=self._private_throttle)
        more = more.get("results", more) if isinstance(more, dict) else more
        orders.extend(self._normalize(o) for o in more or [])
        return orders

    async def my_trades(self, symbol: str, order_id: str | int, market: Market = "spot") -> list[dict]:
        rows = await self._request("GET", "/api/v1/odr/fills/",
                                   params={"symbol": self._bitpin_symbol(symbol.upper()), "limit": 100},
                                   signed=True, throttle=self._private_throttle)
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        fills = []
        for f in rows or []:
            if str(f.get("order_id")) != str(order_id):
                continue
            asset = str(f.get("commission_currency", "")).upper()
            fills.append({
                "price": fmt(f.get("price")),
                "qty": fmt(f.get("base_amount")),
                "commission": fmt(f.get("commission")),
                "commissionAsset": "IRT" if asset in ("RIAL", "TMN", "TOMAN") else asset,
            })
        return fills


def _iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
