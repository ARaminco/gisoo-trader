"""Execution guarantees of the 4h core: exits that must happen, do happen.

Run inside the engine image (the app needs its dependencies):

    docker compose run --rm --no-deps \
        -v "$PWD/trader/engine/app:/srv/app:ro" -v "$PWD/trader/engine/tests:/srv/tests:ro" \
        engine python -m unittest discover -s tests -v

No database, no network: repositories and the exchange client are stubbed.
"""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from app.exchange.binance_client import BinanceError
from app.execution.executor import LiveExecutor
from app.risk.manager import EntryOverrides, RiskManager
from app.strategies import core4h
from app.workers import reconcile as reconcile_mod
from app.workers import trader as trader_mod
from app.workers.reconcile import Reconciler
from app.workers.trader import (
    CORE_BAR_MS, CORE_CATCHUP_MAX_BARS, DEFERRED_CHECK_ATTEMPTS, TradingEngine,
)


async def _noop(*args, **kwargs):
    return None


def _position(**overrides) -> SimpleNamespace:
    base = dict(
        id=6, symbol="LINKUSDT", side="long", market="spot", mode="live", status="open",
        qty=12.5, entry_price=12.40, exit_price=None, notional=155.0, leverage=1.0,
        stop_price=11.45, initial_stop=11.45, target_price=0.0, trail_active=False,
        high_water=12.40, fees_paid=0.12, realized_pnl=0.0, realized_pnl_pct=0.0,
        r_multiple=0.0, exit_reason=None, closed_at=None,
        opened_at=dt.datetime(2024, 1, 1, 4, 0, tzinfo=dt.timezone.utc),
        pending_exit_reason=None, pending_exit_price=0.0, pending_exit_at=None,
        stop_missing=False, stop_retry_at=None, exit_check_bar=None,
        meta={"strategy": "momentum", "stop_order_id": 1000000001},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _symbol_info() -> SimpleNamespace:
    return SimpleNamespace(
        base_asset="LINK", step_size=0.01, min_qty=0.01, min_notional=5.0,
        round_qty=lambda q: round(q, 2), round_price=lambda p: round(p, 3),
        ceil_qty=lambda q: round(q + 0.005, 2),
        is_tradable=lambda q, p: (True, ""),
    )


class FakeClient:
    """Just enough of BinanceClient for the executor and the reconciler."""

    name = "binance"
    title = "Binance"
    supports_earn = True
    supports_stop_orders = True
    supports_fee_token = True
    supports_futures = True

    def __init__(self, *, market_error: BinanceError | None = None, cancel_error: BinanceError | None = None,
                 order: dict | None = None, trades: list | None = None, query_error: BinanceError | None = None):
        self.market_error = market_error
        self.cancel_error = cancel_error
        self.query_error = query_error
        self.order = order or {}
        self.trades = trades or []
        self.calls: list[tuple] = []

    async def cancel_order(self, symbol, order_id, market="spot"):
        self.calls.append(("cancel", order_id))
        if self.cancel_error:
            raise self.cancel_error
        return {"status": "CANCELED"}

    async def free_balance(self, asset):
        return 12.5

    async def create_order(self, **kw):
        self.calls.append(("create", kw["order_type"]))
        if kw["order_type"] == "MARKET":
            if self.market_error:
                raise self.market_error
            return {"status": "FILLED", "executedQty": str(kw["quantity"]),
                    "cummulativeQuoteQty": str(kw["quantity"] * 11.5), "orderId": 1, "fills": []}
        return {"status": "NEW", "orderId": 999}

    async def query_order(self, symbol, order_id=None, market="spot", client_order_id=None):
        self.calls.append(("query", order_id))
        if self.query_error:
            raise self.query_error
        return self.order

    async def my_trades(self, symbol, order_id, market="spot"):
        return self.trades

    async def ticker_price(self, symbol=None, market="spot"):
        return {"price": "650.0"}


FILLED_STOP = {
    "status": "FILLED", "type": "STOP_LOSS_LIMIT", "price": "11.40", "stopPrice": "11.45",
    "executedQty": "12.5", "cummulativeQuoteQty": "143.125", "updateTime": 1700000000000,
}
BNB_FEE_TRADES = [
    {"price": "11.45", "qty": "12.5", "commission": "0.00015", "commissionAsset": "BNB"},
]


# --------------------------------------------------------------------------
# the bar clock
# --------------------------------------------------------------------------
class PendingBarsTest(unittest.TestCase):
    def test_first_run_takes_only_the_newest_bar(self):
        self.assertEqual(TradingEngine._pending_bars(None, 1_000 * CORE_BAR_MS), [1_000 * CORE_BAR_MS])

    def test_nothing_new(self):
        t = 1_000 * CORE_BAR_MS
        self.assertEqual(TradingEngine._pending_bars(t, t), [])
        self.assertEqual(TradingEngine._pending_bars(t + CORE_BAR_MS, t), [])

    def test_outage_across_closes_is_caught_up_in_order(self):
        last = 1_000 * CORE_BAR_MS
        newest = last + 3 * CORE_BAR_MS
        self.assertEqual(
            TradingEngine._pending_bars(last, newest),
            [last + CORE_BAR_MS, last + 2 * CORE_BAR_MS, newest],
        )

    def test_catch_up_is_capped(self):
        last = 1_000 * CORE_BAR_MS
        newest = last + (CORE_CATCHUP_MAX_BARS + 10) * CORE_BAR_MS
        bars = TradingEngine._pending_bars(last, newest)
        self.assertEqual(len(bars), CORE_CATCHUP_MAX_BARS)
        self.assertEqual(bars[-1], newest)


class HighWaterAndDeferralTest(unittest.TestCase):
    def _frame(self):
        t0 = 1_000 * CORE_BAR_MS
        return pd.DataFrame({
            "open_time": [t0, t0 + CORE_BAR_MS, t0 + 2 * CORE_BAR_MS],
            "high": [12.5, 12.9, 12.1], "low": [12.0, 12.3, 11.6], "close": [12.4, 12.6, 11.7],
        })

    def test_high_water_follows_the_bar_high_for_longs(self):
        p = _position(high_water=12.40)
        f = self._frame()
        TradingEngine._track_high_water(p, f, int(f.open_time[1]))
        self.assertAlmostEqual(p.high_water, 12.9)
        TradingEngine._track_high_water(p, f, int(f.open_time[2]))
        self.assertAlmostEqual(p.high_water, 12.9)  # never lowered

    def test_high_water_ignores_a_bar_that_is_not_stored(self):
        p = _position(high_water=12.40)
        TradingEngine._track_high_water(p, self._frame(), 5)
        self.assertAlmostEqual(p.high_water, 12.40)

    def test_deferral_keeps_the_oldest_bar(self):
        p = _position()
        TradingEngine._defer_exit_check(p, 2 * CORE_BAR_MS)
        TradingEngine._defer_exit_check(p, 3 * CORE_BAR_MS)
        self.assertEqual(p.exit_check_bar, 2 * CORE_BAR_MS)
        TradingEngine._defer_exit_check(p, CORE_BAR_MS)
        self.assertEqual(p.exit_check_bar, CORE_BAR_MS)

    def test_a_bar_older_than_the_entry_is_never_deferred(self):
        p = _position(meta={"strategy": "momentum", "entry_open_time": 5 * CORE_BAR_MS})
        TradingEngine._defer_exit_check(p, 4 * CORE_BAR_MS)
        self.assertIsNone(p.exit_check_bar)
        TradingEngine._defer_exit_check(p, 5 * CORE_BAR_MS)
        self.assertEqual(p.exit_check_bar, 5 * CORE_BAR_MS)

    def test_deferral_is_bounded(self):
        self.assertGreaterEqual(DEFERRED_CHECK_ATTEMPTS, 1)


# --------------------------------------------------------------------------
# sizing and the caps the study validated
# --------------------------------------------------------------------------
class FamilyCapTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = RiskManager()
        self.decision = SimpleNamespace(symbol="ABCUSDT", side="long", price=10.0, stop_price=9.4, target_price=0.0)
        self.state = SimpleNamespace(equity=1000.0, free_balance=800.0)
        self.rules = EntryOverrides.for_family(core4h.MOMENTUM)

    async def test_for_family_carries_the_validated_cap(self):
        self.assertEqual(self.rules.family, "momentum")
        self.assertEqual(self.rules.max_family_positions, 3)
        self.assertEqual(EntryOverrides.for_family(core4h.TURTLE).max_family_positions, 3)

    async def test_fourth_momentum_position_is_refused(self):
        with mock.patch.object(trader_mod.repo, "open_position_for", new=_noop), \
             mock.patch.object(trader_mod.repo, "open_positions_count", new=mock.AsyncMock(return_value=3)), \
             mock.patch.object(RiskManager, "check_global", new=mock.AsyncMock(return_value=(True, ""))), \
             mock.patch.object(RiskManager, "_session_blocked", new=staticmethod(lambda now=None: "")):
            verdict = await self.manager.evaluate_entry(None, self.decision, self.state, _symbol_info(), 1.0, self.rules)
        self.assertFalse(verdict.approved)
        self.assertIn("family max 3", verdict.reason)

    async def test_third_momentum_position_is_allowed(self):
        with mock.patch.object(trader_mod.repo, "open_position_for", new=_noop), \
             mock.patch.object(trader_mod.repo, "open_positions_count", new=mock.AsyncMock(return_value=2)), \
             mock.patch.object(trader_mod.repo, "last_loss_time", new=_noop), \
             mock.patch.object(RiskManager, "check_global", new=mock.AsyncMock(return_value=(True, ""))), \
             mock.patch.object(RiskManager, "_session_blocked", new=staticmethod(lambda now=None: "")):
            verdict = await self.manager.evaluate_entry(None, self.decision, self.state, _symbol_info(), 1.0, self.rules)
        self.assertTrue(verdict.approved, verdict.reason)
        self.assertGreater(verdict.qty, 0)

    async def test_already_holding_is_reported_before_the_account_gates(self):
        async def boom(*a, **k):
            raise AssertionError("check_global must not run for a symbol already held")
        with mock.patch.object(trader_mod.repo, "open_position_for", new=mock.AsyncMock(return_value=_position())), \
             mock.patch.object(RiskManager, "check_global", new=boom):
            verdict = await self.manager.evaluate_entry(None, self.decision, self.state, _symbol_info(), 1.0, self.rules)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "already holding ABCUSDT")

    async def test_cooldown_is_scoped_to_the_entering_family(self):
        seen: dict = {}

        async def last_loss(session, symbol, family=None):
            seen["family"] = family
            return None

        rules = EntryOverrides.for_family(core4h.TURTLE)
        with mock.patch.object(trader_mod.repo, "open_position_for", new=_noop), \
             mock.patch.object(trader_mod.repo, "open_positions_count", new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(trader_mod.repo, "last_loss_time", new=last_loss), \
             mock.patch.object(RiskManager, "check_global", new=mock.AsyncMock(return_value=(True, ""))), \
             mock.patch.object(RiskManager, "_session_blocked", new=staticmethod(lambda now=None: "")):
            await self.manager.evaluate_entry(None, self.decision, self.state, _symbol_info(), 1.0, rules)
        self.assertEqual(seen["family"], "turtle")

    async def test_size_multiplier_is_still_clamped(self):
        with mock.patch.object(trader_mod.repo, "open_position_for", new=_noop), \
             mock.patch.object(trader_mod.repo, "open_positions_count", new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(trader_mod.repo, "last_loss_time", new=_noop), \
             mock.patch.object(RiskManager, "check_global", new=mock.AsyncMock(return_value=(True, ""))), \
             mock.patch.object(RiskManager, "_session_blocked", new=staticmethod(lambda now=None: "")):
            full = await self.manager.evaluate_entry(None, self.decision, self.state, _symbol_info(), 1.0, self.rules)
            half = await self.manager.evaluate_entry(None, self.decision, self.state, _symbol_info(), 0.5, self.rules)
        self.assertAlmostEqual(half.risk_amount, full.risk_amount / 2, places=6)


# --------------------------------------------------------------------------
# the exchange-side floor
# --------------------------------------------------------------------------
class ExecutorStopRestoreTest(unittest.IsolatedAsyncioTestCase):
    def _executor(self, client) -> LiveExecutor:
        ex = LiveExecutor.__new__(LiveExecutor)
        ex.client = client
        ex.registry = None
        ex.mode = "live"
        ex._last_quote_balance = None
        ex._bnb_mark = (0.0, 0.0)
        return ex

    def _quiet(self):
        return (
            mock.patch.object(trader_mod.repo, "log_event", new=_noop),
            mock.patch.object(trader_mod.repo, "record_order", new=_noop),
            mock.patch.object(trader_mod.repo, "update_order_status", new=_noop),
        )

    async def test_failed_market_sell_puts_the_stop_back(self):
        client = FakeClient(market_error=BinanceError(400, -2010, "insufficient balance"))
        ex = self._executor(client)
        p = _position()
        a, b, c = self._quiet()
        with a, b, c:
            out = await ex.close_position(None, p, 11.5, "signal_exit", _symbol_info())
        self.assertEqual(out.status, "open")
        self.assertEqual(p.meta["stop_order_id"], 999)
        self.assertFalse(p.stop_missing)
        self.assertIn("close_failed_at", p.meta)
        self.assertEqual([c for c in client.calls if c[0] == "create"],
                         [("create", "MARKET"), ("create", "STOP_LOSS_LIMIT")])

    async def test_a_stop_that_already_filled_is_never_followed_by_a_second_sell(self):
        client = FakeClient(
            cancel_error=BinanceError(400, -2011, "Unknown order sent."),
            order=FILLED_STOP,
        )
        ex = self._executor(client)
        p = _position()
        a, b, c = self._quiet()
        with a, b, c:
            out = await ex.close_position(None, p, 11.5, "signal_exit", _symbol_info())
        self.assertEqual(out.status, "open")  # the sweep books it from the stop order
        self.assertNotIn(("create", "MARKET"), client.calls)
        self.assertIn("already filled", p.meta["close_error"])

    async def test_a_cancelled_by_hand_stop_still_lets_the_exit_through(self):
        client = FakeClient(
            cancel_error=BinanceError(400, -2011, "Unknown order sent."),
            order={"status": "CANCELED", "executedQty": "0"},
        )
        ex = self._executor(client)
        p = _position()
        a, b, c = self._quiet()
        with a, b, c:
            out = await ex.close_position(None, p, 11.5, "signal_exit", _symbol_info())
        self.assertEqual(out.status, "closed")
        self.assertIn(("create", "MARKET"), client.calls)

    async def test_partial_fill_is_throttled_like_a_failure(self):
        class PartialClient(FakeClient):
            async def create_order(self, **kw):
                self.calls.append(("create", kw["order_type"]))
                if kw["order_type"] == "MARKET":
                    return {"status": "EXPIRED", "executedQty": "5.0",
                            "cummulativeQuoteQty": "57.5", "orderId": 1, "fills": []}
                return {"status": "NEW", "orderId": 999}

        client = PartialClient()
        ex = self._executor(client)
        p = _position()
        a, b, c = self._quiet()
        with a, b, c:
            out = await ex.close_position(None, p, 11.5, "signal_exit", _symbol_info())
        self.assertEqual(out.status, "open")
        self.assertIn("close_failed_at", p.meta)  # the retry is throttled
        self.assertAlmostEqual(p.qty, 12.5 - 5.0, places=6)
        self.assertEqual(p.meta["stop_order_id"], 999)  # floor back under the remainder

    async def test_successful_exit_is_unchanged(self):
        client = FakeClient()
        ex = self._executor(client)
        p = _position()
        a, b, c = self._quiet()
        with a, b, c:
            out = await ex.close_position(None, p, 11.5, "signal_exit", _symbol_info())
        self.assertEqual(out.status, "closed")
        self.assertEqual(out.exit_reason, "signal_exit")
        self.assertNotIn(("create", "STOP_LOSS_LIMIT"), client.calls)

    async def test_ensure_places_a_missing_stop(self):
        client = FakeClient()
        ex = self._executor(client)
        p = _position(stop_missing=True, meta={"strategy": "momentum", "stop_order_id": None})
        with mock.patch.object(trader_mod.repo, "log_event", new=_noop), \
             mock.patch.object(trader_mod.repo, "record_order", new=_noop):
            placed = await ex.ensure_protective_stop(None, p, _symbol_info())
        self.assertTrue(placed)
        self.assertEqual(p.meta["stop_order_id"], 999)
        self.assertFalse(p.stop_missing)
        with mock.patch.object(trader_mod.repo, "log_event", new=_noop):
            self.assertFalse(await ex.ensure_protective_stop(None, p, _symbol_info()))

    async def test_a_rejected_stop_is_flagged_for_the_engine(self):
        class NoStopClient(FakeClient):
            async def create_order(self, **kw):
                self.calls.append(("create", kw["order_type"]))
                raise BinanceError(400, -2010, "stop rejected")

        ex = self._executor(NoStopClient())
        p = _position(stop_missing=False, meta={"strategy": "momentum", "stop_order_id": None})
        with mock.patch.object(trader_mod.repo, "log_event", new=_noop):
            placed = await ex.ensure_protective_stop(None, p, _symbol_info())
        self.assertFalse(placed)
        self.assertTrue(p.stop_missing)


# --------------------------------------------------------------------------
# the reconciler
# --------------------------------------------------------------------------
class ReconcilerStopBookingTest(unittest.IsolatedAsyncioTestCase):
    def _reconciler(self, client) -> Reconciler:
        r = Reconciler.__new__(Reconciler)
        r.client = client
        r.registry = None
        r._pending_flow = None
        r.synced_once = False
        return r

    async def test_exchange_stop_fill_is_booked_at_the_real_fill_with_fee(self):
        r = self._reconciler(FakeClient(order=FILLED_STOP, trades=BNB_FEE_TRADES))
        p = _position()
        with mock.patch.object(reconcile_mod.repo, "update_order_status", new=_noop), \
             mock.patch.object(reconcile_mod.repo, "record_order", new=_noop), \
             mock.patch.object(reconcile_mod.repo, "log_event", new=_noop), \
             mock.patch.object(reconcile_mod.bus, "publish", new=_noop):
            settled = await r._settle_via_stop_order(None, p)
        self.assertTrue(settled)
        self.assertEqual(p.status, "closed")
        self.assertEqual(p.exit_reason, "exchange_stop")
        self.assertAlmostEqual(p.exit_price, 11.45, places=6)
        self.assertAlmostEqual(p.fees_paid, 0.12 + 0.00015 * 650.0, places=6)
        expected_pnl = (11.45 - 12.40) * 12.5 - p.fees_paid
        self.assertAlmostEqual(p.realized_pnl, expected_pnl, places=6)
        self.assertAlmostEqual(p.r_multiple, expected_pnl / ((12.40 - 11.45) * 12.5), places=6)
        self.assertEqual(p.closed_at, dt.datetime.fromtimestamp(1700000000000 / 1000, tz=dt.timezone.utc))

    async def test_unfilled_stop_is_not_booked(self):
        r = self._reconciler(FakeClient(order={"status": "CANCELED", "executedQty": "0"}))
        p = _position()
        self.assertFalse(await r._settle_via_stop_order(None, p))
        self.assertEqual(p.status, "open")

    async def test_a_transient_read_failure_waits_instead_of_booking_at_the_mark(self):
        r = self._reconciler(FakeClient(query_error=BinanceError(0, None, "timeout")))
        p = _position()
        for attempt in range(1, reconcile_mod.SETTLE_READ_ATTEMPTS):
            self.assertTrue(await r._settle_via_stop_order(None, p), attempt)
            self.assertEqual(p.status, "open")
        # ... but a stale id cannot keep the row open for ever.
        self.assertFalse(await r._settle_via_stop_order(None, p))

    async def test_missing_stop_is_flagged_for_the_engine(self):
        r = self._reconciler(FakeClient())
        p = _position(meta={"strategy": "momentum"})
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.repo, "log_event", new=_noop):
            await r._check_protective_stop(None, p, [], flags)
        self.assertTrue(p.stop_missing)
        self.assertEqual(flags, ["LINKUSDT:no_stop"])

    async def test_an_orphan_stop_of_the_same_symbol_is_adopted_not_duplicated(self):
        r = self._reconciler(FakeClient())
        p = _position(meta={"strategy": "momentum"})
        resting = [{"orderId": 4242, "symbol": "LINKUSDT", "clientOrderId": "gtrs-abc",
                    "type": "STOP_LOSS_LIMIT", "origQty": "12.5", "price": "11.40"}]
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.repo, "log_event", new=_noop):
            await r._check_protective_stop(None, p, resting, flags)
        self.assertFalse(p.stop_missing)
        self.assertEqual(p.meta["stop_order_id"], 4242)
        self.assertEqual(flags, ["LINKUSDT:stop_adopted"])

    async def test_cancelled_stop_is_flagged_for_the_engine(self):
        r = self._reconciler(FakeClient(order={"status": "CANCELED"}))
        p = _position()
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.repo, "log_event", new=_noop), \
             mock.patch.object(reconcile_mod.repo, "update_order_status", new=_noop):
            await r._check_protective_stop(None, p, [], flags)
        self.assertTrue(p.stop_missing)

    async def test_a_stop_that_filled_closes_the_row_even_when_coins_are_still_held(self):
        r = self._reconciler(FakeClient(order=FILLED_STOP, trades=BNB_FEE_TRADES))
        p = _position()
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.repo, "log_event", new=_noop), \
             mock.patch.object(reconcile_mod.repo, "update_order_status", new=_noop), \
             mock.patch.object(reconcile_mod.repo, "record_order", new=_noop), \
             mock.patch.object(reconcile_mod.bus, "publish", new=_noop):
            await r._check_protective_stop(None, p, [], flags)
        self.assertEqual(p.status, "closed")
        self.assertEqual(p.exit_reason, "exchange_stop")
        self.assertEqual(flags, ["LINKUSDT:stop_filled"])

    async def test_gapped_stop_limit_hands_the_exit_to_the_engine(self):
        r = self._reconciler(FakeClient())
        p = _position()
        resting = [{"orderId": 1000000001, "symbol": "LINKUSDT", "price": "11.40",
                    "stopPrice": "11.45", "type": "STOP_LOSS_LIMIT"}]
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.repo, "log_event", new=_noop), \
             mock.patch.object(reconcile_mod.bus, "get_marks", new=mock.AsyncMock(return_value={"LINKUSDT": 11.30})):
            await r._check_protective_stop(None, p, resting, flags)
        self.assertEqual(p.pending_exit_reason, "stop_loss")
        self.assertAlmostEqual(p.pending_exit_price, 11.30)
        self.assertEqual(flags, ["LINKUSDT:stop_gapped"])

    async def test_resting_stop_above_the_market_is_left_alone(self):
        r = self._reconciler(FakeClient())
        p = _position()
        resting = [{"orderId": 1000000001, "symbol": "LINKUSDT", "price": "11.40",
                    "stopPrice": "11.45", "type": "STOP_LOSS_LIMIT"}]
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.bus, "get_marks", new=mock.AsyncMock(return_value={"LINKUSDT": 11.70})):
            await r._check_protective_stop(None, p, resting, flags)
        self.assertIsNone(p.pending_exit_reason)
        self.assertFalse(p.stop_missing)
        self.assertEqual(flags, [])

    async def test_an_order_of_another_symbol_never_counts_as_our_stop(self):
        r = self._reconciler(FakeClient(order={"status": "CANCELED"}))
        p = _position()
        other = [{"orderId": 1000000001, "symbol": "ATOMUSDT", "price": "1.70",
                  "stopPrice": "1.706", "type": "STOP_LOSS_LIMIT"}]
        flags: list[str] = []
        with mock.patch.object(reconcile_mod.repo, "log_event", new=_noop), \
             mock.patch.object(reconcile_mod.repo, "update_order_status", new=_noop):
            await r._check_protective_stop(None, p, other, flags)
        self.assertIsNone(p.pending_exit_reason)  # no spurious market exit
        self.assertTrue(p.stop_missing)           # ours is gone, so re-place it


# --------------------------------------------------------------------------
# exits that the exchange refused
# --------------------------------------------------------------------------
class StickyExitTest(unittest.IsolatedAsyncioTestCase):
    def _engine(self, executor) -> TradingEngine:
        engine = TradingEngine.__new__(TradingEngine)
        engine.executor = executor
        engine.registry = SimpleNamespace(get=mock.AsyncMock(return_value=_symbol_info()))
        return engine

    async def test_refused_exit_is_marked_pending_and_retried(self):
        attempts: list[str] = []

        async def refuse(session, position, price, reason, symbol_info):
            attempts.append(reason)
            position.meta = {**position.meta, "close_failed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            return position

        engine = self._engine(SimpleNamespace(close_position=refuse))
        p = _position()
        events: list[str] = []

        async def log_event(session, message, **kw):
            events.append(message)

        with mock.patch.object(trader_mod.repo, "log_event", new=log_event):
            await engine._close_position(None, p, 11.5, "signal_exit")
        self.assertEqual(p.pending_exit_reason, "signal_exit")
        self.assertAlmostEqual(p.pending_exit_price, 11.5)
        self.assertIsNotNone(p.pending_exit_at)
        self.assertTrue(any("could not be executed" in e for e in events))
        # Inside CLOSE_RETRY_SECONDS the retry is throttled ...
        with mock.patch.object(trader_mod.repo, "log_event", new=log_event):
            await engine._close_position(None, p, 11.4, "signal_exit")
        self.assertEqual(attempts, ["signal_exit"])
        # ... a manual close always goes.
        with mock.patch.object(trader_mod.repo, "log_event", new=log_event):
            await engine._close_position(None, p, 11.4, "manual", force=True)
        self.assertEqual(attempts, ["signal_exit", "manual"])
        # The first pending reason is kept: the rule that fired is what gets reported.
        self.assertEqual(p.pending_exit_reason, "signal_exit")

    async def test_a_completed_exit_clears_every_execution_flag(self):
        async def succeed(session, position, price, reason, symbol_info):
            position.status = "closed"
            position.exit_price = price
            position.exit_reason = reason
            position.realized_pnl = -1.0
            position.realized_pnl_pct = -1.0
            position.r_multiple = -0.5
            position.qty = 12.5
            position.side = "long"
            position.symbol = "LINKUSDT"
            position.id = 6
            return position

        engine = self._engine(SimpleNamespace(close_position=succeed))
        p = _position(pending_exit_reason="signal_exit", pending_exit_price=11.5,
                      stop_missing=True, exit_check_bar=123, meta={"strategy": "momentum"})
        with mock.patch.object(trader_mod.repo, "log_event", new=_noop), \
             mock.patch.object(trader_mod.notify, "emit", new=lambda *a, **k: None), \
             mock.patch.object(trader_mod.bus, "publish", new=_noop):
            await engine._close_position(None, p, 11.4, "signal_exit", force=True)
        self.assertEqual(p.status, "closed")
        self.assertIsNone(p.pending_exit_reason)
        self.assertIsNone(p.exit_check_bar)
        self.assertFalse(p.stop_missing)


class LlmSizingCapTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_risk_on_market_review_cannot_raise_the_multiplier(self):
        """The heartbeat's own clamp, exercised on the real expression."""
        engine = TradingEngine.__new__(TradingEngine)
        engine._market_stance = "neutral"
        engine._risk_multiplier = 1.0
        for raw, expected in ((1.5, 1.0), (1.25, 1.0), (1.0, 1.0), (0.8, 0.8), (None, 1.0)):
            engine._risk_multiplier = min(1.0, float(raw or 1.0))
            self.assertAlmostEqual(engine._risk_multiplier, expected)

    def test_both_advisor_paths_clamp_at_one(self):
        import inspect
        src = inspect.getsource(trader_mod.TradingEngine)
        self.assertIn("min(1.0, float(review.risk_multiplier or 1.0))", src)
        self.assertIn("min(1.0, float(review.size_multiplier or 1.0))", src)


if __name__ == "__main__":
    unittest.main()
