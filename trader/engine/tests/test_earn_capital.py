"""Simple Earn is capital, not a withdrawal.

Binance's auto-subscribe sweeps idle balances out of the spot wallet into
flexible Simple Earn. An engine that reads only the spot wallet records those
sweeps as withdrawals, shrinks the capital base, and turns a small trading
loss into a large closed-equity drawdown that trips the kill switch.

These tests pin the four properties that prevent it:

  * an account valuation counts Earn and never double-counts the `LD*` receipt
    tokens Binance leaves behind for it;
  * a sweep is not a capital flow;
  * an Earn book that cannot be read is *unknown*, not empty - it stands the
    gates down instead of inventing a withdrawal; and
  * coins Earn swept up are still held, and are redeemed before an exit.

Run inside the engine image (the app needs its dependencies):

    docker compose run --rm --no-deps \
        -v "$PWD/trader/engine/app:/srv/app:ro" -v "$PWD/trader/engine/tests:/srv/tests:ro" \
        engine python -m unittest discover -s tests -v

No database, no network: repositories and the exchange client are stubbed.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.config import settings
from app.exchange.binance_client import BinanceError
from app.exchange.earn import EarnSnapshot, is_receipt_token
from app.execution.executor import LiveExecutor
from app.risk.manager import AccountState
from app.workers.reconcile import Reconciler


# Simple Earn is only read with real money (paper mode never touches it), so
# these tests run as live whatever TRADING_MODE the environment has.
_LIVE = mock.patch.object(settings, "trading_mode", "live")


def setUpModule():
    _LIVE.start()


def tearDownModule():
    _LIVE.stop()


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------
class FakeClient:
    """Spot wallet plus a flexible Simple Earn book."""

    name = "binance"
    title = "Binance"
    supports_earn = True
    supports_stop_orders = True
    supports_fee_token = True
    supports_futures = True

    def __init__(self, *, spot=None, earn=None, earn_error: Exception | None = None,
                 prices=None):
        self.spot = dict(spot or {})
        self.earn = dict(earn or {})
        self.earn_error = earn_error
        self.prices = dict(prices or {})
        self.redemptions: list[tuple[str, float]] = []
        self.orders: list[dict] = []

    async def spot_balances(self):
        return dict(self.spot)

    async def free_balance(self, asset):
        return self.spot.get(asset, 0.0)

    async def ticker_price(self, symbol=None, market="spot"):
        if symbol not in self.prices:
            raise BinanceError(400, -1121, f"no market for {symbol}")
        return {"price": str(self.prices[symbol])}

    async def simple_earn_flexible_all(self):
        if self.earn_error:
            raise self.earn_error
        return {
            asset: {"amount": amount, "product_id": f"{asset}001",
                    "can_redeem": True, "auto_subscribe": True}
            for asset, amount in self.earn.items()
        }

    async def simple_earn_redeem(self, product_id, amount, *, fast=True):
        asset = product_id.replace("001", "")
        self.redemptions.append((asset, amount))
        self.earn[asset] = max(0.0, self.earn.get(asset, 0.0) - amount)
        self.spot[asset] = self.spot.get(asset, 0.0) + amount
        return {"success": True}

    async def create_order(self, **kw):
        self.orders.append(kw)
        qty = kw["quantity"]
        return {"status": "FILLED", "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * 1.0), "orderId": 1, "fills": []}


def _executor(client) -> LiveExecutor:
    return LiveExecutor(client, registry=SimpleNamespace())


async def _noop(*args, **kwargs):
    return None


# --------------------------------------------------------------------------
# receipt tokens
# --------------------------------------------------------------------------
class ReceiptTokenTest(unittest.TestCase):
    def test_ld_tokens_are_recognised(self):
        for asset in ("LDUSDT", "LDXRP", "LDBNB"):
            self.assertTrue(is_receipt_token(asset), asset)

    def test_real_assets_are_not(self):
        for asset in ("USDT", "XRP", "LD", "LINK", "LTC"):
            self.assertFalse(is_receipt_token(asset), asset)


# --------------------------------------------------------------------------
# valuation
# --------------------------------------------------------------------------
class AccountValuationTest(unittest.IsolatedAsyncioTestCase):
    async def test_earn_quote_is_counted_as_equity(self):
        """No free USDT, four positions, the cash parked in Earn."""
        client = FakeClient(
            spot={"XRP": 40.0, "ETH": 0.2, "TRX": 10.0, "ATOM": 1.5,
                  "LDUSDT": 880.0, "LDXRP": 0.05},
            earn={"USDT": 1000.0, "XRP": 0.05},
        )
        marks = {"XRPUSDT": 0.2358, "ETHUSDT": 112.0, "TRXUSDT": 1.18, "ATOMUSDT": 8.9}
        equity, free = await _executor(client).account_equity(None, marks)

        coins = 40.05 * 0.2358 + 0.2 * 112.0 + 10.0 * 1.18 + 1.5 * 8.9
        self.assertAlmostEqual(equity, 1000.0 + coins, places=4)
        # Without Earn this reads as a ~60 USDT account with nothing to spend -
        # the whole bug: every entry was refused for want of free balance.
        self.assertGreater(equity, 1000.0)
        self.assertAlmostEqual(free, 1000.0, places=4)

    async def test_receipt_tokens_are_not_counted_twice(self):
        """`LDUSDT` mirrors the Earn position; counting both doubles the money."""
        client = FakeClient(spot={"LDUSDT": 500.0}, earn={"USDT": 500.0})
        equity, _ = await _executor(client).account_equity(None, {})
        self.assertAlmostEqual(equity, 500.0, places=6)

    async def test_quote_balance_for_the_ledger_spans_both_wallets(self):
        client = FakeClient(spot={"USDT": 20.0, "LDUSDT": 80.0}, earn={"USDT": 80.0})
        executor = _executor(client)
        await executor.account_equity(None, {})
        self.assertAlmostEqual(executor.last_quote_balance(), 100.0, places=6)
        self.assertTrue(executor.balances_complete())

    async def test_an_unreadable_earn_book_is_unknown_not_empty(self):
        client = FakeClient(spot={"USDT": 20.0}, earn_error=BinanceError(418, -1003, "banned"))
        executor = _executor(client)
        equity, _ = await executor.account_equity(None, {})
        self.assertAlmostEqual(equity, 20.0, places=6)
        # The reading is flagged so it cannot become a peak or a capital flow.
        self.assertFalse(executor.balances_complete())

    async def test_a_stale_reading_survives_a_failure(self):
        client = FakeClient(spot={"USDT": 0.0}, earn={"USDT": 1000.0})
        executor = _executor(client)
        await executor.account_equity(None, {})
        client.earn_error = BinanceError(418, -1003, "banned")
        executor.earn.invalidate()
        equity, _ = await executor.account_equity(None, {})
        # The money is still counted from the last good read, but the reading
        # is untrusted until Earn answers again.
        self.assertAlmostEqual(equity, 1000.0, places=6)
        self.assertFalse(executor.balances_complete())


# --------------------------------------------------------------------------
# a sweep is not a capital flow
# --------------------------------------------------------------------------
class CapitalFlowDetectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reconciler = Reconciler(FakeClient(), registry=SimpleNamespace())
        self.recorded: list[dict] = []

    def _repo(self, *, expected: float, tolerance: float = 20.0):
        """Stub of the repository the detector talks to."""
        async def add_capital_flow(session, **kw):
            self.recorded.append(kw)
            return SimpleNamespace(**kw)

        return mock.patch.multiple(
            "app.workers.reconcile.repo",
            expected_free_balance=mock.AsyncMock(return_value=(expected, tolerance)),
            get_sync_state=mock.AsyncMock(
                return_value=SimpleNamespace(
                    balance_unexplained=0.0, balance_unexplained_since=None,
                    last_rest_sync=None,
                )
            ),
            update_sync_state=mock.AsyncMock(),
            settle_unexplained_balance=mock.AsyncMock(),
            capital_flow_exists=mock.AsyncMock(return_value=False),
            add_capital_flow=mock.AsyncMock(side_effect=add_capital_flow),
            capital_base=mock.AsyncMock(return_value=1000.0),
            log_event=mock.AsyncMock(),
        )

    async def test_a_sweep_into_earn_is_not_a_withdrawal(self):
        """650 USDT left the spot wallet for Earn. Not a flow."""
        earn = EarnSnapshot(holdings={"USDT": 650.0}, ok=True, known=True)
        with self._repo(expected=650.0):
            for _ in range(2):  # the detector needs two sweeps to agree
                await self.reconciler._detect_capital_flow(None, {"USDT": 0.0}, earn)
        self.assertEqual(self.recorded, [])

    async def test_a_real_withdrawal_is_still_caught(self):
        """The money is in neither wallet: that is a genuine flow."""
        earn = EarnSnapshot(holdings={"USDT": 0.0}, ok=True, known=True)
        with self._repo(expected=650.0):
            for _ in range(2):
                await self.reconciler._detect_capital_flow(None, {"USDT": 0.0}, earn)
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["kind"], "withdrawal")
        self.assertAlmostEqual(self.recorded[0]["amount"], 650.0, places=2)

    async def test_a_real_deposit_is_still_caught(self):
        earn = EarnSnapshot(holdings={"USDT": 0.0}, ok=True, known=True)
        with self._repo(expected=100.0):
            for _ in range(2):
                await self.reconciler._detect_capital_flow(None, {"USDT": 600.0}, earn)
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["kind"], "deposit")
        self.assertAlmostEqual(self.recorded[0]["amount"], 500.0, places=2)

    async def test_an_unreadable_earn_book_records_nothing(self):
        """Missing money of unknown size must not be classified as a flow."""
        earn = EarnSnapshot(holdings={}, ok=False, known=False)
        with self._repo(expected=650.0):
            for _ in range(3):
                await self.reconciler._detect_capital_flow(None, {"USDT": 0.0}, earn)
        self.assertEqual(self.recorded, [])


# --------------------------------------------------------------------------
# swept coins are still held
# --------------------------------------------------------------------------
class ExchangeHoldingTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_swept_fraction_still_counts_as_held(self):
        """XRP: 40.0 in spot, 0.05 swept into Earn, position is 40.05."""
        position = SimpleNamespace(symbol="XRPUSDT", market="spot", qty=40.05)
        earn = EarnSnapshot(holdings={"XRP": 0.05}, ok=True, known=True)
        held = await Reconciler._exchange_holding(position, {"XRP": 40.0}, {}, earn)
        self.assertGreaterEqual(held, position.qty)

    async def test_without_earn_the_position_looks_short(self):
        position = SimpleNamespace(symbol="XRPUSDT", market="spot", qty=40.05)
        held = await Reconciler._exchange_holding(position, {"XRP": 40.0}, {}, None)
        self.assertLess(held, position.qty)


# --------------------------------------------------------------------------
# redemption
# --------------------------------------------------------------------------
class RedemptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_entry_redeems_what_the_spot_wallet_lacks(self):
        client = FakeClient(spot={"USDT": 5.0}, earn={"USDT": 1000.0})
        executor = _executor(client)
        with mock.patch("app.execution.executor.asyncio.sleep", _noop):
            free = await executor._ensure_spot_funds(200.0)
        self.assertEqual(len(client.redemptions), 1)
        self.assertEqual(client.redemptions[0][0], "USDT")
        self.assertGreaterEqual(free, 200.0)

    async def test_no_redemption_when_the_wallet_already_pays(self):
        client = FakeClient(spot={"USDT": 500.0}, earn={"USDT": 1000.0})
        with mock.patch("app.execution.executor.asyncio.sleep", _noop):
            await _executor(client)._ensure_spot_funds(200.0)
        self.assertEqual(client.redemptions, [])

    async def test_an_exit_redeems_the_swept_fraction_first(self):
        client = FakeClient(spot={"XRP": 40.0}, earn={"XRP": 0.05})
        info = SimpleNamespace(step_size=0.0001)
        with mock.patch("app.execution.executor.asyncio.sleep", _noop):
            held = await _executor(client)._ensure_spot_holding("XRP", 40.05, info)
        self.assertEqual(len(client.redemptions), 1)
        self.assertGreaterEqual(held, 40.05)

    async def test_dust_below_a_lot_is_left_alone(self):
        """Redeeming less than one lot cannot change the sellable quantity."""
        client = FakeClient(spot={"XRP": 40.0}, earn={"XRP": 0.05})
        info = SimpleNamespace(step_size=0.1)
        held = await _executor(client)._ensure_spot_holding("XRP", 40.05, info)
        self.assertEqual(client.redemptions, [])
        self.assertEqual(held, 40.0)

    async def test_a_failed_redemption_is_not_fatal(self):
        client = FakeClient(spot={"USDT": 5.0}, earn={"USDT": 1000.0})
        executor = _executor(client)
        executor.earn.client = SimpleNamespace(
            supports_earn=True,
            simple_earn_flexible_all=client.simple_earn_flexible_all,
            simple_earn_redeem=mock.AsyncMock(side_effect=BinanceError(400, -6001, "redeem failed")),
        )
        with mock.patch("app.execution.executor.asyncio.sleep", _noop):
            free = await executor._ensure_spot_funds(200.0)
        self.assertEqual(free, 5.0)  # the order is still attempted; the exchange decides


# --------------------------------------------------------------------------
# the failure mode, as arithmetic
# --------------------------------------------------------------------------
class DrawdownRegressionTest(unittest.TestCase):
    """A kill switch that fires without a single new loss.

    `closed_drawdown_pct` is a since-inception result in dollars over the
    *current* capital base. Sweeps misread as withdrawals shrink the base and
    nothing else, so the same -20 USDT goes from a 2% reading to a 29% one.
    """

    REALIZED = -20.0           # the trading result, unchanged throughout
    TRUE_BASE = 1000.0         # base with the Earn money still counted
    PHANTOM_BASE = 68.0        # base after the sweeps became withdrawals

    def _state(self, base: float) -> AccountState:
        closed_equity = base + self.REALIZED
        return AccountState(
            equity=closed_equity, free_balance=0.0, peak_equity=base,
            open_positions=4, day_start_equity=closed_equity,
            closed_equity=closed_equity, closed_peak_equity=base,
        )

    def test_the_phantom_base_manufactures_the_drawdown(self):
        self.assertAlmostEqual(self._state(self.PHANTOM_BASE).closed_drawdown_pct, 29.4, delta=0.5)

    def test_the_true_base_is_nowhere_near_the_limit(self):
        reading = self._state(self.TRUE_BASE).closed_drawdown_pct
        self.assertAlmostEqual(reading, 2.0, delta=0.2)
        self.assertLess(reading, 20.0)

    def test_an_untrusted_reading_cannot_be_acted_on(self):
        state = self._state(self.PHANTOM_BASE)
        self.assertFalse(state.suspect)
        state.balances_complete = False
        self.assertTrue(state.suspect)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# a capital flow must not move the drawdown reading
# --------------------------------------------------------------------------
class ClosedPeakScalingTest(unittest.IsolatedAsyncioTestCase):
    """`closed_drawdown_pct` has to answer to trading, not to the account size.

    The peak is a level. Scaling it by what a flow does to closed equity keeps
    the ratio fixed across the flow; adding the flow to it does not, which is
    how withdrawing capital can manufacture a 30% drawdown out of a -2% account
    and stop the engine.
    """

    @staticmethod
    def _drawdown(peak: float, closed_equity: float) -> float:
        return AccountState(
            equity=closed_equity, free_balance=0.0, peak_equity=peak,
            open_positions=0, day_start_equity=closed_equity,
            closed_equity=closed_equity, closed_peak_equity=peak,
        ).closed_drawdown_pct

    async def test_scaling_keeps_the_reading_across_a_withdrawal(self):
        peak, closed = 1000.0, 980.0       # a 2.02% drawdown
        before = self._drawdown(peak, closed)

        withdrawal = -920.0                    # most of the account swept away
        scaled = peak * (closed + withdrawal) / closed
        after = self._drawdown(scaled, closed + withdrawal)

        self.assertAlmostEqual(before, after, places=6)
        self.assertLess(after, 20.0)

    async def test_adding_the_flow_instead_manufactures_the_drawdown(self):
        """The old arithmetic, kept as the thing this must never go back to."""
        peak, closed = 1000.0, 980.0
        withdrawal = -920.0
        shifted = peak + withdrawal            # what `base + closed_pnl_peak` did
        reading = self._drawdown(shifted, closed + withdrawal)
        # 25% - past a 20% kill switch, on no new loss at all.
        self.assertAlmostEqual(reading, 25.0, delta=0.5)
        self.assertGreater(reading, 20.0)

    async def test_scaling_keeps_the_reading_across_a_deposit(self):
        peak, closed = 1000.0, 980.0
        before = self._drawdown(peak, closed)
        deposit = 5000.0
        scaled = peak * (closed + deposit) / closed
        self.assertAlmostEqual(self._drawdown(scaled, closed + deposit), before, places=6)

    async def test_a_real_loss_still_shows_after_a_withdrawal(self):
        """Invariance must not blunt the gate: losses on the remaining capital count."""
        peak, closed = 1000.0, 1000.0
        scaled = peak * 100.0 / closed         # withdraw 900, peak scales to 100
        self.assertAlmostEqual(self._drawdown(scaled, 100.0), 0.0, places=6)
        # now lose a quarter of what is left
        self.assertAlmostEqual(self._drawdown(scaled, 75.0), 25.0, places=6)

    async def test_a_flow_that_empties_the_account_leaves_the_peak_alone(self):
        from app.db import repo
        stored = {"closed_peak_equity": 500.0}

        async def get_setting(session, key, default=None):
            return stored.get(key, default)

        async def set_setting(session, key, value):
            stored[key] = value

        with mock.patch.multiple(
            repo,
            get_setting=mock.AsyncMock(side_effect=get_setting),
            set_setting=mock.AsyncMock(side_effect=set_setting),
        ):
            peak = await repo.rescale_closed_peak(None, 400.0, -400.0)
        self.assertEqual(peak, 500.0)
        self.assertEqual(stored["closed_peak_equity"], 500.0)
