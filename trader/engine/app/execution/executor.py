"""Order execution.

Two backends behind one interface:
  * LiveExecutor  - sends real orders to the selected exchange (spot; futures on Binance)
  * PaperExecutor - fills against the current price with modelled fees/slippage

Both write the same rows to `orders` and `positions`, so the panel, the metrics
and the backtester cannot tell them apart.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo
from app.db.models import Position, utcnow
from app.exchange.base import EXIT_PREFIX, ORDER_PREFIX, STOP_PREFIX, ExchangeClient, ExchangeError
from app.exchange.earn import EarnBook, is_receipt_token
from app.exchange.symbols import SymbolInfo, SymbolRegistry
from app.risk.manager import RiskVerdict
from app.strategies.ensemble import Decision

log = logging.getLogger(__name__)

# A market entry fills a little above the price it was sized at, and the
# exchange checks the balance against the fill. Redeem this much extra from Earn.
ENTRY_FUNDING_BUFFER = 0.005
# Fast redemption is near-instant; these bound the wait for it to land.
ENTRY_FUNDING_POLLS = 6
ENTRY_FUNDING_POLL_SECONDS = 0.5

# Modelled slippage for paper fills, in percent of price.
PAPER_SLIPPAGE_PCT = 0.02

# A market order some exchanges acknowledge before it matches (status NEW) is
# polled this long for a final state, then whatever still rests is cancelled.
FINAL_ORDER_STATES = ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH")
SETTLE_POLLS = 10
SETTLE_POLL_SECONDS = 0.5
# A "base-asset" commission above this share of the fill cannot be one: the
# exchange reported it in another unit, so it is valued as quote instead of
# being subtracted from the coins received.
MAX_BASE_FEE_FRACTION = 0.01


class ExecutionError(RuntimeError):
    pass


def _entry_meta(decision: Decision, verdict: RiskVerdict) -> dict:
    """What a position remembers about its entry.

    `strategy` names the family that opened it (momentum / turtle for the 4h
    core, ensemble for the old core) so the right exit rule is applied for
    the life of the position, whatever core is configured later.
    """
    return {
        "strategy": getattr(decision, "family", "ensemble"),
        "entry_open_time": getattr(decision, "open_time", None),
        "regime": decision.regime,
        "score": decision.score,
        "ml_probability": decision.ml_probability,
        "risk_amount": verdict.risk_amount,
        "size_multiplier": verdict.size_multiplier,
    }


class Executor(ABC):
    mode: str = "paper"

    @abstractmethod
    async def open_position(
        self,
        session: AsyncSession,
        decision: Decision,
        verdict: RiskVerdict,
        symbol_info: SymbolInfo,
        signal_id: int | None = None,
    ) -> Position | None:
        ...

    @abstractmethod
    async def close_position(
        self,
        session: AsyncSession,
        position: Position,
        price: float,
        reason: str,
        symbol_info: SymbolInfo,
    ) -> Position:
        ...

    @abstractmethod
    async def account_equity(self, session: AsyncSession, marks: dict[str, float]) -> tuple[float, float]:
        """Return (equity, free_balance) in the quote asset.

        `free_balance` is what an entry may spend: the spot wallet plus the
        quote asset held in flexible Simple Earn, which the entry path redeems
        on demand.
        """

    def last_quote_balance(self) -> float | None:
        """Total quote balance (free + locked) behind the last `account_equity`.

        The risk manager and the equity loop check it against the trade
        history to tell whether that equity contains an unclassified deposit
        or withdrawal. None when there is no exchange reading (paper).

        It counts the quote asset held in Simple Earn as well as the spot
        wallet: auto-subscribe moves money between the two without the user
        doing anything, and only the sum can be compared with what the trade
        history predicts.
        """
        return None

    def balances_complete(self) -> bool:
        """True when the last valuation saw every wallet it needs.

        False after a Simple Earn read failed: the equity is then missing an
        unknown amount of the user's money, so it may not raise a peak and
        the difference it shows may not be recorded as a capital flow.
        """
        return True

    async def ensure_protective_stop(
        self, session: AsyncSession, position: Position, symbol_info: SymbolInfo
    ) -> bool:
        """Re-place the exchange-side stop if the position has none (live only)."""
        return False

    # -- shared bookkeeping ------------------------------------------------

    @staticmethod
    def _finalise(
        position: Position, exit_price: float, exit_fee: float, reason: str
    ) -> Position:
        long = position.side == "long"
        gross = (
            (exit_price - position.entry_price) * position.qty
            if long
            else (position.entry_price - exit_price) * position.qty
        )
        position.exit_price = exit_price
        position.fees_paid = (position.fees_paid or 0.0) + exit_fee
        position.realized_pnl = gross - position.fees_paid  # net of both legs
        cost_basis = position.entry_price * position.qty
        position.realized_pnl_pct = (
            position.realized_pnl / cost_basis * 100 if cost_basis else 0.0
        )
        risk = abs(position.entry_price - position.initial_stop) * position.qty
        position.r_multiple = position.realized_pnl / risk if risk else 0.0
        position.status = "closed"
        position.closed_at = utcnow()
        position.exit_reason = reason
        return position


class PaperExecutor(Executor):
    """Simulated fills. Equity is tracked in the database, not on the exchange."""

    mode = "paper"

    def __init__(self, starting_balance: float | None = None):
        self.starting_balance = starting_balance or settings.initial_capital_usdt

    async def open_position(
        self,
        session: AsyncSession,
        decision: Decision,
        verdict: RiskVerdict,
        symbol_info: SymbolInfo,
        signal_id: int | None = None,
    ) -> Position | None:
        slip = 1 + PAPER_SLIPPAGE_PCT / 100 * (1 if decision.side == "long" else -1)
        fill_price = symbol_info.round_price(decision.price * slip)
        notional = fill_price * verdict.qty
        fee = notional * settings.taker_fee_pct / 100

        position = Position(
            symbol=decision.symbol,
            market="futures" if decision.side == "short" else "spot",
            side=decision.side,
            status="open",
            mode=self.mode,
            qty=verdict.qty,
            entry_price=fill_price,
            notional=notional,
            leverage=settings.futures_leverage if decision.side == "short" else 1.0,
            stop_price=verdict.stop_price,
            target_price=verdict.target_price,
            initial_stop=verdict.stop_price,
            high_water=fill_price,
            fees_paid=fee,
            signal_id=signal_id,
            meta=_entry_meta(decision, verdict),
        )
        session.add(position)
        await session.flush()

        await repo.record_order(
            session,
            position_id=position.id,
            symbol=decision.symbol,
            market=position.market,
            side="BUY" if decision.side == "long" else "SELL",
            type="MARKET",
            intent="entry",
            qty=verdict.qty,
            price=fill_price,
            filled_qty=verdict.qty,
            avg_price=fill_price,
            fee=fee,
            status="FILLED",
            mode=self.mode,
            exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
        return position

    async def close_position(
        self,
        session: AsyncSession,
        position: Position,
        price: float,
        reason: str,
        symbol_info: SymbolInfo,
    ) -> Position:
        slip = 1 - PAPER_SLIPPAGE_PCT / 100 * (1 if position.side == "long" else -1)
        fill_price = symbol_info.round_price(price * slip)
        fee = fill_price * position.qty * settings.taker_fee_pct / 100

        await repo.record_order(
            session,
            position_id=position.id,
            symbol=position.symbol,
            market=position.market,
            side="SELL" if position.side == "long" else "BUY",
            type="MARKET",
            intent="exit",
            qty=position.qty,
            price=fill_price,
            filled_qty=position.qty,
            avg_price=fill_price,
            fee=fee,
            status="FILLED",
            mode=self.mode,
            exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
        return self._finalise(position, fill_price, fee, reason)

    async def account_equity(
        self, session: AsyncSession, marks: dict[str, float]
    ) -> tuple[float, float]:
        # Paper rows only: a paper run started after a testnet/live run must
        # not inherit the other mode's PnL or its open positions.
        realized = await repo.realized_pnl_total(session, mode=self.mode)
        balance = self.starting_balance + realized
        open_positions = await repo.open_positions(session, mode=self.mode)
        locked = 0.0
        unrealized = 0.0
        for position in open_positions:
            locked += position.entry_price * position.qty
            mark = marks.get(position.symbol, position.entry_price)
            direction = 1 if position.side == "long" else -1
            unrealized += (mark - position.entry_price) * position.qty * direction

        free = max(0.0, balance - locked)
        equity = balance + unrealized
        return equity, free


class LiveExecutor(Executor):
    """Real orders. `mode` is 'live' or 'testnet' depending on configuration."""

    def __init__(self, client: ExchangeClient, registry: SymbolRegistry):
        self.client = client
        self.registry = registry
        self.mode = settings.trading_mode
        self._bnb_mark: tuple[float, float] = (0.0, 0.0)  # (price, monotonic time)
        self._last_quote_balance: float | None = None
        self.earn = EarnBook(client)
        self._balances_complete = True

    async def open_position(
        self,
        session: AsyncSession,
        decision: Decision,
        verdict: RiskVerdict,
        symbol_info: SymbolInfo,
        signal_id: int | None = None,
    ) -> Position | None:
        market = "futures" if decision.side == "short" else "spot"
        side = "BUY" if decision.side == "long" else "SELL"
        client_id = f"{ORDER_PREFIX}{uuid.uuid4().hex[:20]}"

        if market == "futures":
            await self._prepare_futures(decision.symbol)
        elif side == "BUY":
            # Auto-subscribe may have swept the cash this entry was sized
            # against into Simple Earn. Bring back what the order needs before
            # sending it, or Binance rejects it for insufficient balance.
            await self._ensure_spot_funds(verdict.notional)

        try:
            response = await self.client.create_order(
                symbol=decision.symbol,
                side=side,
                order_type="MARKET",
                market=market,
                quantity=verdict.qty,
                client_order_id=client_id,
            )
        except ExchangeError as exc:
            await repo.record_order(
                session,
                symbol=decision.symbol,
                market=market,
                side=side,
                type="MARKET",
                intent="entry",
                qty=verdict.qty,
                price=decision.price,
                status="REJECTED",
                mode=self.mode,
                error=str(exc),
            )
            await repo.log_event(
                session,
                f"Entry rejected for {decision.symbol}: {exc}",
                level="error",
                source="executor",
            )
            return None

        # An ACK-style answer (status NEW) does not mean no fill: wait for a
        # final state, cancel what still rests, book exactly what filled.
        response = await self._settle_order(session, decision.symbol, market, response)
        filled_qty, avg_price, fee = self._parse_fill(
            response, decision.price, symbol_info.base_asset, await self._bnb_price_for(response)
        )
        if filled_qty <= 0:
            await repo.log_event(
                session,
                f"Order for {decision.symbol} returned no fill; skipping.",
                level="warn",
                source="executor",
                data={"order_id": response.get("orderId"), "status": response.get("status")},
            )
            return None

        notional = avg_price * filled_qty
        position = Position(
            symbol=decision.symbol,
            market=market,
            side=decision.side,
            status="open",
            mode=self.mode,
            qty=filled_qty,
            entry_price=avg_price,
            notional=notional,
            leverage=settings.futures_leverage if market == "futures" else 1.0,
            stop_price=verdict.stop_price,
            target_price=verdict.target_price,
            initial_stop=verdict.stop_price,
            high_water=avg_price,
            fees_paid=fee,
            signal_id=signal_id,
            exchange_order_id=str(response.get("orderId", client_id)),
            meta={**_entry_meta(decision, verdict), "client_order_id": client_id},
        )
        session.add(position)
        await session.flush()

        await repo.record_order(
            session,
            position_id=position.id,
            symbol=decision.symbol,
            market=market,
            side=side,
            type="MARKET",
            intent="entry",
            qty=verdict.qty,
            price=decision.price,
            filled_qty=filled_qty,
            avg_price=avg_price,
            fee=fee,
            status=response.get("status", "FILLED"),
            mode=self.mode,
            exchange_order_id=str(response.get("orderId", client_id)),
            raw=_json_safe(response),
        )

        # Protective stop on the exchange, so a crashed engine still has a floor.
        await self._place_protective_stop(session, position, symbol_info)
        return position

    async def close_position(
        self,
        session: AsyncSession,
        position: Position,
        price: float,
        reason: str,
        symbol_info: SymbolInfo,
    ) -> Position:
        side = "SELL" if position.side == "long" else "BUY"
        cancel_state = await self._cancel_open_orders(session, position)
        if cancel_state == "gone" and await self._stop_already_filled(session, position):
            # The protective stop sold the position moments ago. Selling again
            # would sell coins of the same asset the user holds outside the
            # bot; the REST sweep books this row from the stop order.
            self._note_close_failure(position, "protective stop already filled on the exchange")
            await repo.log_event(
                session,
                f"{position.symbol}: the protective stop already filled; no exit order was sent. "
                "Reconciliation will close the position at the stop's fill price.",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )
            return position

        qty = symbol_info.round_qty(position.qty)
        if position.market == "spot":
            # Sell what the wallet actually holds. A base-asset commission on
            # the entry, or a stop that partially filled, leaves less than
            # `position.qty`, and Binance rejects an oversized SELL (-2010).
            try:
                held = await self._ensure_spot_holding(
                    symbol_info.base_asset, position.qty, symbol_info
                )
                qty = symbol_info.round_qty(min(position.qty, held))
            except ExchangeError as exc:
                log.warning(
                    "could not read %s balance before exit: %s", symbol_info.base_asset, exc
                )
        if qty <= 0:
            # Nothing left to sell: the exchange stop (or a manual sale) already
            # took it. The user stream or the REST sweep will close the row.
            self._note_close_failure(position, "nothing to sell: base balance is zero")
            if cancel_state == "cancelled":
                # The row must not keep pointing at an order we cancelled.
                position.meta = {**(position.meta or {}), "stop_order_id": None}
            await repo.log_event(
                session,
                f"{position.symbol}: no {symbol_info.base_asset} left to sell; "
                "waiting for reconciliation to close the position.",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )
            return position

        try:
            response = await self.client.create_order(
                symbol=position.symbol,
                side=side,
                order_type="MARKET",
                market=position.market,
                quantity=qty,
                reduce_only=True if position.market == "futures" else None,
                client_order_id=f"{EXIT_PREFIX}{uuid.uuid4().hex[:19]}",
            )
            response = await self._settle_order(session, position.symbol, position.market, response)
        except ExchangeError as exc:
            # Do NOT flip to "error": the coins are still on the exchange, or
            # the protective stop just sold them. Leave the row open so the next
            # tick, the user stream (FILLED report) or the REST sweep finishes it.
            self._note_close_failure(position, str(exc))
            await repo.log_event(
                session,
                f"Failed to close {position.symbol}: {exc}",
                level="error",
                source="executor",
                data={
                    "position_id": position.id,
                    "attempts": position.meta.get("close_attempts"),
                },
            )
            # The stop was cancelled a moment ago and the coins are still
            # here: put the floor back before anything else.
            if cancel_state == "cancelled":
                await self._restore_protective_stop(session, position, symbol_info)
            return position

        filled_qty, avg_price, fee = self._parse_fill(
            response, price, symbol_info.base_asset, await self._bnb_price_for(response)
        )
        await repo.record_order(
            session,
            position_id=position.id,
            symbol=position.symbol,
            market=position.market,
            side=side,
            type="MARKET",
            intent="exit",
            qty=qty,
            price=price,
            filled_qty=filled_qty,
            avg_price=avg_price,
            fee=fee,
            status=response.get("status", "FILLED"),
            mode=self.mode,
            exchange_order_id=str(response.get("orderId", "")),
            raw=_json_safe(response),
        )
        if filled_qty <= 0:
            self._note_close_failure(position, f"exit order {response.get('status')} with no fill")
            await repo.log_event(
                session,
                f"Exit order for {position.symbol} did not fill "
                f"({response.get('status')}); will retry.",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )
            if cancel_state == "cancelled":
                await self._restore_protective_stop(session, position, symbol_info)
            return position
        if filled_qty < qty - symbol_info.step_size:
            # Partial exit (an EXPIRED market order on a thin book): keep the
            # remainder open for the next tick. The sold part is on the order
            # row; the round trip is booked when the last piece goes.
            position.qty = round(position.qty - filled_qty, 8)
            position.fees_paid = (position.fees_paid or 0.0) + fee
            position.meta = {
                **(position.meta or {}),
                "partial_exit": filled_qty,
                "partial_exit_price": avg_price,
            }
            await repo.log_event(
                session,
                f"Partial exit on {position.symbol}: {filled_qty}/{qty}",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )
            # Throttle the retry of the remainder like any other failed exit.
            self._note_close_failure(position, f"partial exit {filled_qty}/{qty}")
            if cancel_state == "cancelled":
                await self._restore_protective_stop(session, position, symbol_info)
            return position
        return self._finalise(position, avg_price or price, fee, reason)

    async def account_equity(
        self, session: AsyncSession, marks: dict[str, float]
    ) -> tuple[float, float]:
        quote = settings.quote_asset
        balances = await self.client.spot_balances()
        free = await self.client.free_balance(quote)

        # Simple Earn holds trading capital that auto-subscribe swept out of the
        # spot wallet. It is the user's money, redeemable on demand, and it
        # belongs in the valuation - leaving it out turns sweeps into phantom
        # withdrawals and can stop the engine on a drawdown that never happened.
        earn = await self.earn.read()
        self._balances_complete = earn.trustworthy
        if not earn.trustworthy:
            log.warning(
                "valuing the account without a trusted Simple Earn reading; "
                "peaks and capital-flow detection stand down until it returns"
            )

        equity = balances.get(quote, 0.0) + earn.quote
        # What the trade history is compared against: both wallets, because
        # money crosses between them on its own.
        self._last_quote_balance = equity
        # Sizing spends this. Quote in Earn is redeemable on demand and the
        # entry path redeems what an order needs, so it is investable cash -
        # counting only the spot wallet leaves the engine unable to size
        # anything the moment auto-subscribe has swept it (free reads 0.00
        # with the whole cash balance sitting in Earn).
        free += earn.quote
        holdings: dict[str, float] = {
            asset: amount
            for asset, amount in balances.items()
            # `LD<asset>` is Binance's receipt token for an Earn position; the
            # position itself is already counted, so counting the token too
            # would double the money.
            if amount > 0 and asset != quote and not is_receipt_token(asset)
        }
        for asset, amount in earn.holdings.items():
            if asset != quote and amount > 0:
                holdings[asset] = holdings.get(asset, 0.0) + amount
        for asset, amount in holdings.items():
            symbol = f"{asset}{quote}"
            mark = marks.get(symbol)
            if mark is None:
                try:
                    ticker = await self.client.ticker_price(symbol)
                    mark = float(ticker["price"])
                except Exception:  # noqa: BLE001 - dust in an unlisted asset
                    continue
            equity += amount * mark

        if settings.futures_enabled and self.client.supports_futures:
            try:
                account = await self.client.futures_account()
                equity += float(account.get("totalWalletBalance", 0)) + float(
                    account.get("totalUnrealizedProfit", 0)
                )
            except ExchangeError as exc:
                log.warning("futures balance unavailable: %s", exc)

        return equity, free

    def last_quote_balance(self) -> float | None:
        return self._last_quote_balance

    def balances_complete(self) -> bool:
        return self._balances_complete

    # -- Simple Earn ------------------------------------------------------
    async def _settle_order(
        self, session: AsyncSession, symbol: str, market: str, response: dict
    ) -> dict:
        """Wait for a market order to reach a final state; cancel what still rests.

        Several exchanges acknowledge a market order before it matches. Giving
        up on it while it rests would let it fill later: coins with no position
        row and no stop. So poll until it is final, then cancel the remainder
        and re-read it - the caller books exactly what filled.
        """
        order_id = response.get("orderId")
        status = str(response.get("status") or "").upper()
        if not order_id or status in FINAL_ORDER_STATES:
            return response
        for _ in range(SETTLE_POLLS):
            await asyncio.sleep(SETTLE_POLL_SECONDS)
            try:
                response = self._merge(response, await self.client.query_order(symbol, order_id, market))
            except ExchangeError as exc:
                log.warning("could not poll order %s: %s", order_id, exc)
                continue
            if str(response.get("status") or "").upper() in FINAL_ORDER_STATES:
                return response
        try:
            await self.client.cancel_order(symbol, order_id, market)
        except ExchangeError as exc:
            if not exc.is_unknown_order:
                await repo.log_event(
                    session,
                    f"Order {order_id} on {symbol} did not finish and could not be cancelled ({exc}); "
                    "it may still fill - reconciliation will report any holding it leaves.",
                    level="error",
                    source="executor",
                    data={"order_id": order_id},
                )
        try:
            response = self._merge(response, await self.client.query_order(symbol, order_id, market))
        except ExchangeError as exc:
            log.warning("could not re-read order %s after cancelling: %s", order_id, exc)
        return response

    @staticmethod
    def _merge(response: dict, queried: dict) -> dict:
        """A fresh reading of one order over the answer we hold. When the answer
        stood for several orders (a connector that splits a market order) the
        finished ones are kept as `prior*` and added back in."""
        merged = {**response, **queried}
        prior_qty = float(response.get("priorExecutedQty") or 0)
        if prior_qty:
            merged["executedQty"] = str(float(queried.get("executedQty") or 0) + prior_qty)
            merged["cummulativeQuoteQty"] = str(
                float(queried.get("cummulativeQuoteQty") or 0) + float(response.get("priorQuoteQty") or 0)
            )
            merged["fills"] = list(response.get("priorFills") or []) + list(queried.get("fills") or [])
        return merged

    async def _ensure_spot_funds(self, notional: float) -> float:
        """Redeem from Simple Earn until the spot wallet can pay for `notional`.

        Returns the free quote balance afterwards. A redemption that does not
        arrive is not fatal here: the order is still attempted and Binance
        decides, which keeps this off the critical path when Earn is down.
        """
        quote = settings.quote_asset
        # A market buy fills a little above the decision price, and Binance
        # checks the balance against the fill, not the estimate.
        needed = notional * (1 + ENTRY_FUNDING_BUFFER)
        try:
            free = await self.client.free_balance(quote)
        except ExchangeError as exc:
            log.warning("could not read %s balance before the entry: %s", quote, exc)
            return 0.0
        if free >= needed:
            return free
        if not await self.earn.redeem_quote(needed - free):
            return free
        # Fast redemption credits the spot wallet within a second or two.
        for _ in range(ENTRY_FUNDING_POLLS):
            await asyncio.sleep(ENTRY_FUNDING_POLL_SECONDS)
            try:
                free = await self.client.free_balance(quote)
            except ExchangeError:
                break
            if free >= needed:
                break
        return free

    async def _ensure_spot_holding(
        self, asset: str, qty: float, symbol_info: SymbolInfo
    ) -> float:
        """Free balance of `asset`, after redeeming any part of it Earn swept up.

        Auto-subscribe nibbles the fractional remainder of a freshly bought
        coin into Simple Earn, so the wallet holds slightly less than the
        position. The exit clamps to what it can see, which would strand that
        remainder; redeeming first lets the whole position be sold.
        """
        held = await self.client.free_balance(asset)
        shortfall = qty - held
        if shortfall <= 0:
            return held
        # Only worth a round trip if the missing piece is a sellable lot.
        if shortfall < getattr(symbol_info, "step_size", 0.0):
            return held
        if not await self.earn.redeem_asset(asset, shortfall):
            return held
        for _ in range(ENTRY_FUNDING_POLLS):
            await asyncio.sleep(ENTRY_FUNDING_POLL_SECONDS)
            try:
                held = await self.client.free_balance(asset)
            except ExchangeError:
                break
            if held >= qty:
                break
        return held

    # -- internals ---------------------------------------------------------

    async def _prepare_futures(self, symbol: str) -> None:
        try:
            await self.client.set_margin_type(symbol, settings.futures_margin_mode)
            await self.client.set_leverage(symbol, settings.futures_leverage)
        except ExchangeError as exc:
            log.warning("futures setup for %s failed: %s", symbol, exc)

    async def _place_protective_stop(
        self, session: AsyncSession, position: Position, symbol_info: SymbolInfo
    ) -> None:
        if not self.client.supports_stop_orders:
            # No resting stop orders on this exchange: the engine's tick
            # watchdog sells at market when the price reaches the stop, so the
            # engine has to keep running for the floor to hold.
            position.meta = {**(position.meta or {}), "stop_order_id": None, "stop_mode": "engine"}
            position.stop_missing = False
            return
        side = "SELL" if position.side == "long" else "BUY"
        stop_price = symbol_info.round_price(position.stop_price)
        order_type = "STOP_LOSS_LIMIT" if position.market == "spot" else "STOP_MARKET"
        stop_qty = symbol_info.round_qty(position.qty)
        if position.market == "spot" and position.side == "long":
            # Protect what the wallet holds: a fee taken in coins (or a partial
            # earlier exit) leaves less than the row says, and an oversized
            # stop is rejected outright.
            try:
                held = await self.client.free_balance(symbol_info.base_asset)
                if 0 < held < stop_qty:
                    stop_qty = symbol_info.round_qty(held)
            except ExchangeError as exc:
                log.warning("could not read %s before placing the stop: %s", symbol_info.base_asset, exc)
        # Give the limit a buffer so it actually fills in a fast move.
        buffer = 1 - 0.003 if position.side == "long" else 1 + 0.003
        limit_price = symbol_info.round_price(stop_price * buffer)

        try:
            kwargs = dict(
                symbol=position.symbol,
                side=side,
                order_type=order_type,
                market=position.market,
                quantity=stop_qty,
                stop_price=stop_price,
                client_order_id=f"{STOP_PREFIX}{uuid.uuid4().hex[:19]}",
            )
            if position.market == "spot":
                kwargs["price"] = limit_price
                kwargs["time_in_force"] = "GTC"
            else:
                kwargs["reduce_only"] = True
            response = await self.client.create_order(**kwargs)
        except ExchangeError as exc:
            # Flagged on the row: the engine's heartbeat and the REST sweep
            # keep trying to put the floor back until one of them succeeds.
            position.stop_missing = True
            position.meta = {**(position.meta or {}), "stop_order_id": None}
            await repo.log_event(
                session,
                f"Protective stop for {position.symbol} was not accepted: {exc}. "
                "The engine will manage the stop internally and keep retrying.",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )
            return

        position.meta = {**(position.meta or {}), "stop_order_id": response.get("orderId")}
        position.stop_missing = False
        await repo.record_order(
            session,
            position_id=position.id,
            symbol=position.symbol,
            market=position.market,
            side=side,
            type=order_type,
            intent="stop",
            qty=position.qty,
            price=stop_price,
            status=response.get("status", "NEW"),
            mode=self.mode,
            exchange_order_id=str(response.get("orderId", "")),
            raw=_json_safe(response),
        )

    async def _cancel_open_orders(self, session: AsyncSession, position: Position) -> str:
        """Cancel the resting protective stop before an exit order.

        Returns "none" (no stop on record), "cancelled", "gone" (the exchange
        no longer has it: it filled or was cancelled by hand) or "failed"
        (the request itself failed, the stop may still be resting). The
        caller re-places the stop after a failed exit only when it was
        "cancelled": a "gone" stop most likely sold the coins already.
        """
        stop_id = (position.meta or {}).get("stop_order_id")
        if not stop_id:
            return "none"
        try:
            await self.client.cancel_order(position.symbol, stop_id, position.market)
        except ExchangeError as exc:
            # -2011 "Unknown order sent": already filled or already gone.
            log.info("could not cancel stop %s for %s: %s", stop_id, position.symbol, exc)
            return "gone" if exc.is_unknown_order else "failed"
        await repo.update_order_status(session, stop_id, "CANCELED")
        return "cancelled"

    async def _stop_already_filled(self, session: AsyncSession, position: Position) -> bool:
        """Did the protective stop we could not cancel actually fill?"""
        stop_id = (position.meta or {}).get("stop_order_id")
        if not stop_id:
            return False
        try:
            order = await self.client.query_order(position.symbol, stop_id, position.market)
        except ExchangeError as exc:
            log.warning("could not read stop %s for %s: %s", stop_id, position.symbol, exc)
            return False
        filled = float(order.get("executedQty") or 0) > 0
        if filled:
            await repo.update_order_status(
                session, stop_id, str(order.get("status", "FILLED")).upper(),
                filled_qty=float(order.get("executedQty") or 0),
            )
        return filled

    async def _restore_protective_stop(
        self, session: AsyncSession, position: Position, symbol_info: SymbolInfo
    ) -> None:
        """After a failed exit: the stop is gone and the coins are not - put it back."""
        position.meta = {**(position.meta or {}), "stop_order_id": None}
        position.stop_missing = True
        await self._place_protective_stop(session, position, symbol_info)
        if position.stop_missing:
            log.error("%s is on the exchange with no protective stop", position.symbol)
        else:
            await repo.log_event(
                session,
                f"Protective stop for {position.symbol} re-placed at "
                f"{position.stop_price:.6f} after the exit order failed.",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )

    async def ensure_protective_stop(
        self, session: AsyncSession, position: Position, symbol_info: SymbolInfo
    ) -> bool:
        """Place the exchange stop if the row says it has none. True when placed."""
        if not self.client.supports_stop_orders:
            if position.stop_missing:
                position.stop_missing = False
            return False
        if (position.meta or {}).get("stop_order_id") and not position.stop_missing:
            return False
        await self._place_protective_stop(session, position, symbol_info)
        placed = not position.stop_missing
        if placed:
            await repo.log_event(
                session,
                f"Protective stop for {position.symbol} placed at {position.stop_price:.6f}.",
                level="warn",
                source="executor",
                data={"position_id": position.id},
            )
        return placed

    @staticmethod
    def _note_close_failure(position: Position, error: str) -> None:
        """Record a failed exit on the row without changing its status.

        A terminal "error" state would hide the position from the tick
        watchdog, the user stream and the REST sweep - the three paths that
        can still finish it. `close_failed_at` throttles the engine's retries.
        """
        meta = dict(position.meta or {})
        meta["close_error"] = error[:200]
        meta["close_failed_at"] = utcnow().isoformat()
        meta["close_attempts"] = int(meta.get("close_attempts", 0)) + 1
        position.meta = meta

    async def _bnb_price(self) -> float:
        """BNB in quote terms, cached for a minute; 0.0 when unavailable."""
        price, at = self._bnb_mark
        if price > 0 and time.monotonic() - at < 60:
            return price
        try:
            ticker = await self.client.ticker_price(f"BNB{settings.quote_asset}")
            price = float(ticker["price"])
            self._bnb_mark = (price, time.monotonic())
        except Exception as exc:  # noqa: BLE001 - the fee falls back to the estimate
            log.warning("BNB price unavailable, fee will be estimated: %s", exc)
            price = 0.0
        return price

    async def _bnb_price_for(self, response: dict) -> float:
        """Only hit the ticker when a fill was actually paid in BNB."""
        if not self.client.supports_fee_token:
            return 0.0
        fills = response.get("fills") or []
        if any(f.get("commissionAsset") == "BNB" for f in fills):
            return await self._bnb_price()
        return 0.0

    @staticmethod
    def _parse_fill(
        response: dict, fallback_price: float, base_asset: str = "", bnb_price: float = 0.0
    ) -> tuple[float, float, float]:
        """Return (qty actually received, average price, fee in quote terms).

        A spot BUY pays commission in the *base* asset unless BNB fee payment
        is active and funded, so `executedQty` is gross: the wallet receives
        `executedQty - commission`, and only that much can be sold later.
        """
        filled = float(response.get("executedQty", 0) or 0)
        cummulative = float(
            response.get("cummulativeQuoteQty") or response.get("cumQuote") or 0
        )
        avg = cummulative / filled if filled else float(response.get("avgPrice", 0) or 0)
        if not avg:
            avg = fallback_price

        fee = 0.0
        base_commission = 0.0
        unpriced_notional = 0.0  # BNB-paid fills we could not value
        for fill in response.get("fills", []) or []:
            try:
                commission = float(fill.get("commission", 0))
            except (TypeError, ValueError):
                continue
            asset = fill.get("commissionAsset", "")
            fill_price = float(fill.get("price", avg) or avg)
            if asset == settings.quote_asset:
                fee += commission
            elif asset == "BNB":
                if bnb_price > 0:
                    fee += commission * bnb_price
                else:
                    unpriced_notional += float(fill.get("qty", 0) or 0) * fill_price
            elif asset and asset == base_asset:
                if filled and commission > filled * MAX_BASE_FEE_FRACTION:
                    # Not a plausible coin fee: the exchange gave it in quote
                    # units. Value it as such; never shrink the position by it.
                    log.warning("%s commission %s is not a base-asset fee; valued as quote", asset, commission)
                    fee += commission
                    continue
                # paid out of the coins just bought: we hold LESS than executedQty
                base_commission += commission
                fee += commission * fill_price
            else:
                fee += commission * fill_price
        if unpriced_notional:
            fee += unpriced_notional * settings.taker_fee_pct / 100
        if fee == 0.0 and filled:
            # No commission data at all (ACK/RESULT answer): use the configured rate.
            fee = filled * avg * settings.taker_fee_pct / 100
        return filled - base_commission, avg, fee


def _json_safe(payload: dict) -> dict:
    """Strip anything that will not serialise into a JSON column."""
    try:
        import json

        json.dumps(payload)
        return payload
    except (TypeError, ValueError):
        return {"raw": str(payload)[:2000]}


def build_executor(client: ExchangeClient, registry: SymbolRegistry) -> Executor:
    if settings.trading_mode == "paper":
        return PaperExecutor()
    return LiveExecutor(client, registry)
