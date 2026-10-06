"""Periodic jobs.

  * equity snapshot        every minute   - the equity curve and drawdown
  * exchange reconcile     every 2 min    - REST sweep against the exchange
  * user data stream       continuous     - push updates from the account
  * LLM market review      configurable   - the risk stance
  * ML retrain             configurable   - walk-forward refit per symbol
                                            (ensemble core only: the 4h
                                            families do not use the model)
  * housekeeping           hourly         - prune candles and old events
  * public IP watch        every 5 min    - exchange keys are IP-restricted
  * Telegram chat discovery every minute  - until the user has written to the bot
  * investment committee  twice a day    - COMMITTEE_HOURS_UTC (and on the
                                            `hold_meeting` bus command)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal

from app.committee import runner as committee
from app.config import settings
from app.db import repo
from app.db.session import init_db, session_scope
from app.exchange.registry import make_client
from app.exchange.symbols import SymbolRegistry
from app.exchange.user_stream import UserDataStream
from app.execution.executor import build_executor
from app.features.build import build_features, latest_snapshot
from app.llm.advisor import advisor
from app.ml import model as ml
from app.notify import ipwatch, telegram
from app.notify import service as notify
from app.util.logging_setup import setup_logging
from app.util.runtime_settings import refresh_runtime_settings
from app.workers import bus
from app.workers.reconcile import Reconciler

log = setup_logging("scheduler")


class Scheduler:
    def __init__(self) -> None:
        self.client = make_client()
        self.registry = SymbolRegistry(self.client)
        self.reconciler = Reconciler(self.client, self.registry)
        self.executor = None
        self.symbols: list[str] = []
        self.stream: UserDataStream | None = None
        self._stop = asyncio.Event()
        # Set by the reconcile loop after its first successful sweep; the
        # equity loop waits for it (bounded) before its first snapshot.
        self._synced = asyncio.Event()
        # shutdown() is reached twice (signal handler task + main's finally).
        self._shutdown_lock = asyncio.Lock()
        self._shut_down = False
        # Meetings started by a bus command run as their own tasks; keep a
        # reference so a bare create_task() is not garbage collected.
        self._background: set[asyncio.Task] = set()

    async def start(self) -> None:
        await init_db()
        await self._refresh_settings()
        await self._heal_peaks()
        await self.client.sync_time()

        ok, _ = await self.registry.validate_universe(settings.active_symbols)
        self.symbols = ok
        self.executor = build_executor(self.client, self.registry)

        log.info("scheduler online (%s symbols)", len(self.symbols))
        async with session_scope() as session:
            await repo.log_event(
                session, f"Scheduler online ({len(self.symbols)} symbols)",
                source="scheduler", data={"symbols": self.symbols}, notify=False,
            )
        notify.emit(
            "system", "info", "زمان‌بند روشن شد",
            f"همگام‌سازی با {self.client.title_fa}، منحنی سرمایه، بازبینی هوش مصنوعی و پایش IP از این لحظه فعال است.",
        )

        tasks = [
            self._equity_loop(),
            self._reconcile_loop(),
            self._llm_loop(),
            self._ml_loop(),
            self._housekeeping_loop(),
            self._ipwatch_loop(),
            self._telegram_discovery_loop(),
            self._committee_loop(),
            self._command_loop(),
            self._heartbeat(),
        ]
        if (
            settings.trading_mode != "paper"
            and self.client.has_credentials
            and self.client.supports_user_stream
        ):
            tasks.append(self._user_stream_loop())

        await asyncio.gather(*tasks)

    async def _refresh_settings(self) -> None:
        """Pull panel overrides onto this process (see util.runtime_settings)."""
        async with session_scope() as session:
            changed = await refresh_runtime_settings(session)
        if changed:
            log.info("runtime settings updated from panel: %s", ", ".join(changed))

    async def _sleep(self, seconds: float) -> None:
        """Sleep that returns early on shutdown, so SIGTERM does not wait a full period."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _heal_peaks(self) -> None:
        """Rebuild a closed-equity peak that trade history cannot support.

        The stored peak is only ever raised, so one reading taken with an
        unclassified deposit on the exchange poisons it until someone edits
        the settings row by hand - see repo.heal_closed_peak.
        """
        async with session_scope() as session:
            corrected = await repo.heal_closed_peak(session)
            if corrected is None:
                return
            before, after = corrected
            log.warning(
                "closed-equity peak %.2f is not supported by trade history; reset to %.2f",
                before, after,
            )
            await repo.log_event(
                session,
                f"Closed-equity peak corrected from {before:.2f} to {after:.2f} "
                f"{settings.quote_asset} at start: the stored value was not supported "
                "by the closed-trade history (a capital flow had been read as profit).",
                level="warn",
                source="risk",
                data={"before": before, "after": after},
            )

    # ------------------------------------------------------------------
    async def _equity_loop(self) -> None:
        # The first snapshot after a restart waits (bounded) for the first
        # REST sweep: a deposit made while the process was down would
        # otherwise be read as a peak before the reconciler has seen it.
        try:
            await asyncio.wait_for(self._synced.wait(), timeout=45)
        except asyncio.TimeoutError:
            log.warning("no exchange sweep yet; equity readings stay suspect until one succeeds")

        suspect_logged = False
        while not self._stop.is_set():
            try:
                marks = await bus.get_marks()
                async with session_scope() as session:
                    equity, free = await self.executor.account_equity(session, marks)
                    quote_balance = self.executor.last_quote_balance()
                    open_positions = await repo.open_positions(session)

                    unrealized = 0.0
                    for position in open_positions:
                        mark = marks.get(position.symbol, position.entry_price)
                        direction = 1 if position.side == "long" else -1
                        unrealized += (
                            (mark - position.entry_price) * position.qty * direction
                        )

                    base = await repo.capital_base(session) or settings.initial_capital_usdt
                    closed_equity = equity - unrealized

                    # Is this reading attributable? Money that landed on (or
                    # left) the exchange before the reconciler classified it
                    # is in `equity` but not in the ledger; such a reading is
                    # written to the curve but may not become a peak. Two
                    # signals, either suffices: the reconciler's persisted
                    # verdict or this loop's own check of the very balance
                    # the equity was built from - plus "no sweep of this
                    # process has classified anything yet".
                    unexplained = await repo.unexplained_balance(session, quote_balance)
                    suspect = (
                        unexplained != 0.0
                        or not self.reconciler.synced_once
                        # An equity built without a trusted Simple Earn reading
                        # is missing an unknown amount of the user's money.
                        or not self.executor.balances_complete()
                    )

                    # Closed-equity peak for the 4h core's kill switch, kept
                    # current between evaluations (see risk.manager). Neither
                    # peak moves on a suspect reading.
                    peak, closed_peak = await repo.peaks_for_reading(
                        session,
                        equity=equity,
                        closed_equity=closed_equity,
                        suspect=suspect,
                    )
                    # While money is in flight the reading is measured with
                    # the unclassified amount removed - equity in the same
                    # terms as the ledger - so the figures do not swing by
                    # the size of a deposit for the minutes it takes to
                    # classify it.
                    known_equity = equity - unexplained
                    known_closed = closed_equity - unexplained
                    drawdown = (
                        (peak - known_equity) / peak * 100 if peak > 0 else 0.0
                    )
                    closed_drawdown = (
                        (closed_peak - known_closed) / closed_peak * 100 if closed_peak > 0 else 0.0
                    )
                    await repo.add_equity_snapshot(
                        session,
                        mode=settings.trading_mode,
                        equity=equity,
                        free_balance=free,
                        unrealized_pnl=unrealized,
                        open_positions=len(open_positions),
                        peak_equity=peak,
                        drawdown_pct=max(0.0, drawdown),
                        suspect=suspect,
                    )
                if suspect and not suspect_logged:
                    log.warning(
                        "equity reading is suspect (unexplained balance %+.2f %s, first sweep "
                        "done: %s): peaks frozen until the reconciler classifies it",
                        unexplained, settings.quote_asset, self.reconciler.synced_once,
                    )
                suspect_logged = suspect
                await bus.publish(
                    bus.CH_EVENTS,
                    {
                        "type": "equity", "equity": equity, "drawdown_pct": max(0.0, drawdown),
                        "closed_equity": closed_equity,
                        "closed_drawdown_pct": max(0.0, closed_drawdown),
                        "suspect": suspect,
                        "balance_unexplained": unexplained,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("equity snapshot failed: %s", exc)
            await self._sleep(60)

    # ------------------------------------------------------------------
    async def _reconcile_loop(self) -> None:
        await self._sleep(20)  # let the other workers settle first
        while not self._stop.is_set():
            interval = 120
            try:
                async with session_scope() as session:
                    await self.reconciler.full_sync(session)
                    # Binance retired the push stream (listenKey 410), so the
                    # REST sweep is the only way to notice an exchange-side
                    # stop fill. Sweep every 30 s while money is at risk.
                    if await repo.open_positions(session):
                        interval = 30
                if self.reconciler.synced_once:
                    self._synced.set()
            except Exception as exc:  # noqa: BLE001
                log.exception("reconciliation failed: %s", exc)
            await self._sleep(interval)

    async def _user_stream_loop(self) -> None:
        async def handler(event: dict) -> None:
            async with session_scope() as session:
                await self.reconciler.handle_stream_event(session, event)

        async def on_state(connected: bool, error: str | None) -> None:
            async with session_scope() as session:
                await repo.update_sync_state(
                    session, stream_connected=connected, last_error=error
                )

        self.stream = UserDataStream(self.client, handler, "spot", on_state)
        await self.stream.run()

    # ------------------------------------------------------------------
    async def _llm_loop(self) -> None:
        await self._sleep(90)
        while not self._stop.is_set():
            interval = max(5, settings.llm_review_minutes) * 60
            try:
                if advisor.enabled:
                    await self._run_market_review()
            except Exception as exc:  # noqa: BLE001
                log.warning("market review failed: %s", exc)
            await self._sleep(interval)

    async def _run_market_review(self) -> None:
        snapshots: dict[str, dict] = {}
        async with session_scope() as session:
            for symbol in self.symbols:
                candles = await repo.load_candles(session, symbol, settings.decision_timeframe, 300)
                if len(candles) < 210:
                    continue
                try:
                    snapshots[symbol] = latest_snapshot(build_features(candles))
                except ValueError:
                    continue

            if not snapshots:
                return

            marks = await bus.get_marks()
            equity, _ = await self.executor.account_equity(session, marks)

            open_payload = []
            for position in await repo.open_positions(session):
                mark = marks.get(position.symbol, position.entry_price)
                direction = 1 if position.side == "long" else -1
                pct = (
                    (mark - position.entry_price)
                    / position.entry_price
                    * 100
                    * direction
                )
                open_payload.append(
                    {
                        "symbol": position.symbol,
                        "side": position.side,
                        "entry_price": round(position.entry_price, 6),
                        "unrealized_pct": round(pct, 2),
                    }
                )

            week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
            recent = await repo.closed_positions_since(session, week_ago)
            wins = [p for p in recent if p.realized_pnl > 0]
            latest = await repo.latest_equity(session)
            performance = {
                "trades": len(recent),
                "win_rate": len(wins) / len(recent) if recent else 0.0,
                "net_pnl": sum(p.realized_pnl for p in recent),
                "drawdown_pct": latest.drawdown_pct if latest else 0.0,
            }

        review, usage = await advisor.review_market(
            snapshots, equity, open_payload, performance
        )
        if review is None:
            return

        async with session_scope() as session:
            # The user is told when the stance *changes*; the same stance
            # every LLM_REVIEW_MINUTES stays in the event log only.
            previous = await repo.latest_llm_review(session, "market")
            stance_changed = previous is None or previous.stance != review.stance
            await repo.record_llm_review(
                session,
                scope="market",
                stance=review.stance,
                risk_multiplier=review.risk_multiplier,
                confidence=review.confidence,
                reasoning=review.reasoning,
                payload={
                    "symbols": [s.model_dump() for s in review.symbols],
                    "watch_for": review.watch_for,
                },
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
            await repo.log_event(
                session,
                f"Market stance: {review.stance} (risk x{review.risk_multiplier:.2f}) - "
                f"{review.reasoning[:220]}",
                source="llm",
                data={"stance": review.stance, "confidence": review.confidence},
                notify=stance_changed,
            )
        await bus.publish(
            bus.CH_EVENTS,
            {
                "type": "market_review",
                "stance": review.stance,
                "risk_multiplier": review.risk_multiplier,
            },
        )
        log.info(
            "market review: %s x%.2f (%s)",
            review.stance, review.risk_multiplier, review.reasoning[:120],
        )

    # ------------------------------------------------------------------
    async def _ml_loop(self) -> None:
        if settings.core_is_portfolio4h:
            # The momentum and turtle families take no ML input (the study
            # found the gate adds nothing); training models nobody reads
            # would only burn CPU every ML_RETRAIN_HOURS.
            log.info("ML retraining is off for the %s core", settings.strategy_core)
            return
        await self._sleep(120)
        while not self._stop.is_set():
            if settings.ml_enabled:
                try:
                    await self._retrain_all()
                except Exception as exc:  # noqa: BLE001
                    log.exception("retraining failed: %s", exc)
            await self._sleep(max(1, settings.ml_retrain_hours) * 3600)

    async def _retrain_all(self) -> None:
        for symbol in self.symbols:
            async with session_scope() as session:
                candles = await repo.load_candles(
                    session, symbol, settings.timeframe, settings.candle_history
                )
            if len(candles) < 600:
                log.info("%s: %s candles, not enough to train yet", symbol, len(candles))
                continue

            # Training is CPU-bound; keep the event loop responsive.
            result = await asyncio.to_thread(ml.train, symbol, settings.timeframe, candles)
            if result is None:
                continue

            async with session_scope() as session:
                await repo.register_model(
                    session,
                    symbol=symbol,
                    timeframe=settings.timeframe,
                    path=result.path,
                    rows=result.rows,
                    features=result.features,
                    metrics=result.metrics,
                    active=True,
                )
                await repo.log_event(
                    session,
                    f"Model retrained for {symbol}: AUC {result.metrics['auc']:.3f} "
                    f"on {result.rows} rows",
                    source="ml",
                    data=result.metrics,
                )
            await bus.publish(bus.CH_COMMANDS, {"action": "reload_models"})
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    async def _housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(3600)
            if self._stop.is_set():
                break
            try:
                async with session_scope() as session:
                    for symbol in settings.data_symbols:
                        for timeframe in settings.data_timeframes:
                            await repo.prune_candles(
                                session, symbol, timeframe, settings.candle_history * 2
                            )
            except Exception as exc:  # noqa: BLE001
                log.warning("housekeeping failed: %s", exc)

    # ------------------------------------------------------------------
    async def _ipwatch_loop(self) -> None:
        await self._sleep(10)  # let the first sync_time / reconcile go first
        while not self._stop.is_set():
            try:
                await ipwatch.check_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("public IP check failed: %s", exc)
            await self._sleep(ipwatch.INTERVAL_SECONDS)

    async def _telegram_discovery_loop(self) -> None:
        """Poll getUpdates until the user writes to the bot, then stop.

        Only this process polls: Telegram hands each update to one consumer.
        """
        if not telegram.configured() or settings.telegram_chat_id:
            return
        while not self._stop.is_set():
            try:
                if await telegram.chat_id():
                    return  # discovered earlier (or by a previous run)
                if await telegram.discover_chat_id():
                    return
            except Exception as exc:  # noqa: BLE001 - the token may be wrong, the net down
                log.warning("telegram chat discovery failed: %s", exc)
            await self._sleep(60)

    # ------------------------------------------------------------------
    async def _committee_loop(self) -> None:
        """Hold the investment committee at COMMITTEE_HOURS_UTC (+5 min).

        Restart-tolerant: `committee_last_held` is persisted, and a meeting
        whose slot passed while the process was down is held within two
        minutes of start. The lock in the runner keeps a manual trigger from
        doubling a scheduled meeting.
        """
        if not settings.committee_enabled:
            log.info("investment committee is off (COMMITTEE_ENABLED=false)")
            return
        await self._sleep(90)  # first sweep and equity snapshot first
        while not self._stop.is_set():
            wait = 600.0
            try:
                now = dt.datetime.now(dt.timezone.utc)
                async with session_scope() as session:
                    last = await committee.last_held(session)
                if committee.is_due(now, last):
                    await committee.hold_meeting("scheduled", client=self.client)
                    now = dt.datetime.now(dt.timezone.utc)
                wait = (committee.next_meeting_at(now) - now).total_seconds()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.exception("committee meeting failed: %s", exc)
            # Wake at least every 30 min so a missed slot is never later than that.
            await self._sleep(max(30.0, min(wait, 1800.0)))

    async def _command_loop(self) -> None:
        """Panel commands addressed to this process (the trader has its own)."""
        async for _channel, payload in bus.subscribe(bus.CH_COMMANDS):
            if self._stop.is_set():
                break
            action = payload.get("action")
            if action == "hold_meeting":
                if not settings.committee_enabled:
                    log.info("hold_meeting ignored: committee is off")
                    continue
                task = asyncio.create_task(self._manual_meeting())
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    async def _manual_meeting(self) -> None:
        try:
            await committee.hold_meeting("manual", client=self.client)
        except Exception as exc:  # noqa: BLE001
            log.exception("manual committee meeting failed: %s", exc)

    # ------------------------------------------------------------------
    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                async with session_scope() as session:
                    changed = await refresh_runtime_settings(session)
                    state = await repo.get_sync_state(session)
                    payload = {
                        "connected": state.connected,
                        "stream_connected": state.stream_connected,
                        "last_rest_sync": state.last_rest_sync.isoformat()
                        if state.last_rest_sync
                        else None,
                        "last_stream_event": state.last_stream_event.isoformat()
                        if state.last_stream_event
                        else None,
                        "last_error": state.last_error,
                    }
                if changed:
                    log.info("runtime settings updated from panel: %s", ", ".join(changed))
                await bus.set_status(
                    "scheduler",
                    {**payload, "llm": advisor.status(),
                     "at": dt.datetime.now(dt.timezone.utc).isoformat()},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat failed: %s", exc)
            await self._sleep(20)

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
            self._stop.set()
            try:
                async with session_scope() as session:
                    await repo.log_event(
                        session, "Scheduler shutting down", level="warn",
                        source="scheduler", notify=False,
                    )
                await asyncio.wait_for(
                    notify.send(notify.Notification(
                        "system", "warn", "زمان‌بند خاموش شد",
                        "همگام‌سازی با صرافی، منحنی سرمایه و پایش IP تا بالا آمدن دوباره متوقف است.",
                    )),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001 - shutdown must finish regardless
                log.debug("shutdown notification skipped: %s", exc)
            await notify.flush(timeout=3.0)
            if self.stream is not None:
                await self.stream.aclose()
            await self.client.aclose()
            await bus.close_redis()


async def main() -> None:
    scheduler = Scheduler()
    loop = asyncio.get_running_loop()
    shutdown_task: asyncio.Task | None = None

    def _on_signal() -> None:
        nonlocal shutdown_task
        # Keep a reference: a bare create_task() can be garbage collected.
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(scheduler.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass
    try:
        await scheduler.start()
    finally:
        await scheduler.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
