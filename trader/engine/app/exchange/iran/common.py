"""Shared plumbing for the Iranian exchange connectors.

Each connector only knows its own exchange: URLs, auth headers, symbol
spelling, units and field names. Everything they have in common lives here -
the HTTP client and its retry rules, the translation of their answers into the
Binance-shaped dictionaries the engine reads (see app/exchange/base.py), and
the arithmetic for intervals, Toman/Rial and order states.

Ground rules every connector follows:

* An order is sent exactly once. A timeout or a 5xx on placement means
  "unknown", not "failed": the connector looks the order up by its client id
  (or recent orders) instead of sending it again. Once anything was placed, a
  connector never raises: it returns the last state it knows.
* Prices and amounts are snapped to the exchange grid in Decimal and cross
  the boundary as plain decimal strings (no float noise); floats inside the
  engine.
* Spot only: a futures / short request is refused, never sent as a spot sell.
* IRT is Toman inside the engine. A connector whose exchange speaks Rial
  multiplies prices by 10 on the way out and divides on the way in.
"""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import settings
from app.exchange.base import ExchangeClient, ExchangeError, Market

log = logging.getLogger(__name__)

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}

USER_AGENT = "GisooTrader/0.1 (+https://github.com/ARaminco/gisoo-trader)"


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval] * 1000
    except KeyError:
        raise ExchangeError(0, None, f"unsupported interval {interval!r}", kind="unsupported") from None


def fmt(value: float | Decimal | str | None, places: int | None = None) -> str | None:
    """A number as a plain decimal string, no exponent, trailing zeros trimmed."""
    if value is None:
        return None
    d = Decimal(str(value))
    if places is not None:
        d = round(d, places)
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def snap(value: Any, step: Any, *, up: bool = False) -> Decimal:
    """`value` on the `step` grid, exactly (Decimal), rounded down or up."""
    d = Decimal(str(value))
    st = Decimal(str(step or 0))
    if st <= 0:
        return d
    return (d / st).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * st


def jnum(value: Decimal) -> int | float:
    """A Decimal for a JSON body: an int when integral, else the shortest float."""
    return int(value) if value == value.to_integral_value() else float(value)


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def kline_row(open_ms: int, interval: str, o: Any, h: Any, l: Any, c: Any, v: Any) -> list:
    """One Binance-shaped kline. Exchanges that do not report quote volume,
    trade count or taker volume get estimates/zeros there; the strategies
    only read OHLCV."""
    close_ms = open_ms + interval_ms(interval) - 1
    volume = num(v)
    close = num(c)
    return [
        int(open_ms), str(o), str(h), str(l), str(c), str(v), close_ms,
        str(volume * close), 0, "0",
    ]


def window(
    interval: str, limit: int, start_time: int | None, end_time: int | None
) -> tuple[int, int]:
    """(from_ms, to_ms) covering `limit` bars the way Binance pages: from
    `start_time` forwards, else back from `end_time` (or now)."""
    step = interval_ms(interval)
    limit = max(1, min(limit, 1000))
    if start_time is not None:
        start = int(start_time) - int(start_time) % step
        end = start + step * limit - 1
        if end_time is not None:
            end = min(end, int(end_time))
        return start, end
    end = int(end_time) if end_time is not None else int(time.time() * 1000)
    start = end - step * limit
    return start - start % step, end


def trim(rows: list[list], limit: int, start_time: int | None, end_time: int | None) -> list[list]:
    """Sort, de-duplicate and cut to the Binance paging contract."""
    seen: dict[int, list] = {}
    for row in rows:
        seen[int(row[0])] = row
    out = [seen[k] for k in sorted(seen)]
    if start_time is not None:
        # Binance: bars whose open time is at or after startTime, oldest first.
        out = [r for r in out if int(r[0]) >= int(start_time)]
        if end_time is not None:
            out = [r for r in out if int(r[0]) <= int(end_time)]
        out = out[:limit]
    else:
        if end_time is not None:
            out = [r for r in out if int(r[0]) <= int(end_time)]
        out = out[-limit:]
    return out


def aggregate(rows: list[list], interval: str) -> list[list]:
    """Fold smaller UTC-aligned bars into `interval` bars, also UTC-aligned.

    Several Iranian exchanges align 1h and 4h candles to Tehran time
    (UTC+3:30), while the 4h core is defined on UTC bars (the momentum
    rebalance is the bar that opens at 00:00 UTC). Their 30m candles are
    UTC-aligned, so the connectors fetch those and build 4h bars here. A
    bucket's close time is its UTC end, so the bar still forming is dropped
    by the ingest layer exactly like Binance's.
    """
    step = interval_ms(interval)
    buckets: dict[int, list] = {}
    for r in sorted(rows, key=lambda row: int(row[0])):
        key = int(r[0]) - int(r[0]) % step
        o, h, l, c = num(r[1]), num(r[2]), num(r[3]), num(r[4])
        v, qv = num(r[5]), num(r[7])
        b = buckets.get(key)
        if b is None:
            buckets[key] = [key, o, h, l, c, v, key + step - 1, qv]
        else:
            b[2] = max(b[2], h)
            b[3] = min(b[3], l)
            b[4] = c
            b[5] += v
            b[7] += qv
    return [
        [k, fmt(b[1]), fmt(b[2]), fmt(b[3]), fmt(b[4]), fmt(b[5]), b[6], fmt(b[7]), 0, "0"]
        for k, b in sorted(buckets.items())
    ]


def chunks(start_ms: int, end_ms: int, step_ms: int, per_request: int) -> list[tuple[int, int]]:
    """Split [start, end] into ranges of at most `per_request` source bars."""
    span = step_ms * per_request
    out = []
    a = start_ms
    while a <= end_ms:
        b = min(end_ms, a + span - 1)
        out.append((a, b))
        a = b + 1
    return out


class Throttle:
    """Spaces requests to stay under an exchange's per-minute limit."""

    def __init__(self, per_minute: int):
        self.gap = 60.0 / max(1, per_minute)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap


def filters(tick: Any, step: Any, min_qty: Any = None, min_notional: Any = None) -> list[dict]:
    """Binance-style filter list for SymbolRegistry."""
    out = [
        {"filterType": "PRICE_FILTER", "tickSize": fmt(tick) or "0.00000001"},
        {"filterType": "LOT_SIZE", "stepSize": fmt(step) or "0.00000001",
         "minQty": fmt(min_qty if min_qty is not None else step) or "0", "maxQty": "1000000000"},
    ]
    if min_notional is not None:
        out.append({"filterType": "NOTIONAL", "minNotional": fmt(min_notional) or "0"})
    return out


def step_from_precision(precision: Any, default: str = "0.00000001") -> str:
    """10^-precision as a string; `default` when precision is missing."""
    try:
        p = int(precision)
    except (TypeError, ValueError):
        return default
    if p <= 0:
        return "1"
    return "0." + "0" * (p - 1) + "1"


def order(
    *,
    symbol: str,
    order_id: Any,
    client_order_id: str | None,
    side: str,
    order_type: str,
    status: str,
    price: Any = None,
    stop_price: Any = None,
    orig_qty: Any = None,
    executed_qty: Any = 0,
    quote_qty: Any = None,
    avg_price: Any = None,
    fee: Any = None,
    fee_asset: str | None = None,
    created_ms: Any = None,
    updated_ms: Any = None,
    raw: dict | None = None,
) -> dict:
    """A Binance-shaped order dict, with one aggregated fill when anything filled."""
    executed = num(executed_qty)
    avg = num(avg_price)
    cumulative = num(quote_qty) if quote_qty not in (None, "") else executed * avg
    if not avg and executed:
        avg = cumulative / executed if cumulative else num(price)
    fills = []
    if executed > 0:
        fills.append({
            "price": fmt(avg) or "0",
            "qty": fmt(executed) or "0",
            "commission": fmt(num(fee)) or "0",
            "commissionAsset": fee_asset or "",
        })
    return {
        "symbol": symbol,
        "orderId": str(order_id) if order_id is not None else None,
        "clientOrderId": client_order_id or "",
        "side": side.upper(),
        "type": order_type,
        "status": status,
        "price": fmt(price) or "0",
        "stopPrice": fmt(stop_price) or "0",
        "origQty": fmt(orig_qty) or "0",
        "executedQty": fmt(executed) or "0",
        "cummulativeQuoteQty": fmt(cumulative) or "0",
        "avgPrice": fmt(avg) or "0",
        "time": int(num(created_ms)) if created_ms else 0,
        "updateTime": int(num(updated_ms or created_ms)) if (updated_ms or created_ms) else 0,
        "fills": fills,
        "raw": raw or {},
    }


class RestExchange(ExchangeClient):
    """httpx plumbing shared by the Iranian connectors."""

    base_url: str = ""
    #: quote assets this exchange lists, canonical names
    quotes: tuple[str, ...] = ("USDT", "IRT")
    #: seconds an exchange_info() answer is reused by the connector itself
    markets_ttl: float = 3600.0

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
        proxy: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if settings.trading_mode == "testnet":
            # These exchanges have no testnet: "testnet" orders would be real.
            raise RuntimeError(f"TRADING_MODE=testnet is not available on {self.title}; use paper")
        key, secret = settings.exchange_keys()
        self.api_key = (api_key if api_key is not None else key).strip()
        self.api_secret = (api_secret if api_secret is not None else secret).strip()
        self.testnet = False
        self.base_url = (base_url or settings.exchange_base_url or self.base_url).rstrip("/")
        proxy = proxy if proxy is not None else (settings.exchange_http_proxy or None)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0, connect=10.0),
            proxy=proxy,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self.time_offset_ms = 0
        # canonical symbol -> exchange market record (filled by load_markets)
        self._markets: dict[str, dict] = {}
        self._markets_at = 0.0
        self._markets_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------- HTTP ----------------
    def _error(self, status: int, code: Any, message: str, kind: str | None = None) -> ExchangeError:
        return ExchangeError(status, code, message, exchange=self.name, kind=kind)

    async def _auth_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        """Headers for a signed request; connectors override.

        `target` is the path with its query string exactly as sent and `body`
        the exact bytes of the request body (b"" when there is none), so a
        connector can sign precisely what goes over the wire.
        """
        raise NotImplementedError

    #: spacing for public market-data calls (None = no throttle)
    public_throttle: Throttle | None = None

    def _classify(self, status: int, body: Any, text: str) -> tuple[Any, str, str | None]:
        """(code, message, kind) for an error answer; connectors refine it."""
        code = None
        message = text[:300]
        if isinstance(body, dict):
            code = body.get("code") or body.get("status")
            message = str(body.get("message") or body.get("detail") or body.get("msg") or message)[:300]
        kind = None
        if status in (401, 403):
            kind = "auth"
        elif status == 404:
            kind = "unknown_order"
        elif status == 429:
            kind = "rate_limit"
        return code, message, kind

    def _is_error_body(self, body: Any) -> bool:
        """Some exchanges answer 200 with {"status": "failed"}; connectors override."""
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        form: dict | None = None,
        signed: bool = False,
        attempts: int = 3,
        throttle: Throttle | None = None,
        absolute: bool = False,
    ) -> Any:
        """One API call. `attempts=1` for order placement: a failure there is
        "unknown", never retried blindly (see `_lookup_after_ambiguous`).
        `absolute` = `path` is a full URL on another host of the same exchange."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        query = urlencode(params, doseq=True)
        target = f"{path}?{query}" if query else path
        content: bytes | None = None
        content_type: str | None = None
        if json is not None:
            content = jsonlib.dumps(json, separators=(",", ":"), ensure_ascii=False).encode()
            content_type = "application/json"
        elif form is not None:
            content = urlencode({k: v for k, v in form.items() if v is not None}).encode()
            content_type = "application/x-www-form-urlencoded"
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            headers: dict[str, str] = {}
            if content_type:
                headers["Content-Type"] = content_type
            if signed:
                if not self.api_key:
                    raise self._error(401, None, "API key not configured", "auth")
                sign_target = target
                if absolute:
                    parts = urlsplit(target)
                    sign_target = parts.path + (f"?{parts.query}" if parts.query else "")
                headers.update(await self._auth_headers(method, sign_target, content or b""))
            if throttle is not None:
                await throttle.wait()
            try:
                response = await self._client.request(method, target, content=content, headers=headers)
            except httpx.HTTPError as exc:
                last = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(min(2 ** attempt, 15))
                continue

            text = response.text
            try:
                body = response.json() if text.strip() else {}
            except ValueError:
                body = None
            if response.status_code < 400 and body is None:
                # A success status with a body that is not JSON (a proxy page,
                # a truncated answer): unknown outcome, like a 5xx.
                raise self._error(502, None, f"non-JSON answer: {text[:120]!r}")
            if response.status_code >= 400 or self._is_error_body(body):
                code, message, kind = self._classify(response.status_code, body, text)
                error = self._error(response.status_code if response.status_code >= 400 else 400,
                                    code, message, kind)
                if error.is_retryable and attempt < attempts:
                    retry_after = num(response.headers.get("retry-after"), 0.0)
                    await asyncio.sleep(retry_after or min(2 ** attempt, 15))
                    last = error
                    continue
                raise error
            return body
        raise self._error(0, None, f"request failed after {attempts} attempts: {last}")

    # ---------------- symbols ----------------
    async def load_markets(self, force: bool = False) -> dict[str, dict]:
        async with self._markets_lock:
            if force or not self._markets or time.monotonic() - self._markets_at > self.markets_ttl:
                self._markets = await self._fetch_markets()
                self._markets_at = time.monotonic()
        return self._markets

    async def _fetch_markets(self) -> dict[str, dict]:
        """canonical symbol -> {"base", "quote", "tick", "step", "min_qty",
        "min_notional", "status", ...exchange fields}"""
        raise NotImplementedError

    async def market(self, symbol: str) -> dict:
        markets = await self.load_markets()
        try:
            return markets[symbol.upper()]
        except KeyError:
            raise self._error(400, None, f"{symbol} is not listed on {self.name}", "invalid_symbol") from None

    async def exchange_info(self, market: Market = "spot") -> dict:
        if market != "spot":
            return {"symbols": []}
        markets = await self.load_markets()
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "baseAsset": m["base"],
                    "quoteAsset": m["quote"],
                    "status": m.get("status", "TRADING"),
                    "filters": filters(m["tick"], m["step"], m.get("min_qty"), m.get("min_notional")),
                }
                for symbol, m in markets.items()
            ]
        }

    # ---------------- shared account helpers ----------------
    async def spot_account(self) -> dict:
        balances = await self._balances()
        return {
            "balances": [
                {"asset": asset, "free": fmt(free) or "0", "locked": fmt(locked) or "0"}
                for asset, (free, locked) in balances.items()
            ]
        }

    async def _balances(self) -> dict[str, tuple[float, float]]:
        """asset (canonical, IRT for Toman) -> (free, locked)."""
        raise NotImplementedError

    # ---------------- candles ----------------
    #: interval -> the exchange's resolution parameter, for intervals it
    #: serves UTC-aligned
    native_intervals: dict[str, str] = {}
    #: interval -> smaller native interval it is aggregated from (4h from 30m
    #: where the exchange's own 4h bars are Tehran-aligned)
    built_from: dict[str, str] = {}
    candles_per_request: int = 500

    def candle_source(self, interval: str) -> str:
        if interval in self.native_intervals:
            return interval
        source = self.built_from.get(interval)
        if source:
            return source
        raise self._unsupported(f"{interval} candles")

    async def _fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        """Binance-shaped rows of a native `interval` in [start_ms, end_ms], canonical units."""
        raise NotImplementedError

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
        market: Market = "spot",
    ) -> list[list]:
        source = self.candle_source(interval)
        start, end = window(interval, limit, start_time, end_time)
        rows: list[list] = []
        for a, b in chunks(start, end, interval_ms(source), self.candles_per_request):
            rows.extend(await self._fetch_candles(symbol.upper(), source, a, b))
        if source != interval:
            rows = aggregate(rows, interval)
        return trim(rows, limit, start_time, end_time)

    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any:
        if symbol is None:
            raise self._unsupported("all-symbol ticker")
        source = next(iter(self.native_intervals), None)
        if source is None:
            raise self._unsupported("ticker")
        now = int(time.time() * 1000)
        rows = await self._fetch_candles(symbol.upper(), source, now - interval_ms(source) * 3, now)
        if not rows:
            raise self._error(404, None, f"no price for {symbol}", "invalid_symbol")
        return {"symbol": symbol.upper(), "price": rows[-1][4]}

    async def _lookup_after_ambiguous(
        self, symbol: str, client_order_id: str | None, original: ExchangeError
    ) -> dict:
        """After a placement whose outcome is unknown, find what the exchange did."""
        if not client_order_id:
            raise original
        for _ in range(3):
            await asyncio.sleep(1.0)
            try:
                return await self.query_order(symbol, None, client_order_id=client_order_id)
            except ExchangeError as exc:
                if exc.is_unknown_order:
                    raise original
        raise self._error(0, None, f"order {client_order_id} unresolved after ambiguous failure: {original}")

    # ---------------- shared order helpers ----------------
    def _spot_only(self, market: str) -> None:
        if market != "spot":
            raise self._unsupported("futures / short orders (spot only)")

    #: how far through the best price a "market" order is priced, per attempt.
    #: A sell (an exit, a stop) widens until it fills; a buy tries once.
    MARKET_SLIPPAGES_BUY: tuple[float, ...] = (0.01,)
    MARKET_SLIPPAGES_SELL: tuple[float, ...] = (0.01, 0.025, 0.05)
    market_fill_wait: float = 4.0

    async def _place_limit(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal, client_order_id: str | None
    ) -> str:
        """Place one limit order and return its exchange id (connectors override)."""
        raise NotImplementedError

    async def _enrich(self, symbol: str, order: dict) -> dict:
        """Hook: attach fee details a connector can only read from fills."""
        return order

    async def _wait_or_cancel(self, symbol: str, order_id: str) -> dict:
        """Poll a resting limit for up to `market_fill_wait` seconds, then cancel
        the rest. Never raises: returns the last known state (status NEW when a
        cancel could not be confirmed)."""
        order: dict = {"symbol": symbol, "orderId": str(order_id), "status": "NEW",
                       "executedQty": "0", "cummulativeQuoteQty": "0", "fills": []}
        waited = 0.0
        while True:
            try:
                order = await self.query_order(symbol, order_id)
            except ExchangeError as exc:
                log.warning("%s: could not read order %s: %s", self.name, order_id, exc)
            if order.get("status") not in ("NEW", "PARTIALLY_FILLED") or waited >= self.market_fill_wait:
                break
            await asyncio.sleep(1.0)
            waited += 1.0
        for attempt in range(3):
            if order.get("status") not in ("NEW", "PARTIALLY_FILLED"):
                break
            if attempt:
                await asyncio.sleep(1.0)
            try:
                await self.cancel_order(symbol, order_id)
            except ExchangeError as exc:
                if not exc.is_unknown_order:
                    log.warning("%s: could not cancel order %s: %s", self.name, order_id, exc)
            try:
                order = await self.query_order(symbol, order_id)
            except ExchangeError as exc:
                log.warning("%s: could not re-read order %s: %s", self.name, order_id, exc)
        try:
            order = await self._enrich(symbol, order)
        except ExchangeError as exc:
            log.warning("%s: could not read the fills of %s: %s", self.name, order_id, exc)
        return order

    async def _marketable_order(
        self, symbol: str, side: str, quantity: float, client_order_id: str | None
    ) -> dict:
        """A market order on an exchange whose market orders are unusable.

        Limit orders priced through the best price; a sell widens over
        MARKET_SLIPPAGES_SELL until it fills (an exit must happen), a buy tries
        once (an entry may be skipped). Each attempt rests at most
        `market_fill_wait` seconds before the remainder is cancelled. Once
        anything was placed this never raises; the answer adds up every
        attempt in one Binance-shaped order.
        """
        m = await self.market(symbol)
        buy = side.upper() == "BUY"
        step = Decimal(str(m.get("step") or 0))
        remaining = snap(quantity, step)
        parts: list[dict] = []
        slippages = self.MARKET_SLIPPAGES_BUY if buy else self.MARKET_SLIPPAGES_SELL
        for attempt, slip in enumerate(slippages):
            try:
                book = await self.order_book(symbol, limit=1)
            except ExchangeError:
                if not parts:
                    raise
                break
            levels = book["asks"] if buy else book["bids"]
            if not levels:
                if not parts:
                    raise self._error(400, None, f"empty order book for {symbol}", "invalid_symbol")
                break
            best = Decimal(str(levels[0][0]))
            factor = Decimal(str(1 + slip if buy else 1 - slip))
            price = snap(best * factor, m.get("tick"), up=buy)
            qty = snap(remaining, step)
            if qty <= 0 or qty < Decimal(str(m.get("min_qty") or 0)):
                break
            cid = client_order_id if attempt == 0 or not client_order_id else f"{client_order_id[:28]}r{attempt}"
            try:
                order_id = await self._place_limit(symbol, side, qty, price, cid)
            except ExchangeError:
                if not parts:
                    raise
                break
            part = await self._wait_or_cancel(symbol, order_id)
            parts.append(part)
            remaining -= Decimal(str(part.get("executedQty") or "0"))
            if part.get("status") in ("NEW", "PARTIALLY_FILLED") or remaining <= step / 2:
                break
        return self._combine(symbol, side, quantity, parts)

    def _combine(self, symbol: str, side: str, quantity: float, parts: list[dict]) -> dict:
        executed = sum(num(p.get("executedQty")) for p in parts)
        quote = sum(num(p.get("cummulativeQuoteQty")) for p in parts)
        fills = [f for p in parts for f in (p.get("fills") or [])]
        open_part = next((p for p in parts if p.get("status") in ("NEW", "PARTIALLY_FILLED")), None)
        prior: dict = {}
        if open_part is not None:
            # A part whose cancel never confirmed is still live: report the
            # order open under that part's id so the executor settles it. The
            # finished parts travel along as "prior" fills so settling one part
            # does not forget what the others already filled.
            closed = [p for p in parts if p is not open_part]
            prior = {
                "priorExecutedQty": fmt(sum(num(p.get("executedQty")) for p in closed)) or "0",
                "priorQuoteQty": fmt(sum(num(p.get("cummulativeQuoteQty")) for p in closed)) or "0",
                "priorFills": [f for p in closed for f in (p.get("fills") or [])],
            }
            status = "PARTIALLY_FILLED" if executed > 0 else open_part["status"]
        elif executed >= quantity * 0.999999:
            status = "FILLED"
        else:
            status = "EXPIRED"                    # final; the caller books what filled
        first = parts[0] if parts else {}
        return {
            "symbol": symbol,
            "orderId": (open_part or first).get("orderId"),
            "clientOrderId": first.get("clientOrderId", ""),
            "side": side.upper(),
            "type": "MARKET",
            "status": status,
            "price": "0",
            "stopPrice": "0",
            "origQty": fmt(quantity),
            "executedQty": fmt(executed) or "0",
            "cummulativeQuoteQty": fmt(quote) or "0",
            "avgPrice": fmt(quote / executed) if executed else "0",
            "time": first.get("time", 0),
            "updateTime": (parts[-1] if parts else {}).get("updateTime", 0),
            "fills": fills,
            "raw": {"parts": [p.get("orderId") for p in parts]},
            **prior,
        }

