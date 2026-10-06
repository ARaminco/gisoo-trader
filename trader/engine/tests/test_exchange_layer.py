"""The exchange layer around the connectors: registry, REST polling, NetArz FX
and the LLM gateways.

No network: HTTP is answered by httpx.MockTransport and the market-data
callbacks are collected in memory.

    cd trader/engine && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import httpx

from app.config import settings
from app.data import ingest
from app.exchange import fx, registry
from app.exchange.base import ExchangeClient, ExchangeError
from app.exchange.iran import common


class RegistryTest(unittest.TestCase):
    def test_every_registered_exchange_builds(self):
        for name in registry.EXCHANGES:
            cls = registry.client_class(name)
            self.assertTrue(issubclass(cls, ExchangeClient), name)
            self.assertEqual(cls.name, name)
            info = registry.describe(name)
            self.assertIn(info["status"], ("reference", "beta"))

    def test_unknown_exchange_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            registry.spec("no-such-exchange")

    def test_only_binance_claims_the_binance_extras(self):
        for name in registry.EXCHANGES:
            cls = registry.client_class(name)
            if name == "binance":
                continue
            self.assertFalse(cls.supports_earn, name)
            self.assertFalse(cls.supports_futures, name)
            self.assertFalse(cls.supports_fee_token, name)
            self.assertFalse(cls.supports_user_stream, name)


class ErrorKindsTest(unittest.TestCase):
    def test_binance_codes_map_to_portable_kinds(self):
        from app.exchange.binance_client import BinanceError

        self.assertTrue(BinanceError(400, -2011, "Unknown order sent.").is_unknown_order)
        self.assertTrue(BinanceError(400, -2013, "Order does not exist.").is_unknown_order)
        self.assertFalse(BinanceError(400, -2010, "insufficient").is_unknown_order)
        self.assertTrue(BinanceError(429, None, "slow down").is_retryable)
        self.assertIsInstance(BinanceError(400, -1, "x"), ExchangeError)


class HelpersTest(unittest.TestCase):
    def test_fmt_never_uses_exponents(self):
        self.assertEqual(common.fmt(1e-8), "0.00000001")
        self.assertEqual(common.fmt(12.5000), "12.5")
        self.assertEqual(common.fmt(100), "100")

    def test_window_pages_back_from_now(self):
        start, end = common.window("4h", 10, None, 1_700_000_000_000)
        self.assertEqual(end, 1_700_000_000_000)
        self.assertEqual(start % (4 * 3600 * 1000), 0)
        self.assertGreaterEqual(end - start, 10 * 4 * 3600 * 1000 - 4 * 3600 * 1000)

    def test_trim_follows_the_binance_paging_contract(self):
        step = 4 * 3600 * 1000
        rows = [common.kline_row(i * step, "4h", 1, 2, 0.5, 1.5, 10) for i in range(10)]
        rows.append(rows[3])  # duplicate
        self.assertEqual([r[0] for r in common.trim(rows, 3, None, None)], [7 * step, 8 * step, 9 * step])
        self.assertEqual([r[0] for r in common.trim(rows, 2, 4 * step, None)], [4 * step, 5 * step])
        self.assertEqual(common.trim(rows, 3, None, None)[-1][6], 10 * step - 1)

    def test_order_aggregates_one_fill(self):
        o = common.order(symbol="BTCUSDT", order_id=7, client_order_id="gtr1", side="buy",
                         order_type="MARKET", status="FILLED", orig_qty="0.01",
                         executed_qty="0.01", quote_qty="600", fee="0.6", fee_asset="USDT")
        self.assertEqual(o["side"], "BUY")
        self.assertEqual(o["avgPrice"], "60000")
        self.assertEqual(o["fills"][0]["commission"], "0.6")
        self.assertEqual(o["orderId"], "7")


class FakeKlines:
    """Serves a 4h series that grows when `advance()` is called."""

    supports_kline_stream = False
    name = "fake"

    def __init__(self, bars: int):
        self.step = 4 * 3600 * 1000
        now = int(time.time() * 1000)
        self.forming_open = now - now % self.step
        self.bars = bars

    def advance(self):
        self.forming_open += self.step

    async def klines(self, symbol, interval, limit=500, start_time=None, end_time=None, market="spot"):
        opens = [self.forming_open - self.step * i for i in range(limit)][::-1]
        rows = []
        for o in opens:
            row = common.kline_row(o, "4h", 1, 2, 0.5, 1.5, 10)
            if o == self.forming_open:
                row[6] = int(time.time() * 1000) + 10_000_000  # still forming
            rows.append(row)
        return rows


class PollingStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_closed_bar_is_announced_once_and_marks_flow(self):
        client = FakeKlines(10)
        seen: list[tuple[str, int, bool]] = []

        async def on_candle(symbol, timeframe, row, closed):
            seen.append((symbol, row["open_time"], closed))

        stream = ingest.PollingKlineStream(client, ["BTCUSDT"], ["4h"], on_candle)
        await stream._poll("BTCUSDT", "4h")
        # first poll: only the forming bar's mark, no closed re-announcement
        self.assertEqual([c for c in seen if c[2]], [])
        self.assertTrue(any(not c[2] for c in seen))

        await stream._poll("BTCUSDT", "4h")
        self.assertEqual([c for c in seen if c[2]], [])

        previous_forming = client.forming_open
        client.advance()
        # the bar that was forming has now closed
        # closed only after the grace period has passed
        with mock.patch("time.time", return_value=(client.forming_open + 1000) / 1000):
            await stream._poll("BTCUSDT", "4h")
        self.assertEqual([c for c in seen if c[2]], [])
        grace = ingest.PollingKlineStream.CLOSE_GRACE_MS
        with mock.patch("time.time", return_value=(client.forming_open + grace + 1000) / 1000):
            await stream._poll("BTCUSDT", "4h")
            await stream._poll("BTCUSDT", "4h")
        closed = [c for c in seen if c[2]]
        self.assertEqual(closed, [("BTCUSDT", previous_forming, True)])


class FxTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fx._cache.clear()

    async def test_not_configured_is_not_an_error(self):
        with mock.patch.object(settings, "netarz_fx_key", ""):
            result = await fx.toman_rate()
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "not_configured")

    async def _run(self, handler):
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs.pop("transport", None)
            return real(*args, transport=transport, **kwargs)

        with mock.patch.object(settings, "netarz_fx_key", "fx-ntz-v1-test"), \
                mock.patch.object(fx.httpx, "AsyncClient", side_effect=client):
            return await fx.toman_rate("USD")

    async def test_rate_is_per_one_unit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer fx-ntz-v1-test")
            self.assertEqual(request.url.params["codes"], "USD")
            return httpx.Response(200, json={
                "data": [{"code": "USD", "unit": 1, "buy": 100000, "sell": 101000, "mid": 100500}],
                "meta": {"as_of": "2026-01-01T00:00:00Z", "plan": "free", "delayed_minutes": 15},
            })

        result = await self._run(handler)
        self.assertTrue(result["available"])
        self.assertEqual(result["mid"], 100500.0)
        self.assertEqual(result["plan"], "free")

    async def test_ip_refusal_passes_the_seen_ip_through(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "ip_not_allowed", "ip": "203.0.113.7"}})

        result = await self._run(handler)
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "ip_not_allowed")
        self.assertEqual(result["ip"], "203.0.113.7")


class GatewayProviderTest(unittest.TestCase):
    def test_gateways_accept_any_vendor_model(self):
        self.assertEqual(settings.llm_model_for("gisoo", "claude-sonnet-5-5"), "claude-sonnet-5-5")
        self.assertEqual(settings.llm_model_for("netarz", "gemini-3.5-flash"), "gemini-3.5-flash")
        self.assertEqual(settings.llm_model_for("gisoo", ""), "gpt-4o-mini")
        # a direct vendor still refuses another vendor's model name
        self.assertEqual(settings.llm_model_for("deepseek", "claude-opus-5"), "deepseek-chat")

    def test_gisoo_is_built_against_its_base_url(self):
        from app.llm import providers

        with mock.patch.object(settings, "gisoo_api_key", "sk-gisoo-v1-test"):
            provider = providers.build_provider("gisoo", "gpt-4o-mini")
        self.assertEqual(provider.name, "gisoo")
        self.assertTrue(provider._completions_url.startswith("https://gisoo.pro/api/v1/"))

    def test_auto_prefers_the_gateways(self):
        with mock.patch.object(settings, "llm_provider", "auto"), \
                mock.patch.object(settings, "gisoo_api_key", ""), \
                mock.patch.object(settings, "netarz_ai_api_key", "sk-ntz-v1-test"), \
                mock.patch.object(settings, "openai_api_key", "sk-test"):
            self.assertEqual(settings.active_llm_provider, "netarz")


if __name__ == "__main__":
    unittest.main()
