"""The Iranian exchange connectors, against recorded-shape answers.

Every exchange is answered by httpx.MockTransport with the response shapes
from its official docs (and, for public data, what the live API returned when
the connector was written). The tests pin what the engine relies on: units
(Rial vs Toman), UTC-aligned 4h bars, the exact order bodies and signatures,
and the Binance-shaped answers the executor and the reconciler read.

    cd trader/engine && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx

from app.exchange.base import ExchangeError
from app.exchange.iran import common
from app.exchange.iran.bitpin import BitpinClient
from app.exchange.iran.nobitex import NobitexClient
from app.exchange.iran.okex import OkexClient
from app.exchange.iran.ramzinex import RamzinexClient
from app.exchange.iran.wallex import WallexClient

H4 = 4 * 3600 * 1000
M30 = 30 * 60 * 1000


class Router:
    """(METHOD, path) -> handler(request) or a JSON-able answer; records every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        route = self.routes.get(key)
        if route is None:
            for (method, prefix), value in self.routes.items():
                if method == request.method and prefix.endswith("*") and request.url.path.startswith(prefix[:-1]):
                    route = value
                    break
        if route is None:
            return httpx.Response(404, json={"detail": f"no route {key}"})
        answer = route(request) if callable(route) else route
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls if r.method == method and r.url.path == path and r.content]


def make(cls, routes: dict, **kwargs):
    router = Router(routes)
    client = cls(transport=httpx.MockTransport(router), **kwargs)
    for name in ("public_throttle", "_private_throttle", "_book_throttle", "_order_throttle"):
        if hasattr(client, name):
            setattr(client, name, None)
    return client, router


def udf(start_ms: int, step_ms: int, closes: list[float], offset_s: int = 0) -> dict:
    t = [(start_ms + i * step_ms) // 1000 + offset_s for i in range(len(closes))]
    return {"s": "ok", "t": t, "o": closes, "h": [c + 1 for c in closes], "l": [c - 1 for c in closes],
            "c": closes, "v": [1.0] * len(closes)}


async def _no_sleep(*_args, **_kwargs):
    return None


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
class AggregationTest(unittest.TestCase):
    def test_thirty_minute_bars_fold_into_utc_four_hour_bars(self):
        start = 1_700_006_400_000 - 1_700_006_400_000 % H4   # a UTC 4h boundary
        rows = [common.kline_row(start + i * M30, "30m", 10 + i, 11 + i, 9 + i, 10.5 + i, 2) for i in range(12)]
        bars = common.aggregate(rows, "4h")
        self.assertEqual([b[0] for b in bars], [start, start + H4])
        first = bars[0]
        self.assertEqual(first[1], "10")        # open of the first 30m bar
        self.assertEqual(first[2], "18")        # highest high of the eight
        self.assertEqual(first[3], "9")         # lowest low
        self.assertEqual(first[4], "17.5")      # close of the eighth
        self.assertEqual(first[5], "16")        # summed volume
        self.assertEqual(first[6], start + H4 - 1)


# --------------------------------------------------------------------------
# Nobitex
# --------------------------------------------------------------------------
OPTIONS = {"nobitex": {
    "amountPrecisions": {"BTCIRT": "0.000001", "BTCUSDT": "0.000001", "USDTIRT": "0.01"},
    "pricePrecisions": {"BTCIRT": "10", "BTCUSDT": "0.01", "USDTIRT": "10"},
    "minOrders": {"rls": "500000", "usdt": "0.5"},
}}


def _nobitex_done_order(**extra):
    order = {"id": 77, "type": "buy", "execution": "Market", "market": "BTC-RLS", "price": "market",
             "amount": "0.01", "matchedAmount": "0.01", "unmatchedAmount": "0", "status": "Done",
             "averagePrice": "2310000000", "totalPrice": "23100000", "fee": "0.00002",
             "created_at": "2026-01-01T00:00:00+00:00", "clientOrderId": "gtrabc"}
    order.update(extra)
    return order


class NobitexTest(unittest.IsolatedAsyncioTestCase):
    async def test_markets_are_in_toman(self):
        client, _ = make(NobitexClient, {("GET", "/v2/options"): OPTIONS}, api_key="", api_secret="")
        info = await client.exchange_info()
        btc = next(s for s in info["symbols"] if s["symbol"] == "BTCIRT")
        filters = {f["filterType"]: f for f in btc["filters"]}
        self.assertEqual(filters["PRICE_FILTER"]["tickSize"], "1")        # 10 Rial
        self.assertEqual(filters["NOTIONAL"]["minNotional"], "50000")     # 500,000 Rial
        self.assertEqual(btc["quoteAsset"], "IRT")

    async def test_four_hour_bars_are_built_from_thirty_minutes(self):
        now = int(time.time() * 1000)
        forming = now - now % H4
        start = forming - 2 * H4
        closes = [100.0 + i for i in range(24)]

        def history(request: httpx.Request):
            self.assertEqual(request.url.params["resolution"], "30")
            return udf(start, M30, closes)

        client, _ = make(NobitexClient, {("GET", "/market/udf/history"): history}, api_key="", api_secret="")
        bars = await client.klines("BTCUSDT", "4h", limit=3)
        self.assertEqual([b[0] for b in bars], [start, start + H4, forming])
        self.assertEqual(bars[0][4], "107")   # eighth 30m close
        self.assertEqual(bars[-1][6], forming + H4 - 1)

    async def test_order_body_speaks_rial_and_the_answer_toman(self):
        def add(request: httpx.Request):
            return {"status": "ok", "order": _nobitex_done_order()}

        client, router = make(NobitexClient, {("POST", "/market/orders/add"): add}, api_key="tok", api_secret="")
        order = await client.create_order("BTCIRT", "BUY", "STOP_LOSS_LIMIT", quantity=0.01,
                                          price=2_300_000_000.0, stop_price=2_310_000_000.0,
                                          client_order_id="gtrs123")
        body = router.bodies("POST", "/market/orders/add")[0]
        self.assertEqual(body["execution"], "stop_limit")
        self.assertEqual(body["dstCurrency"], "rls")
        self.assertEqual(body["price"], "23000000000")
        self.assertEqual(body["stopPrice"], "23100000000")
        self.assertEqual(body["clientOrderId"], "gtrs123")
        self.assertEqual(router.calls[0].headers["authorization"], "Token tok")
        # the answer, back in the engine's Toman
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["avgPrice"], "231000000")
        self.assertEqual(order["fills"][0]["commissionAsset"], "BTC")

    async def test_api_key_requests_are_ed25519_signed(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                     serialization.NoEncryption())
        secret = base64.urlsafe_b64encode(seed).decode()
        client, router = make(NobitexClient, {("POST", "/users/wallets/list"): {
            "status": "ok", "wallets": [
                {"currency": "rls", "balance": "1000000", "blockedBalance": "200000", "activeBalance": "800000"},
                {"currency": "btc", "balance": "0.5", "blockedBalance": "0", "activeBalance": "0.5"},
            ]}}, api_key="PUBLICKEY", api_secret=secret)
        balances = await client.spot_balances()
        self.assertEqual(balances["IRT"], 100000.0)     # 1,000,000 Rial
        self.assertEqual(await client.free_balance("IRT"), 80000.0)
        request = router.calls[0]
        message = (request.headers["nobitex-timestamp"] + "POST" + "/users/wallets/list").encode() + request.content
        signature = base64.urlsafe_b64decode(request.headers["nobitex-signature"])
        private.public_key().verify(signature, message)   # raises if wrong
        self.assertEqual(request.headers["nobitex-key"], "PUBLICKEY")

    async def test_a_failed_status_on_http_200_is_an_error(self):
        client, _ = make(NobitexClient, {("POST", "/market/orders/add"): {
            "status": "failed", "code": "SmallOrder", "message": "too small"}}, api_key="tok", api_secret="")
        with self.assertRaises(ExchangeError) as ctx:
            await client.create_order("BTCUSDT", "BUY", "MARKET", quantity=0.00001)
        self.assertEqual(ctx.exception.code, "SmallOrder")

    async def test_cancelling_a_done_order_reads_as_gone(self):
        client, _ = make(NobitexClient, {
            ("POST", "/market/orders/update-status"): {"status": "failed", "code": "InvalidOrderStatus",
                                                       "message": "cannot cancel"},
            ("POST", "/market/orders/status"): {"status": "ok", "order": _nobitex_done_order()},
        }, api_key="tok", api_secret="")
        with self.assertRaises(ExchangeError) as ctx:
            await client.cancel_order("BTCIRT", 77)
        self.assertTrue(ctx.exception.is_unknown_order)


# --------------------------------------------------------------------------
# Wallex
# --------------------------------------------------------------------------
class WallexTest(unittest.IsolatedAsyncioTestCase):
    async def test_market_order_uses_client_id_and_documented_casing(self):
        def add(request: httpx.Request):
            body = json.loads(request.content)
            return httpx.Response(201, json={"success": True, "result": {
                "clientOrderId": body["client_id"], "symbol": "BTCTMN", "side": "BUY", "type": "MARKET",
                "price": body["price"], "origQty": "0.01", "executedQty": "0.01", "executedPrice": "2300000000",
                "executedSum": "23000000", "status": "FILLED", "active": False,
                "fills": [{"price": "2300000000", "quantity": "0.01", "fee": "0.00002", "feeAsset": "BTC"}]}})

        client, router = make(WallexClient, {
            ("POST", "/v1/account/orders"): add,
            ("GET", "/v1/trades"): {"result": {"latestTrades": [{"price": "2300000000.000"}]}},
        }, api_key="k", api_secret="")
        order = await client.create_order("BTCIRT", "BUY", "MARKET", quantity=0.01, client_order_id="gtrab12")
        body = router.bodies("POST", "/v1/account/orders")[0]
        self.assertEqual(body["symbol"], "BTCTMN")
        self.assertEqual(body["client_id"], "GTRAB12")
        self.assertEqual(body["price"], "2300000000")
        self.assertNotIn("stop_Price", body)
        self.assertEqual(router.calls[-1].headers["x-api-key"], "k")
        self.assertEqual(order["orderId"], "GTRAB12")
        self.assertEqual(order["clientOrderId"], "gtrab12")     # prefix checks still match
        self.assertEqual(order["symbol"], "BTCIRT")
        self.assertEqual(order["fills"][0]["commissionAsset"], "BTC")

    async def test_stop_limit_sends_stop_Price(self):
        def add(request: httpx.Request):
            return httpx.Response(201, json={"success": True, "result": {
                "clientOrderId": "X", "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_LIMIT",
                "status": "NEW", "origQty": "0.01", "executedQty": "0"}})

        client, router = make(WallexClient, {("POST", "/v1/account/orders"): add}, api_key="k", api_secret="")
        order = await client.create_order("BTCUSDT", "SELL", "STOP_LOSS_LIMIT", quantity=0.01,
                                          price=59000.0, stop_price=60000.0)
        body = router.bodies("POST", "/v1/account/orders")[0]
        self.assertEqual(body["type"], "STOP_LIMIT")
        self.assertEqual(body["stop_Price"], "60000")
        self.assertEqual(order["type"], "STOP_LOSS_LIMIT")

    async def test_open_orders_object_of_arrays_and_balances(self):
        client, _ = make(WallexClient, {
            ("GET", "/v1/account/openOrders"): {"result": {"BTCUSDT": [
                {"clientOrderId": "GTRS1", "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_LIMIT",
                 "status": "NEW", "origQty": "0.01", "executedQty": "0", "active": True}]}},
            ("GET", "/v1/account/balances"): {"result": {"balances": {
                "TMN": {"asset": "TMN", "value": "500000", "locked": "100000"},
                "USDT": {"asset": "USDT", "value": "12.5", "locked": "0"}}}},
        }, api_key="k", api_secret="")
        orders = await client.open_orders()
        self.assertEqual(orders[0]["clientOrderId"], "gtrs1")
        self.assertEqual(orders[0]["type"], "STOP_LOSS_LIMIT")
        account = await client.spot_account()
        free = {b["asset"]: b["free"] for b in account["balances"]}
        self.assertEqual(free["IRT"], "400000")
        self.assertEqual(free["USDT"], "12.5")

    async def test_four_hour_bars_from_utc_hours(self):
        now = int(time.time() * 1000)
        forming = now - now % H4
        start = forming - H4
        client, _ = make(WallexClient, {("GET", "/v1/udf/history"): udf(start, 3600_000, [1, 2, 3, 4, 5, 6])},
                         api_key="", api_secret="")
        bars = await client.klines("BTCUSDT", "4h", limit=2)
        self.assertEqual([b[0] for b in bars], [start, forming])
        self.assertEqual(bars[0][4], "4")

    async def test_order_book_sides_follow_the_prices(self):
        client, _ = make(WallexClient, {("GET", "/v1/depth"): {"result": {
            "ask": [{"price": 99.0, "quantity": 1}], "bid": [{"price": 101.0, "quantity": 2}]}}},
            api_key="", api_secret="")
        book = await client.order_book("BTCUSDT")
        self.assertEqual(book["bids"][0][0], "99")
        self.assertEqual(book["asks"][0][0], "101")


# --------------------------------------------------------------------------
# Bitpin
# --------------------------------------------------------------------------
def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"x.{payload}.y"


class BitpinTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # No Redis in unit tests: tokens stay in memory.
        self._redis = mock.patch("app.workers.bus.get_redis", side_effect=RuntimeError("no redis"))
        self._redis.start()

    async def asyncTearDown(self):
        self._redis.stop()

    async def test_tokens_are_fetched_once_and_sent_as_bearer(self):
        access = _jwt(time.time() + 900)

        def wallets(request: httpx.Request):
            self.assertEqual(request.headers["authorization"], f"Bearer {access}")
            return [{"asset": "IRT", "balance": "250000", "frozen": "50000"},
                    {"asset": "BTC", "balance": "0.1", "frozen": "0"}]

        client, router = make(BitpinClient, {
            ("POST", "/api/v1/usr/authenticate/"): {"access": access, "refresh": _jwt(time.time() + 86400)},
            ("GET", "/api/v1/wlt/wallets/"): wallets,
        }, api_key="key", api_secret="secret")
        self.assertEqual((await client.spot_balances())["IRT"], 300000.0)
        await client.spot_balances()
        auth_calls = [r for r in router.calls if r.url.path == "/api/v1/usr/authenticate/"]
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(json.loads(auth_calls[0].content), {"api_key": "key", "secret_key": "secret"})

    async def test_closed_states_become_filled_or_cancelled(self):
        client, _ = make(BitpinClient, {}, api_key="", api_secret="")
        done = client._normalize({"id": 1, "symbol": "BTC_IRT", "side": "buy", "type": "market", "state": "closed",
                                  "base_amount": "0.01", "dealed_base_amount": "0.01",
                                  "dealed_quote_amount": "23000000", "commission": "0.00002"})
        self.assertEqual(done["status"], "FILLED")
        self.assertEqual(done["avgPrice"], "2300000000")
        cancelled = client._normalize({"id": 2, "symbol": "BTC_IRT", "side": "sell", "type": "stop_limit",
                                       "state": "closed", "base_amount": "0.01", "dealed_base_amount": "0"})
        self.assertEqual(cancelled["status"], "CANCELED")
        self.assertEqual(cancelled["type"], "STOP_LOSS_LIMIT")

    async def test_unrequested_resolutions_are_ignored(self):
        now = int(time.time() * 1000)
        start = now - now % H4 - H4
        rows = [{"open": 1, "close": 2, "low": "0.5", "high": "3", "volume": 1, "ts": (start + i * M30) / 1000,
                 "resolution": "30m"} for i in range(8)]
        rows.append({"open": 9, "close": 9, "low": "9", "high": "9", "volume": 1, "ts": start / 1000 + 60,
                     "resolution": "1m"})
        client, _ = make(BitpinClient, {("GET", "/api/v1/mkt/candles/"): rows}, api_key="", api_secret="")
        bars = await client.klines("BTCUSDT", "4h", limit=2)
        self.assertEqual(bars[0][0], start)
        self.assertEqual(bars[0][2], "3")


# --------------------------------------------------------------------------
# Ramzinex
# --------------------------------------------------------------------------
PAIRS = {"status": 0, "data": {"pairs": [
    {"id": 2, "price_precision": 0, "is_temporarily_suspended": False,
     "trading_chart_settings": {"ramzinex": "btcirr"},
     "base_currency": {"id": 1, "symbol": {"en": "btc"}, "precision": 6},
     "quote_currency": {"id": 2, "symbol": {"en": "irr"}, "precision": 0}},
    {"id": 12, "price_precision": 2, "is_temporarily_suspended": False,
     "trading_chart_settings": {"ramzinex": "btcusdt"},
     "base_currency": {"id": 1, "symbol": {"en": "btc"}, "precision": 6},
     "quote_currency": {"id": 9, "symbol": {"en": "usdt"}, "precision": 2}},
]}}


class RamzinexTest(unittest.IsolatedAsyncioTestCase):
    def _routes(self, order_states: list[dict]):
        states = iter(order_states)
        last = {}

        def status(request: httpx.Request):
            nonlocal last
            last = next(states, last)
            return {"status": 0, "data": last}

        return {
            ("GET", "/exchange/api/v2.0/exchange/pairs"): PAIRS,
            ("POST", "/exchange/api/v1.0/exchange/auth/api_key/getToken"): {
                "status": 0, "data": {"token": _jwt(time.time() + 3600)}},
            ("GET", "/exchange/api/v1.0/exchange/orderbooks/2/buys_sells"): {"status": 0, "data": {
                "buys": [[2_300_000_000, 0.5], [2_299_000_000, 1]], "sells": [[2_302_000_000, 0.4]]}},
            ("POST", "/exchange/api/v1.0/exchange/users/me/orders/limit"): {"status": 0, "data": {"order_id": 5}},
            ("GET", "/exchange/api/v1.0/exchange/users/me/orders2/5"): status,
            ("POST", "/exchange/api/v1.0/exchange/users/me/orders/5/cancel"): {"status": 0, "data": []},
        }

    async def test_no_stop_orders_so_the_engine_keeps_the_floor(self):
        self.assertFalse(RamzinexClient.supports_stop_orders)

    async def test_market_buy_is_a_marketable_limit_in_rial(self):
        base = {"id": 5, "pair_id": 2, "type_en": "buy", "amount_nr": 0.01, "order_price_nr": 2_325_020_000,
                "commission_currency_id": 1, "commission_nr": 0.00002}
        client, router = make(RamzinexClient, self._routes([
            {**base, "status_id": 1, "filled_nr": 0.004, "average_price_nr": 2_302_000_000},
            {**base, "status_id": 1, "filled_nr": 0.004, "average_price_nr": 2_302_000_000},
            {**base, "status_id": 2, "filled_nr": 0.004, "average_price_nr": 2_302_000_000},
        ]), api_key="k", api_secret="s")
        client.market_fill_wait = 1.0
        with mock.patch("app.exchange.iran.common.asyncio.sleep", _no_sleep):
            order = await client.create_order("BTCIRT", "BUY", "MARKET", quantity=0.01)
        body = router.bodies("POST", "/exchange/api/v1.0/exchange/users/me/orders/limit")[0]
        self.assertEqual(body["pair_id"], 2)
        self.assertEqual(body["type"], "buy")
        # best ask 230,200,000 Toman + 1% -> 232,502,000 Toman -> an integral Rial price, no float noise
        self.assertEqual(body["price"], 2_325_020_000)
        self.assertIsInstance(body["price"], int)
        self.assertEqual(order["type"], "MARKET")
        self.assertEqual(order["status"], "EXPIRED")           # final: 0.004 of 0.01 filled
        self.assertEqual(order["executedQty"], "0.004")
        self.assertEqual(order["avgPrice"], "230200000")      # Toman
        cancel = [r for r in router.calls if r.url.path.endswith("/orders/5/cancel")]
        self.assertEqual(len(cancel), 1)
        token_call = router.calls[[r.url.path for r in router.calls].index(
            "/exchange/api/v1.0/exchange/users/me/orders/limit")]
        self.assertTrue(token_call.headers["authorization2"].startswith("Bearer "))
        self.assertEqual(token_call.headers["x-api-key"], "k")

    async def test_balances_come_from_the_summary_in_toman(self):
        routes = self._routes([])
        routes[("GET", "/exchange/api/v1.0/exchange/users/me/funds/summaryDesktop")] = {"status": 0, "data": [
            {"currency_id": 1, "total_nr": 0.002, "in_order_nr": 0},
            {"currency_id": 2, "total_nr": 11_190_040, "in_order_nr": 7_664_790},
            {"currency_id": 9, "total_nr": 20, "in_order_nr": 5},
        ]}
        client, _ = make(RamzinexClient, routes, api_key="k", api_secret="s")
        balances = await client.spot_balances()
        self.assertAlmostEqual(balances["IRT"], 1_119_004.0)
        self.assertEqual(await client.free_balance("USDT"), 15.0)
        self.assertEqual(balances["BTC"], 0.002)


# --------------------------------------------------------------------------
# OK-EX
# --------------------------------------------------------------------------
OKEX_PAIRS = [{"symbol": "BTC-USDT", "base": "BTC", "quote": "USDT", "minNotional": 1,
               "quantityTick": 1e-05, "priceTick": 0.01, "isTrade": True}]


class OkexTest(unittest.IsolatedAsyncioTestCase):
    async def test_signature_and_market_buy_as_marketable_limit(self):
        placed = {}

        def order(request: httpx.Request):
            placed.update(json.loads(request.content))
            return {"id": "9", "symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "quantity": placed["qt"],
                    "price": placed["price"], "filled": 0, "status": "NEW", "clientOrderId": placed["clientOrderId"]}

        def open_orders(request: httpx.Request):
            return []

        def history(request: httpx.Request):
            return [{"id": "9", "symbol": "BTC-USDT", "side": "BUY", "type": "LIMIT", "quantity": 0.01,
                     "price": placed["price"], "filled": 0.01, "avgPrice": 60010, "status": "FILLED",
                     "clientOrderId": "gtr1"}]

        client, router = make(OkexClient, {
            ("GET", "/api/v1/spot/public/pairs"): OKEX_PAIRS,
            ("GET", "/api/v1/spot/public/books"): {"bids": [[59990.0, 1]], "asks": [[60010.0, 1]]},
            ("POST", "/hapi/v1/spot/order"): order,
            ("GET", "/hapi/v1/spot/order/open"): open_orders,
            ("GET", "/hapi/v1/spot/order/history"): history,
            ("GET", "/hapi/v1/spot/order/trades"): [{"order_id": "9", "price": 60010, "qty": 0.01,
                                                     "fee": 0.00001, "fee_asset": "BTC"}],
        }, api_key="key", api_secret="secret")
        result = await client.create_order("BTCUSDT", "BUY", "MARKET", quantity=0.01, client_order_id="gtr1")
        request = [r for r in router.calls if r.url.path == "/hapi/v1/spot/order"][0]
        body = json.loads(request.content)
        self.assertEqual(body["type"], "LIMIT")
        self.assertEqual(body["qt"], 0.01)
        self.assertEqual(body["price"], 60610.1)            # best ask + 1%, on the 0.01 grid
        message = f"POST\n/hapi/v1/spot/order\n{request.headers['x-timestamp']}\n" + base64.b64encode(request.content).decode()
        expected = hmac.new(b"secret", message.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request.headers["x-signature"], expected)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["type"], "MARKET")
        self.assertEqual(result["executedQty"], "0.01")
        self.assertEqual(result["fills"][0]["commissionAsset"], "BTC")   # read from the fills

    async def test_futures_are_refused(self):
        client, _ = make(OkexClient, {}, api_key="key", api_secret="secret")
        with self.assertRaises(ExchangeError) as ctx:
            await client.create_order("BTCUSDT", "SELL", "MARKET", market="futures", quantity=0.01)
        self.assertTrue(ctx.exception.is_unsupported)

    async def test_candles_arrive_newest_first(self):
        now = int(time.time() * 1000)
        forming = now - now % H4
        rows = [{"time": forming - i * H4, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3}
                for i in range(3)]
        client, _ = make(OkexClient, {("GET", "/api/v1/spot/public/candles"): {"res": rows}},
                         api_key="", api_secret="")
        bars = await client.klines("BTCUSDT", "4h", limit=3)
        self.assertEqual([b[0] for b in bars], [forming - 2 * H4, forming - H4, forming])


# --------------------------------------------------------------------------
# the executor with an exchange that has no stop orders
# --------------------------------------------------------------------------
class EngineManagedStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_exchange_stop_is_attempted(self):
        from app.execution.executor import LiveExecutor

        client = SimpleNamespace(supports_stop_orders=False, supports_earn=False, supports_fee_token=False,
                                 create_order=mock.AsyncMock())
        executor = LiveExecutor(client, registry=SimpleNamespace())
        position = SimpleNamespace(meta={}, stop_missing=True, side="long", symbol="BTCIRT",
                                   stop_price=1.0, qty=1.0, market="spot")
        await executor._place_protective_stop(None, position, SimpleNamespace())
        client.create_order.assert_not_called()
        self.assertEqual(position.meta["stop_mode"], "engine")
        self.assertFalse(position.stop_missing)
        self.assertFalse(await executor.ensure_protective_stop(None, position, SimpleNamespace()))


# --------------------------------------------------------------------------
# review fixes
# --------------------------------------------------------------------------
class RamzinexReviewTest(unittest.IsolatedAsyncioTestCase):
    def _client(self, routes):
        client, router = make(RamzinexClient, {
            ("GET", "/exchange/api/v2.0/exchange/pairs"): PAIRS,
            ("POST", "/exchange/api/v1.0/exchange/auth/api_key/getToken"): {
                "status": 0, "data": {"token": _jwt(time.time() + 3600)}},
            **routes,
        }, api_key="k", api_secret="s")
        client.market_fill_wait = 0.0
        return client, router

    async def test_a_sell_widens_until_it_fills(self):
        placed: list[dict] = []
        states = {}

        def limit(request: httpx.Request):
            body = json.loads(request.content)
            placed.append(body)
            order_id = 100 + len(placed)
            # the first attempt rests unfilled, the second fills completely
            states[order_id] = 2 if len(placed) == 1 else 3
            return {"status": 0, "data": {"order_id": order_id}}

        def status(request: httpx.Request):
            order_id = int(request.url.path.rsplit("/", 1)[1])
            filled = 0.0 if states[order_id] == 2 else 0.01
            return {"status": 0, "data": {"id": order_id, "pair_id": 12, "type_en": "sell", "amount_nr": 0.01,
                                          "filled_nr": filled, "status_id": states[order_id],
                                          "average_price_nr": 59400, "commission_currency_id": 9,
                                          "commission_nr": 0.5}}

        client, router = self._client({
            ("GET", "/exchange/api/v1.0/exchange/orderbooks/12/buys_sells"): {"status": 0, "data": {
                "buys": [[60000, 1]], "sells": [[60100, 1]]}},
            ("POST", "/exchange/api/v1.0/exchange/users/me/orders/limit"): limit,
            ("GET", "/exchange/api/v1.0/exchange/users/me/orders2/*"): status,
            ("POST", "/exchange/api/v1.0/exchange/users/me/orders/*"): {"status": 0, "data": []},
        })
        with mock.patch("app.exchange.iran.common.asyncio.sleep", _no_sleep):
            order = await client.create_order("BTCUSDT", "SELL", "MARKET", quantity=0.01)
        self.assertEqual([p["price"] for p in placed], [59400, 58500])   # -1 %, then -2.5 %
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["executedQty"], "0.01")

    async def test_an_ambiguous_placement_is_found_not_resent(self):
        sent = []

        def limit(request: httpx.Request):
            sent.append(request)
            return httpx.Response(504, text="gateway timeout")

        def orders3(request: httpx.Request):
            return {"status": 0, "data": [{"id": 777, "type_en": "buy", "amount_nr": 0.01,
                                           "created_at_ms": int(time.time())}]}

        client, _ = self._client({
            ("POST", "/exchange/api/v1.0/exchange/users/me/orders/limit"): limit,
            ("POST", "/exchange/api/v1.0/exchange/users/me/orders3"): orders3,
        })
        order_id = await client._place_limit("BTCUSDT", "BUY", common.snap(0.01, "0.000001"),
                                             common.snap(60000, "0.01"), None)
        self.assertEqual(order_id, "777")
        self.assertEqual(len(sent), 1)

    async def test_missing_wallet_data_is_an_error_not_zero(self):
        client, _ = self._client({
            ("GET", "/exchange/api/v1.0/exchange/users/me/funds/summaryDesktop"): {"status": 0},
        })
        with self.assertRaises(ExchangeError):
            await client.spot_balances()


class NobitexHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_bars_come_from_shifted_four_hour_candles(self):
        now = int(time.time() * 1000)
        old_end = now - 10 * 24 * 3600 * 1000
        old_end -= old_end % H4
        seen = []

        def history(request: httpx.Request):
            resolution = request.url.params["resolution"]
            seen.append(resolution)
            start = int(request.url.params["from"]) * 1000
            if resolution == "240":
                first = start - start % H4 + 30 * 60 * 1000      # Tehran-aligned: :30 UTC
                return udf(first, H4, [1.0, 2.0, 3.0])
            return {"s": "no_data"}

        client, _ = make(NobitexClient, {("GET", "/market/udf/history"): history}, api_key="", api_secret="")
        bars = await client.klines("BTCUSDT", "4h", limit=2, end_time=old_end)
        self.assertEqual(seen, ["240"])
        self.assertTrue(all(b[0] % H4 == 0 for b in bars))          # on the UTC grid
        self.assertEqual(len(bars), 2)

    async def test_prices_go_out_without_float_noise(self):
        self.assertEqual(NobitexClient._price_out(0.011, "IRT"), "0.11")
        self.assertEqual(NobitexClient._price_out(23_000_000_000.0, "IRT"), "230000000000")


class SettleOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_resting_market_order_is_cancelled_and_its_fill_booked(self):
        from app.execution import executor as executor_mod
        from app.execution.executor import LiveExecutor

        states = iter([
            {"orderId": "5", "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0", "fills": []},
        ] * executor_mod.SETTLE_POLLS + [
            {"orderId": "5", "status": "CANCELED", "executedQty": "0.004", "cummulativeQuoteQty": "240",
             "fills": []},
        ])
        client = SimpleNamespace(
            supports_stop_orders=True, supports_earn=False, supports_fee_token=False,
            query_order=mock.AsyncMock(side_effect=lambda *a, **k: next(states)),
            cancel_order=mock.AsyncMock(return_value={}),
        )
        executor = LiveExecutor(client, registry=SimpleNamespace())
        with mock.patch("app.execution.executor.asyncio.sleep", _no_sleep):
            order = await executor._settle_order(None, "BTCUSDT", "spot", {"orderId": "5", "status": "NEW"})
        client.cancel_order.assert_awaited_once()
        self.assertEqual(order["status"], "CANCELED")
        self.assertEqual(order["executedQty"], "0.004")

    def test_an_implausible_base_fee_never_shrinks_the_position(self):
        from app.execution.executor import LiveExecutor

        qty, avg, fee = LiveExecutor._parse_fill(
            {"executedQty": "0.01", "cummulativeQuoteQty": "2300000",
             "fills": [{"price": "230000000", "qty": "0.01", "commission": "575000", "commissionAsset": "BTC"}]},
            230000000.0, "BTC",
        )
        self.assertEqual(qty, 0.01)
        self.assertEqual(fee, 575000.0)


class ConfigGuardTest(unittest.TestCase):
    def test_testnet_is_binance_only(self):
        from app.config import Settings

        with self.assertRaises(ValueError):
            Settings(_env_file=None, trading_mode="testnet", exchange="nobitex")

    def test_symbols_must_match_the_quote(self):
        from app.config import Settings

        with self.assertRaises(ValueError):
            Settings(_env_file=None, quote_asset="IRT", symbols="BTCUSDT,ETHIRT")
        ok = Settings(_env_file=None, quote_asset="IRT", symbols="BTCIRT,ETHIRT")
        self.assertEqual(ok.market_gate_symbol, "BTCIRT")


class CombineAndMergeTest(unittest.TestCase):
    def test_a_live_part_keeps_the_order_open_and_prior_fills_survive_settling(self):
        from app.execution.executor import LiveExecutor

        client, _ = make(RamzinexClient, {}, api_key="k", api_secret="s")
        done = {"orderId": "5", "status": "CANCELED", "executedQty": "0.004", "cummulativeQuoteQty": "240",
                "fills": [{"price": "60000", "qty": "0.004", "commission": "0", "commissionAsset": "USDT"}]}
        live = {"orderId": "6", "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0", "fills": []}
        combined = client._combine("BTCUSDT", "SELL", 0.01, [done, live])
        self.assertEqual(combined["status"], "PARTIALLY_FILLED")
        self.assertEqual(combined["orderId"], "6")
        # the executor re-reads only order 6; the 0.004 already sold must stay counted
        settled = LiveExecutor._merge(combined, {"orderId": "6", "status": "CANCELED", "executedQty": "0.006",
                                                 "cummulativeQuoteQty": "357", "fills": []})
        self.assertAlmostEqual(float(settled["executedQty"]), 0.01)
        self.assertAlmostEqual(float(settled["cummulativeQuoteQty"]), 597.0)
        self.assertEqual(len(settled["fills"]), 1)


if __name__ == "__main__":
    unittest.main()
