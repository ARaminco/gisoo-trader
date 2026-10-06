"""Reconciliation: keep the local database identical to the exchange account.

Two mechanisms, both needed:

  * The user data stream (push) reports fills and balance changes within
    milliseconds. It is the fast path but it only reports *changes*, so a
    disconnect leaves a hole.
  * A REST sweep (pull) reads the whole account state and repairs anything the
    stream missed - including trades made by hand in the exchange app, deposits,
    and positions closed by an exchange-side stop.

Anything that differs is written to `exchange_sync.drift` so the panel can show
exactly what was corrected and when.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo
from app.db.models import Position, utcnow
from app.exchange.base import EXIT_PREFIX, ORDER_PREFIX, STOP_PREFIX, ExchangeClient, ExchangeError
from app.exchange.earn import EarnBook, EarnSnapshot, is_receipt_token
from app.exchange.symbols import SymbolRegistry
from app.execution.executor import Executor
from app.workers import bus

log = logging.getLogger(__name__)

# A resting STOP_LOSS_LIMIT whose limit was gapped through sits unfilled with
# the market below it. Once the mark is this far under the limit the sweep
# hands the exit to the engine (market sell) instead of waiting.
STOP_LIMIT_GAP_TOLERANCE = 0.001
# How often a sweep may fail to read a filled stop before the position is
# booked at the mark instead (the old behaviour).
SETTLE_READ_ATTEMPTS = 3


class Reconciler:
    def __init__(self, client: ExchangeClient, registry: SymbolRegistry):
        self.client = client
        self.registry = registry
        # Its own Earn view rather than the executor's: the two components read
        # on their own schedules, and the two-sweep confirmation below already
        # absorbs the staleness that costs.
        self.earn = EarnBook(client)
        # A balance difference seen on one sweep only; confirmed on the next.
        self._pending_flow: float | None = None
        # True once a sweep of this process has classified the balance. The
        # equity loop treats every reading before that as suspect: a deposit
        # made while the process was down must not become a peak.
        self.synced_once = False

    # ------------------------------------------------------------------
    # push path: user data stream events
    # ------------------------------------------------------------------

    async def handle_stream_event(self, session: AsyncSession, event: dict) -> None:
        kind = event.get("e")
        if kind == "executionReport":
            await self._on_execution_report(session, event)
        elif kind in ("outboundAccountPosition", "balanceUpdate"):
            await self._on_balance_event(session, event)
        elif kind == "ORDER_TRADE_UPDATE":  # futures
            await self._on_execution_report(session, event.get("o", {}), futures=True)
        elif kind == "ACCOUNT_UPDATE":  # futures
            await self._on_balance_event(session, event)

        await repo.update_sync_state(session, last_stream_event=utcnow())

    async def _on_execution_report(
        self, session: AsyncSession, report: dict, futures: bool = False
    ) -> None:
        symbol = report.get("s")
        status = report.get("X")  # order status
        client_id = report.get("c") or report.get("C")
        exchange_id = str(report.get("i", ""))

        if status not in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED", "REJECTED"):
            return

        await repo.log_event(
            session,
            f"Exchange: {symbol} order {status.lower()} "
            f"({report.get('S', '')} {report.get('z', report.get('q', ''))})",
            level="trade" if status == "FILLED" else "info",
            source="exchange",
            data={"order_id": exchange_id, "client_order_id": client_id, "status": status},
        )
        await bus.publish(
            bus.CH_EVENTS,
            {"type": "exchange_order", "symbol": symbol, "status": status},
        )

        if status != "FILLED":
            return

        # A protective stop we placed has fired -> the position is gone on the
        # exchange, so close it locally with the actual fill price.
        position = await repo.open_position_for(session, symbol) if symbol else None
        if position is None:
            return

        stop_id = str((position.meta or {}).get("stop_order_id", ""))
        is_our_stop = stop_id and stop_id == exchange_id
        is_our_entry = client_id and client_id == (position.meta or {}).get(
            "client_order_id"
        )
        if is_our_entry:
            return  # entry fill, already recorded by the executor

        if is_our_stop or (client_id or "").startswith(EXIT_PREFIX):
            fill_price = float(report.get("L") or report.get("p") or 0) or position.entry_price
            fee = float(report.get("n", 0) or 0)
            long = position.side == "long"
            gross = (
                (fill_price - position.entry_price) * position.qty
                if long
                else (position.entry_price - fill_price) * position.qty
            )
            position.exit_price = fill_price
            position.fees_paid = (position.fees_paid or 0) + fee
            position.realized_pnl = gross - position.fees_paid  # net of both legs
            basis = position.entry_price * position.qty
            position.realized_pnl_pct = (
                position.realized_pnl / basis * 100 if basis else 0.0
            )
            risk = abs(position.entry_price - position.initial_stop) * position.qty
            position.r_multiple = position.realized_pnl / risk if risk else 0.0
            position.status = "closed"
            position.closed_at = utcnow()
            position.exit_reason = "exchange_stop" if is_our_stop else "exchange_fill"

            await repo.log_event(
                session,
                f"Exchange closed {position.symbol} @ {fill_price:.6f} "
                f"({position.exit_reason}) PnL {position.realized_pnl:+.3f} {settings.quote_asset}",
                level="trade",
                source="exchange",
                data={"position_id": position.id},
            )

    async def _on_balance_event(self, session: AsyncSession, event: dict) -> None:
        balances: dict[str, float] = {}
        for entry in event.get("B", []) or []:
            asset = entry.get("a")
            free = float(entry.get("f", 0) or 0)
            locked = float(entry.get("l", 0) or 0)
            if asset and free + locked > 0:
                balances[asset] = free + locked
        if balances:
            state = await repo.get_sync_state(session)
            merged = {**(state.balances or {}), **balances}
            await repo.update_sync_state(session, balances=merged)

    # ------------------------------------------------------------------
    # pull path: full REST sweep
    # ------------------------------------------------------------------

    async def full_sync(self, session: AsyncSession) -> dict:
        """Read the whole account and repair every difference found."""
        drift: dict[str, Any] = {}

        if settings.trading_mode == "paper":
            await repo.update_sync_state(
                session,
                connected=True,
                last_rest_sync=utcnow(),
                balances={settings.quote_asset: settings.initial_capital_usdt},
                drift={"note": "paper mode - nothing to reconcile"},
                last_error=None,
            )
            self.synced_once = True
            return {}

        try:
            balances = await self.client.spot_balances()
            open_orders = await self.client.open_orders()
        except ExchangeError as exc:
            await repo.update_sync_state(
                session, connected=False, last_error=str(exc)[:400]
            )
            log.error("account sync failed: %s", exc)
            return {"error": str(exc)}

        # Simple Earn holds coins auto-subscribe swept out of the spot wallet.
        # Both the holdings check and the capital-flow check are wrong without
        # it: a swept balance is not a sold position and not a withdrawal.
        earn = await self.earn.read()

        futures_positions: dict[str, Any] = {}
        if settings.futures_enabled:
            try:
                for p in await self.client.futures_positions():
                    futures_positions[p["symbol"]] = {
                        "amount": float(p.get("positionAmt", 0)),
                        "entry": float(p.get("entryPrice", 0)),
                        "unrealized": float(p.get("unrealizedProfit", 0)),
                    }
            except ExchangeError as exc:
                log.warning("futures sync failed: %s", exc)

        # ---- 1. positions the database thinks are open (or left in "error") ----
        local_positions = await repo.unresolved_positions(session)
        orphaned: list[str] = []
        reopened: list[str] = []
        stopped: list[str] = []
        stop_flags: list[str] = []
        for position in local_positions:
            if position.mode == "paper":
                continue
            # The engine writes the same row from another process while this
            # sweep is doing network I/O. Re-read it here so a decision below
            # is taken on the current state, not on the snapshot this sweep
            # started with.
            await session.refresh(position)
            if position.status not in ("open", "error"):
                continue
            held = await self._exchange_holding(
                position, balances, futures_positions, earn
            )
            if held >= position.qty * 0.5 and position.status == "error":
                # Left behind by a failed exit before failed exits kept the row
                # open. The coins are still here: hand it back to the engine.
                position.status = "open"
                position.exit_reason = None
                reopened.append(position.symbol)
                await repo.log_event(
                    session,
                    f"{position.symbol} is still held on {self.client.title}; the position was "
                    "re-opened for the engine to manage.",
                    level="warn",
                    source="reconcile",
                    data={"position_id": position.id},
                )
                continue
            if held < position.qty * 0.5:
                # The exchange no longer holds it. Our own protective stop is
                # the usual reason: book that from the order's real fill and
                # fee. Anything else (a sale by hand) is closed at the mark.
                if await self._settle_via_stop_order(session, position):
                    if position.status == "closed":
                        stopped.append(position.symbol)
                    continue
                marks = await bus.get_marks()
                price = marks.get(position.symbol, position.entry_price)
                Executor._finalise(position, price, 0.0, "closed_outside_engine")  # noqa: SLF001
                orphaned.append(position.symbol)
                await repo.log_event(
                    session,
                    f"{position.symbol} is no longer held on {self.client.title} - the position "
                    f"was closed outside the engine. Marked closed at {price:.6f}.",
                    level="warn",
                    source="reconcile",
                    data={"position_id": position.id},
                )
                continue
            if position.status == "open":
                # The coins are still here, but our stop may have filled
                # anyway (the balance can belong to the user rather than to
                # this position), so the check below also settles a stop it
                # finds FILLED.
                await self._check_protective_stop(session, position, open_orders, stop_flags)
                if position.status == "closed":
                    stopped.append(position.symbol)
        if stopped:
            drift["exchange_stop"] = stopped
        if orphaned:
            drift["closed_outside_engine"] = orphaned
        if reopened:
            drift["reopened"] = reopened
        if stop_flags:
            drift["stop_flags"] = stop_flags

        # ---- 2. holdings on the exchange the database does not know about ----
        tracked = {p.symbol for p in local_positions}
        untracked: list[dict] = []
        for asset, amount in balances.items():
            if asset in (settings.quote_asset, "BNB") or amount <= 0:
                continue
            symbol = f"{asset}{settings.quote_asset}"
            if symbol in tracked or symbol not in settings.symbols:
                continue
            try:
                info = await self.registry.get(symbol)
                marks = await bus.get_marks()
                price = marks.get(symbol) or float(
                    (await self.client.ticker_price(symbol))["price"]
                )
                if amount * price >= info.min_notional:
                    untracked.append(
                        {"symbol": symbol, "qty": amount, "value": round(amount * price, 2)}
                    )
            except (KeyError, ExchangeError, ValueError):
                continue
        if untracked:
            drift["untracked_holdings"] = untracked
            await repo.log_event(
                session,
                f"Balances held on {self.client.title} that the engine is not managing: "
                + ", ".join(f"{u['symbol']} (~{u['value']} {settings.quote_asset})" for u in untracked)
                + ". The engine will not sell these; adopt or sell them manually.",
                level="info",
                source="reconcile",
                data={"untracked": untracked},
            )

        # ---- 3. stale protective stops for positions that are already closed ----
        stale_orders = []
        active_stop_ids = {
            str((p.meta or {}).get("stop_order_id", "")) for p in local_positions
        }
        untracked_symbols = {u["symbol"] for u in untracked}
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        for order in open_orders:
            oid = str(order.get("orderId", ""))
            cid = str(order.get("clientOrderId", ""))
            # A young stop may belong to an entry the engine has filled but not
            # yet committed - cancelling it would strip the only protection.
            # The same goes for coins the engine does not manage: the user
            # decides about those, and their stop stays until they do.
            age_ms = now_ms - int(order.get("time") or now_ms)
            if order.get("symbol") in untracked_symbols:
                continue
            if cid.startswith(ORDER_PREFIX) and oid not in active_stop_ids and age_ms > 10 * 60 * 1000:
                try:
                    await self.client.cancel_order(order["symbol"], oid)
                    stale_orders.append(f"{order['symbol']}:{oid}")
                except ExchangeError as exc:
                    log.info("could not cancel stale order %s: %s", oid, exc)
        if stale_orders:
            drift["cancelled_stale_orders"] = stale_orders

        # ---- 4. detect capital added or removed on the exchange ----
        await self._detect_capital_flow(session, balances, earn)
        self.synced_once = True

        await repo.update_sync_state(
            session,
            connected=True,
            last_rest_sync=utcnow(),
            # The panel and the API value the account from this map, so it has
            # to be the whole account: spot plus Simple Earn, with the `LD*`
            # receipt tokens dropped so the Earn money is counted once.
            balances=self._account_balances(balances, earn),
            exchange_open_orders=len(open_orders),
            exchange_positions=futures_positions,
            drift=drift,
            server_time_offset_ms=self.client.time_offset_ms,
            last_error=None,
        )
        if drift:
            log.info("reconciliation corrected: %s", drift)
        return drift

    # ------------------------------------------------------------------
    async def _settle_via_stop_order(
        self, session: AsyncSession, position: Position, order: dict | None = None
    ) -> bool:
        """Close a row whose protective stop filled on the exchange, at the real fill.

        The push path that used to price this from the executionReport is
        dead (Binance retired the listenKey stream), so the sweep asks the
        order itself: fill price from cummulativeQuoteQty / executedQty, the
        commission from the trades behind it. `order` is the already-read
        order when the caller has one. Returns True when the row is settled -
        or when a read failed and the next sweep should try again; False when
        the stop is not on record or did not fill, so the caller books the
        close at the mark, as before.
        """
        stop_id = (position.meta or {}).get("stop_order_id")
        if not stop_id:
            return False
        if order is None:
            try:
                order = await self.client.query_order(position.symbol, stop_id, position.market)
            except ExchangeError as exc:
                # A transient read failure must not book the close at the mark:
                # the next sweep (30 s) tries again. After SETTLE_READ_ATTEMPTS
                # the caller's mark-price fallback takes over, so a stale id
                # cannot keep the row open forever.
                attempts = int((position.meta or {}).get("settle_attempts", 0)) + 1
                position.meta = {**(position.meta or {}), "settle_attempts": attempts}
                log.warning(
                    "could not read stop %s for %s (attempt %s/%s): %s",
                    stop_id, position.symbol, attempts, SETTLE_READ_ATTEMPTS, exc,
                )
                return attempts < SETTLE_READ_ATTEMPTS  # True = "not settled yet, wait"
        status = str(order.get("status", "")).upper()
        if status in ("NEW", "PARTIALLY_FILLED"):
            # The stop still rests and locks the coins. A wallet reading that
            # leaves locked coins out must not orphan the row (or later get
            # this stop cancelled as stale): the coins are still ours.
            return True
        if status != "FILLED":
            return False
        executed = float(order.get("executedQty") or 0)
        quote = float(order.get("cummulativeQuoteQty") or order.get("cumQuote") or 0)
        if executed <= 0 or quote <= 0:
            return False
        fill_price = quote / executed
        fee = await self._order_fee(position, stop_id, executed, fill_price)

        Executor._finalise(position, fill_price, fee, "exchange_stop")  # noqa: SLF001
        filled_at = order.get("updateTime") or order.get("time")
        if filled_at:
            position.closed_at = dt.datetime.fromtimestamp(
                int(filled_at) / 1000, tz=dt.timezone.utc
            )
        await repo.update_order_status(
            session, stop_id, "FILLED", filled_qty=executed, avg_price=fill_price, fee=fee
        )
        await repo.record_order(
            session,
            position_id=position.id,
            symbol=position.symbol,
            market=position.market,
            side="SELL" if position.side == "long" else "BUY",
            type=str(order.get("type") or "STOP_LOSS_LIMIT"),
            intent="exit",
            qty=executed,
            price=float(order.get("stopPrice") or fill_price),
            filled_qty=executed,
            avg_price=fill_price,
            fee=fee,
            status="FILLED",
            mode=position.mode,
            exchange_order_id=str(stop_id),
            raw={k: order.get(k) for k in ("status", "type", "price", "stopPrice",
                                           "executedQty", "cummulativeQuoteQty", "updateTime")},
        )
        await repo.log_event(
            session,
            f"Exchange closed {position.symbol} @ {fill_price:.6f} "
            f"(exchange_stop) PnL {position.realized_pnl:+.3f} {settings.quote_asset} "
            f"({position.realized_pnl_pct:+.2f}%, {position.r_multiple:+.2f}R)",
            level="trade",
            source="exchange",
            data={"position_id": position.id, "reason": "exchange_stop", "order_id": str(stop_id)},
        )
        await bus.publish(
            bus.CH_EVENTS,
            {
                "type": "position_closed",
                "symbol": position.symbol,
                "pnl": position.realized_pnl,
                "pnl_pct": position.realized_pnl_pct,
                "reason": "exchange_stop",
            },
        )
        log.info(
            "%s closed by the exchange stop @ %.6f pnl=%+.3f (%+.2f%%)",
            position.symbol, fill_price, position.realized_pnl, position.realized_pnl_pct,
        )
        return True

    async def _order_fee(
        self, position: Position, order_id: str | int, executed: float, fill_price: float
    ) -> float:
        """Commission of one order in quote terms; the configured rate when unreadable."""
        try:
            trades = await self.client.my_trades(position.symbol, order_id, position.market)
        except ExchangeError as exc:
            log.warning("could not read fills of %s: %s", order_id, exc)
            trades = []
        base = position.symbol.replace(settings.quote_asset, "")
        fee = 0.0
        bnb_price: float | None = None
        for trade in trades or []:
            try:
                commission = float(trade.get("commission", 0) or 0)
            except (TypeError, ValueError):
                continue
            asset = trade.get("commissionAsset", "")
            if asset == settings.quote_asset:
                fee += commission
            elif asset == "BNB":
                if bnb_price is None:
                    try:
                        ticker = await self.client.ticker_price(f"BNB{settings.quote_asset}")
                        bnb_price = float(ticker["price"])
                    except Exception:  # noqa: BLE001 - fall back to the rate
                        bnb_price = 0.0
                fee += (
                    commission * bnb_price if bnb_price
                    else float(trade.get("qty", 0) or 0) * float(trade.get("price", fill_price) or fill_price)
                    * settings.taker_fee_pct / 100
                )
            elif asset == base:
                fee += commission * float(trade.get("price", fill_price) or fill_price)
            else:
                fee += commission
        if fee <= 0:
            fee = executed * fill_price * settings.taker_fee_pct / 100
        return fee

    async def _check_protective_stop(
        self,
        session: AsyncSession,
        position: Position,
        open_orders: list,
        flags: list[str],
    ) -> None:
        """An open position must always have a live stop on the exchange.

        Ways it can lack one, all handed to the engine through the row:
          * no stop on record -> adopt one this engine placed for the same
            symbol if it is resting (an entry whose commit was lost), else
            flag `stop_missing` and the engine places one on its heartbeat;
          * the recorded stop is no longer open (cancelled by hand, expired,
            rejected) -> the same flag;
          * the stop triggered but its limit was gapped through, so it rests
            unfilled with the market below it -> `pending_exit_reason`, the
            engine cancels it and sells at market.
        """
        if not self.client.supports_stop_orders:
            # This exchange has no resting stop orders; the engine's tick
            # watchdog enforces the stop, so there is nothing to check here.
            return
        symbol_orders = [o for o in open_orders if o.get("symbol") == position.symbol]
        stop_id = str((position.meta or {}).get("stop_order_id") or "")
        # Order ids are per symbol, so both sides of this comparison must be.
        resting = next((o for o in symbol_orders if str(o.get("orderId")) == stop_id), None) if stop_id else None

        if not stop_id:
            orphan = next(
                (
                    o for o in symbol_orders
                    if str(o.get("clientOrderId", "")).startswith(STOP_PREFIX)
                    and str(o.get("type", "")).upper() in ("STOP_LOSS_LIMIT", "STOP_LOSS", "STOP_MARKET")
                    and abs(float(o.get("origQty") or 0) - position.qty) <= max(1e-9, position.qty * 0.02)
                ),
                None,
            )
            if orphan is not None:
                # The stop is on the exchange, only the link was lost. Adopt
                # it: placing a second one would be rejected (the coins are
                # locked by this one) and cancelling it would strip the floor.
                position.meta = {**(position.meta or {}), "stop_order_id": orphan.get("orderId")}
                position.stop_missing = False
                flags.append(f"{position.symbol}:stop_adopted")
                await repo.log_event(
                    session,
                    f"Adopted the protective stop {orphan.get('orderId')} already resting on "
                    f"{self.client.title} for {position.symbol}.",
                    level="warn",
                    source="reconcile",
                    data={"position_id": position.id},
                )
                return
            if not position.stop_missing:
                position.stop_missing = True
                flags.append(f"{position.symbol}:no_stop")
                await repo.log_event(
                    session,
                    f"{position.symbol} has no protective stop on {self.client.title}; the engine will place one.",
                    level="warn",
                    source="reconcile",
                    data={"position_id": position.id},
                )
            return

        if resting is None:
            try:
                order = await self.client.query_order(position.symbol, stop_id, position.market)
                status = str(order.get("status", "")).upper()
            except ExchangeError as exc:
                log.warning("could not read stop %s for %s: %s", stop_id, position.symbol, exc)
                return
            if status == "FILLED":
                # The stop sold the position; the wallet balance that kept it
                # out of the branch above belongs to the user, not to us.
                if await self._settle_via_stop_order(session, position, order):
                    flags.append(f"{position.symbol}:stop_filled")
                return
            if status in ("CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"):
                position.stop_missing = True
                flags.append(f"{position.symbol}:stop_{status.lower()}")
                await repo.update_order_status(session, stop_id, status)
                await repo.log_event(
                    session,
                    f"Protective stop {stop_id} for {position.symbol} is {status.lower()} on "
                    f"{self.client.title} while the coins are still held; the engine will re-place it.",
                    level="warn",
                    source="reconcile",
                    data={"position_id": position.id},
                )
            return

        if position.stop_missing:
            # It is there after all (the engine placed it between sweeps).
            position.stop_missing = False

        # The stop is resting. If the market is already under its limit the
        # trigger fired and the limit did not fill: sell at market instead.
        if position.side != "long" or position.pending_exit_reason:
            return
        try:
            limit_price = float(resting.get("price") or 0)
        except (TypeError, ValueError):
            return
        marks = await bus.get_marks()
        mark = marks.get(position.symbol)
        if not limit_price or not mark:
            return
        if mark < limit_price * (1 - STOP_LIMIT_GAP_TOLERANCE):
            position.pending_exit_reason = "stop_loss"
            position.pending_exit_price = mark
            position.pending_exit_at = utcnow()
            flags.append(f"{position.symbol}:stop_gapped")
            await repo.log_event(
                session,
                f"{position.symbol}: stop {stop_id} triggered but its limit {limit_price:.6f} "
                f"did not fill (mark {mark:.6f}); the engine will sell at market.",
                level="error",
                source="reconcile",
                data={"position_id": position.id},
            )

    @staticmethod
    def _account_balances(
        balances: dict[str, float], earn: EarnSnapshot | None
    ) -> dict[str, float]:
        """Spot wallet and Simple Earn as one holdings map.

        `LD<asset>` rows are Binance's receipt tokens for the Earn positions
        they mirror; they have no market and must not be added on top of the
        positions themselves.
        """
        merged = {
            asset: amount
            for asset, amount in balances.items()
            if not is_receipt_token(asset)
        }
        if earn is not None:
            for asset, amount in earn.holdings.items():
                if amount > 0:
                    merged[asset] = merged.get(asset, 0.0) + amount
        return merged

    @staticmethod
    async def _exchange_holding(
        position: Position,
        balances: dict[str, float],
        futures: dict[str, Any],
        earn: EarnSnapshot | None = None,
    ) -> float:
        if position.market == "futures":
            entry = futures.get(position.symbol)
            return abs(entry["amount"]) if entry else 0.0
        base = position.symbol.replace(settings.quote_asset, "")
        # Auto-subscribe nibbles the fractional remainder of a position into
        # Simple Earn. Those coins are still held - counting only the spot
        # wallet makes a position look partly sold and can orphan the row.
        held = balances.get(base, 0.0)
        if earn is not None:
            held += earn.amount(base)
        return held

    async def _detect_capital_flow(
        self,
        session: AsyncSession,
        balances: dict[str, float],
        earn: EarnSnapshot | None = None,
    ) -> None:
        """Notice deposits and withdrawals so performance stays honest.

        Compares the actual quote balance against what the engine expects from
        its own trade history. A difference larger than the tolerance is
        recorded as a capital flow rather than being mistaken for PnL.

        The "actual" balance is the spot wallet *plus* flexible Simple Earn.
        Binance's auto-subscribe moves the quote asset between the two with no
        user involvement, so comparing the spot side alone reads every sweep
        as a withdrawal: phantom withdrawals shrink the capital base and a
        small trading loss reads as a large drawdown. An Earn book that
        could not be read is unknown, not empty, and stands the whole check
        down rather than inventing a flow out of the missing money.

        The difference is persisted on the sync state the moment it is seen
        (`balance_unexplained`), before the two-sweep confirmation: until the
        flow is in the ledger every equity reading contains money the ledger
        does not, and the equity loop and the risk manager must know not to
        turn such a reading into a peak. `balance_unexplained_since` is the
        last sweep at which the balance was explained - the flow happened
        after it - and becomes the recorded flow's `occurred_at`, so snapshots
        before it are restated with the flow and those inside the window are
        marked suspect by `repo.add_capital_flow`.
        """
        if earn is not None and not earn.trustworthy:
            # Never classify a flow from an incomplete picture of the money.
            self._pending_flow = None
            return
        quote_balance = balances.get(settings.quote_asset, 0.0)
        if earn is not None:
            quote_balance += earn.quote
        expected_free, tolerance = await repo.expected_free_balance(session)
        difference = quote_balance - expected_free
        state = await repo.get_sync_state(session)

        if abs(difference) < tolerance:
            self._pending_flow = None
            if state.balance_unexplained or state.balance_unexplained_since is not None:
                # Explained without this sweep recording anything: the user
                # entered the flow by hand, or the entry a sweep caught
                # mid-commit has landed in the database.
                await repo.settle_unexplained_balance(session)
            return

        since = state.balance_unexplained_since or state.last_rest_sync or utcnow()
        await repo.update_sync_state(
            session, balance_unexplained=difference, balance_unexplained_since=since
        )

        # An entry filled on Binance but not yet committed looks exactly like a
        # withdrawal for one sweep. Only record a difference seen twice in a row.
        if self._pending_flow is None or abs(self._pending_flow - difference) > tolerance / 2:
            self._pending_flow = difference
            log.info("possible capital flow of %+.2f; waiting for the next sweep", difference)
            return
        self._pending_flow = None

        kind = "deposit" if difference > 0 else "withdrawal"
        external_id = f"sync-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M}"
        if await repo.capital_flow_exists(session, external_id):
            return

        await repo.add_capital_flow(
            session,
            occurred_at=since,
            kind=kind,
            amount=abs(difference),
            asset=settings.quote_asset,
            source="exchange_sync",
            note=(
                f"Detected on reconciliation: balance {quote_balance:.2f} vs "
                f"expected {expected_free:.2f}"
            ),
            equity_before=expected_free,
            equity_after=quote_balance,
            external_id=external_id,
        )
        base = await repo.capital_base(session)
        await repo.log_event(
            session,
            f"Capital {kind} of {abs(difference):.2f} {settings.quote_asset} detected "
            f"on {self.client.title} and recorded. Performance percentages now use a base of "
            f"{base:.2f}.",
            level="info",
            source="reconcile",
            data={"kind": kind, "amount": abs(difference)},
        )
        log.info("capital %s of %.2f recorded", kind, abs(difference))
