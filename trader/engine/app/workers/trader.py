"""The trading loop.

Two cores, selected by STRATEGY_CORE (app/config.py):

portfolio4h (default) - on every closed 4h candle, once the bars of every
symbol in the universe are stored (or a short grace period has passed), one
evaluation of the whole universe (app/strategies/core4h.py):
  1. BTC regime, momentum ranks and turtle breakouts for every symbol
  2. exits first: rank / channel / regime exits for open positions, by the
     family that opened them (position.meta["strategy"])
  3. entries, best score first: one signal row per candidate, the risk
     manager with the family's sizing, the optional Claude review, the order

ensemble30m - on every closed candle of the decision timeframe, per symbol:
  1. manage the open position first (stops, targets, trailing)
  2. build features, ask the ensemble, ask the ML model
  3. run the risk manager
  4. optionally get a second opinion from Claude
  5. place the order

On every tick, for both cores: check stops against the live price, not just
on candle close.

Locking: one asyncio.Lock per symbol. A candle evaluation and the tick watchdog
for the same symbol never interleave, while a slow step on one symbol (an
exchange round trip, the LLM review) cannot stall stop checks on another. The
LLM review runs with no lock held and no transaction open; the gates are
re-validated afterwards, before any order is sent.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal
import time
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.config import CORE4H_TIMEFRAME, MARKET_GATE_SYMBOL, settings
from app.db import repo
from app.db.models import Position, Signal
from app.db.session import init_db, session_scope
from app.exchange.registry import make_client
from app.exchange.symbols import SymbolRegistry
from app.execution.executor import Executor, build_executor
from app.features.build import build_features, classify_regime, feature_matrix, latest_snapshot
from app.llm.advisor import TradeReview, advisor
from app.ml.model import Predictor
from app.notify import service as notify
from app.notify import texts as notify_texts
from app.risk.manager import AccountState, EntryOverrides, RiskManager, position_family
from app.strategies import core4h
from app.strategies.ensemble import Decision, Ensemble
from app.util.logging_setup import setup_logging
from app.util.runtime_settings import refresh_runtime_settings
from app.util.timeframes import tf_seconds
from app.workers import bus

log = setup_logging("trader")

# Hard cap on one trade review. The advisor is optional: a slow answer must not
# hold the entry decision for minutes.
LLM_REVIEW_TIMEOUT = 30.0
# A failed exit is retried by the engine at most this often; the user stream
# and the REST sweep can finish it in between.
CLOSE_RETRY_SECONDS = 60

# ---- portfolio4h timing ----
CORE_BAR_MS = tf_seconds(CORE4H_TIMEFRAME) * 1000
# How long after a 4h close to wait for the last symbol's bar before
# evaluating with whatever is stored. Kline closes for one bar arrive within
# a second or two; the grace covers a slow websocket or a REST gap repair.
CORE_GRACE_SECONDS = 90
# The simulator fills entries at the bar close. An entry taken this long
# after the close is a different trade, so a late evaluation (engine restart,
# data outage) still processes exits but only records the entry signals.
CORE_LATE_ENTRY_SECONDS = 90 * 60
# Inside the entry window a bar is not evaluated on fewer stored symbols than
# this: the momentum ranking is cross-sectional, and after a restart the
# backfill / gap repair lands the symbols one by one, so the first wake after
# the grace could otherwise rank (and buy) the top 3 of whatever had arrived.
# Same floor as the "the study validated 12" warning in _evaluate_portfolio.
CORE_MIN_UNIVERSE = 8
CORE_CLOCK_SECONDS = 20
CORE_LAST_BAR_KEY = "core4h_last_open_time"
# Bars that closed while the engine could not evaluate them (an outage that
# spanned a close, a BTC bar that never arrived in time) are caught up in
# order on the next tick, exits first. Anything older than this many bars is
# dropped with a warning: turtle and regime exits are re-checked on every bar
# anyway, and a momentum name that should have left is re-ranked on the next
# rebalance.
CORE_CATCHUP_MAX_BARS = 42
# A position flagged stop_missing (its exchange stop was rejected, cancelled or
# lost) gets a new one at most this often from the heartbeat.
STOP_RESTORE_SECONDS = 60
# A catch-up bar whose exits raised is retried on the next clock ticks; after
# this many attempts it is given up on (its exits are deferred instead) so one
# permanently failing bar cannot stall the clock for every later bar.
CORE_CATCHUP_ATTEMPTS = 3
# A deferred exit check that still cannot be read (the symbol has too little
# history, the gate bar never arrived) is retried this many times before the
# deferral is dropped with a warning; each attempt rebuilds the feature frames.
DEFERRED_CHECK_ATTEMPTS = 6


@dataclass
class CoreDecision(Decision):
    """A 4h-core candidate: an ensemble Decision plus the family that made it.

    The executor stores `family` in position.meta["strategy"]; the risk
    manager, the tick watchdog and the next evaluation all key their exit
    rules on it.
    """

    family: str = ""
    open_time: int = 0


class TradingEngine:
    def __init__(self) -> None:
        self.client = make_client()
        self.registry = SymbolRegistry(self.client)
        self.ensemble = Ensemble()
        self.risk = RiskManager()
        self.predictor = Predictor()
        self.executor: Executor | None = None
        self.symbols: list[str] = []          # tradable
        self.data_symbols: list[str] = []     # tradable plus BTC, the 4h core's ranking universe
        self._stop = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._market_stance = "neutral"
        self._risk_multiplier = 1.0
        self._last_candle_seen: dict[str, int] = {}
        # portfolio4h: which symbols have reported the bar that opened at T
        self._core_seen: dict[int, set[str]] = defaultdict(set)
        self._core_last_open_time: int | None = None
        self._core_wake = asyncio.Event()
        self._core_eval_lock = asyncio.Lock()
        self._core_status: dict = {}
        # bar open time -> failed catch-up attempts, and position id -> failed
        # deferred-exit attempts. In memory on purpose: a restart re-reads the
        # durable state (core4h_last_open_time, positions.exit_check_bar) and
        # starts the counting again.
        self._catchup_failures: dict[int, int] = {}
        self._deferred_failures: dict[int, int] = {}
        # shutdown() is reached twice (signal handler task + main's finally);
        # the lock makes the second caller wait for the first, not repeat it.
        self._shutdown_lock = asyncio.Lock()
        self._shut_down = False

    def _lock_for(self, symbol: str) -> asyncio.Lock:
        return self._locks[symbol]

    # ------------------------------------------------------------------
    async def start(self) -> None:
        await init_db()
        await self._refresh_settings()
        await self.client.sync_time()

        ok, rejected = await self.registry.validate_universe(settings.active_symbols)
        self.symbols = ok
        if not self.symbols:
            raise RuntimeError("no tradable symbols configured")
        if settings.core_is_portfolio4h:
            ok, rejected = await self.registry.validate_universe(settings.data_symbols)
            self.data_symbols = ok
            if MARKET_GATE_SYMBOL not in self.data_symbols:
                raise RuntimeError(f"{MARKET_GATE_SYMBOL} is required for the 4h market gate")
            if rejected:
                log.warning("universe symbols skipped: %s", ", ".join(rejected))
        else:
            self.data_symbols = list(self.symbols)

        self.executor = build_executor(self.client, self.registry)

        if (
            settings.trading_mode == "live"
            and settings.binance_use_bnb_fees
            and self.client.supports_fee_token
        ):
            try:
                await self.client.set_bnb_fee_discount(True)
                log.info("BNB fee discount enabled")
            except Exception as exc:  # noqa: BLE001 - not fatal
                log.warning("could not enable the BNB fee discount: %s", exc)

        await self._seed_capital_base()
        if settings.core_is_portfolio4h:
            async with session_scope() as session:
                stored = await repo.get_setting(session, CORE_LAST_BAR_KEY, None)
            self._core_last_open_time = int(stored) if stored else None
        else:
            await self._load_models()

        async with session_scope() as session:
            armed = bool(await repo.get_setting(session, "trading_armed", settings.trading_armed))
            await repo.log_event(
                session,
                f"Trading engine online in {settings.trading_mode} mode "
                f"({settings.strategy_core}, {len(self.symbols)} symbols)",
                source="engine",
                data={
                    "mode": settings.trading_mode,
                    "core": settings.strategy_core,
                    "symbols": self.symbols,
                    "universe": self.data_symbols,
                    "armed": armed,
                },
                notify=False,
            )
        notify.emit(
            "system", "info", "موتور معامله‌گر روشن شد",
            f"حالت {notify_texts.MODE_FA.get(settings.trading_mode, settings.trading_mode)} · "
            f"هسته {notify_texts.CORE_FA.get(settings.strategy_core, settings.strategy_core)} · "
            f"{len(self.symbols)} نماد · مسلح: {'بله' if armed else 'خیر'}",
            {"mode": settings.trading_mode, "armed": armed},
        )

        log.info(
            "engine ready | mode=%s core=%s symbols=%s tf=%s",
            settings.trading_mode, settings.strategy_core, ",".join(self.symbols),
            settings.decision_timeframe,
        )

        tasks = [self._event_loop(), self._position_watchdog(), self._heartbeat()]
        if settings.core_is_portfolio4h:
            tasks.append(self._portfolio_clock())
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

    async def _seed_capital_base(self) -> None:
        """First run: record the starting capital so percentages have a baseline."""
        async with session_scope() as session:
            base = await repo.capital_base(session)
            if base > 0:
                return
            await repo.add_capital_flow(
                session,
                kind="deposit",
                amount=settings.initial_capital_usdt,
                asset=settings.quote_asset,
                source="initial",
                note="Initial capital from configuration",
                equity_before=0.0,
                equity_after=settings.initial_capital_usdt,
            )
            log.info("capital base seeded at %.2f %s", settings.initial_capital_usdt, settings.quote_asset)

    async def _load_models(self) -> None:
        if not settings.ml_enabled:
            return
        async with session_scope() as session:
            for symbol in self.symbols:
                record = await repo.active_model(session, symbol, settings.timeframe)
                if record and self.predictor.load(
                    f"{symbol}:{settings.timeframe}", record.path
                ):
                    log.info(
                        "loaded model for %s (auc=%.3f)",
                        symbol, record.metrics.get("auc", 0),
                    )

    # ------------------------------------------------------------------
    async def _event_loop(self) -> None:
        """React to closed candles and to panel commands."""
        async for channel, payload in bus.subscribe(bus.CH_CANDLES, bus.CH_COMMANDS):
            if self._stop.is_set():
                break
            try:
                if channel == bus.CH_CANDLES:
                    await self._on_candle_closed(payload)
                elif channel == bus.CH_COMMANDS:
                    await self._on_command(payload)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.exception("event handling failed: %s", exc)

    async def _on_candle_closed(self, payload: dict) -> None:
        symbol = payload.get("symbol")
        timeframe = payload.get("timeframe")
        open_time = int(payload.get("open_time", 0) or 0)

        if settings.core_is_portfolio4h:
            if timeframe != CORE4H_TIMEFRAME or symbol not in self.data_symbols:
                return
            self._core_seen[open_time].add(symbol)
            # The clock decides when the universe is complete enough to evaluate.
            self._core_wake.set()
            return

        if timeframe != settings.timeframe or symbol not in self.symbols:
            return
        if self._last_candle_seen.get(symbol) == open_time:
            return
        self._last_candle_seen[symbol] = open_time

        await self._evaluate_symbol(symbol)

    async def _on_command(self, payload: dict) -> None:
        action = payload.get("action")
        log.info("command received: %s", payload)

        if action == "close_position":
            position_id = payload.get("position_id")
            await self._close_by_id(int(position_id), "manual")
        elif action == "flatten_all":
            await self._flatten_all("manual flatten")
        elif action == "reload_models":
            if not settings.core_is_portfolio4h:
                await self._load_models()
        elif action == "evaluate_now":
            if settings.core_is_portfolio4h:
                # Any bar the clock has not reached yet runs first, in order,
                # so a forced evaluation cannot skip an unevaluated bar.
                await self._maybe_evaluate_portfolio()
                await self._evaluate_portfolio(self._last_closed_bar(), forced=True)
            else:
                for symbol in self.symbols:
                    await self._evaluate_symbol(symbol)

    # ------------------------------------------------------------------
    # portfolio4h core
    # ------------------------------------------------------------------
    @staticmethod
    def _last_closed_bar(now_ms: int | None = None) -> int:
        """Open time of the most recent 4h bar that has closed."""
        now_ms = now_ms or int(time.time() * 1000)
        return (now_ms // CORE_BAR_MS) * CORE_BAR_MS - CORE_BAR_MS

    async def _portfolio_clock(self) -> None:
        """Evaluate each closed 4h bar exactly once.

        Wakes on every stored 4h candle and every CORE_CLOCK_SECONDS. A bar is
        evaluated when every universe symbol has stored it, or when the grace
        period after its close has passed and at least BTC (the gate) is in.
        A bar that never gets its BTC candle is skipped when the next one
        closes: the gate cannot be read, and guessing it is worse than
        sitting out four hours.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._core_wake.wait(), timeout=CORE_CLOCK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._core_wake.clear()
            if self._stop.is_set():
                break
            try:
                await self._maybe_evaluate_portfolio()
            except Exception as exc:  # noqa: BLE001 - the clock must survive anything
                log.exception("portfolio evaluation failed: %s", exc)

    @staticmethod
    def _pending_bars(last_marked: int | None, last_closed: int) -> list[int]:
        """Closed 4h bars not yet evaluated, oldest first.

        On the first run only the newest closed bar counts. Otherwise every
        bar between the last evaluated one and the newest closed one is due:
        an engine that was down across a close must still run that bar's
        exits. Capped at CORE_CATCHUP_MAX_BARS (the older ones are logged
        and skipped).
        """
        if last_marked is None:
            return [last_closed]
        if last_closed <= last_marked:
            return []
        bars = list(range(last_marked + CORE_BAR_MS, last_closed + 1, CORE_BAR_MS))
        if len(bars) > CORE_CATCHUP_MAX_BARS:
            log.warning(
                "%s closed 4h bars were never evaluated (%s .. %s); catching up the last %s",
                len(bars), _iso(bars[0]), _iso(bars[-1]), CORE_CATCHUP_MAX_BARS,
            )
            bars = bars[-CORE_CATCHUP_MAX_BARS:]
        return bars

    async def _maybe_evaluate_portfolio(self) -> None:
        pending = self._pending_bars(self._core_last_open_time, self._last_closed_bar())
        if not pending:
            await self._maybe_recheck_deferred_exits()
            return
        # Catch-up: every bar that closed while the engine could not evaluate
        # it runs now, in order. Each one runs its rule exits and is marked;
        # its entries are refused as late by _evaluate_portfolio (the
        # simulator's entry is the bar close, which is long gone). Only the
        # newest bar goes through the completeness checks below.
        for open_time in pending[:-1]:
            minutes = (time.time() - (open_time + CORE_BAR_MS) / 1000) / 60
            log.warning("catching up 4h bar %s (closed %.0f min ago)", _iso(open_time), minutes)
            await self._evaluate_portfolio(open_time, catch_up=True)
            if self._stop.is_set():
                return
            if (self._core_last_open_time or 0) >= open_time:
                self._catchup_failures.pop(open_time, None)
                continue
            # The bar was left unmarked: its exits or entries raised. Stop
            # here so the next tick retries it - marking the later bars would
            # bury it for good. After CORE_CATCHUP_ATTEMPTS give up on it,
            # but defer its exit rules onto the open positions first.
            failures = self._catchup_failures.get(open_time, 0) + 1
            self._catchup_failures[open_time] = failures
            if failures < CORE_CATCHUP_ATTEMPTS:
                log.warning(
                    "catch-up of 4h bar %s did not complete (attempt %s/%s); retrying on the next tick",
                    _iso(open_time), failures, CORE_CATCHUP_ATTEMPTS,
                )
                return
            log.error(
                "catch-up of 4h bar %s failed %s times; deferring its exit rules and moving on",
                _iso(open_time), failures,
            )
            await self._defer_all_open_positions(open_time, "catch-up failed")
            await self._mark_core_bar(open_time)
            self._catchup_failures.pop(open_time, None)
        open_time = pending[-1]
        # Bars pushed over the bus plus whatever the backfill / gap repair
        # stored while the engine was not listening.
        async with session_scope() as session:
            stored = set()
            for symbol in self.data_symbols:
                latest = await repo.last_candle_time(session, symbol, CORE4H_TIMEFRAME)
                if latest is not None and latest >= open_time:
                    stored.add(symbol)
        stored |= self._core_seen.get(open_time, set())
        missing = [s for s in self.data_symbols if s not in stored]
        since_close = time.time() - (open_time + CORE_BAR_MS) / 1000
        if MARKET_GATE_SYMBOL not in stored:
            self._core_status = {"waiting_for": MARKET_GATE_SYMBOL, "bar": open_time}
            return
        if missing and since_close < CORE_GRACE_SECONDS:
            self._core_status = {"waiting_for": missing, "bar": open_time}
            return
        if len(stored) < min(CORE_MIN_UNIVERSE, len(self.data_symbols)) and since_close < CORE_LATE_ENTRY_SECONDS:
            # Too few symbols to rank while entries are still allowed (a
            # backfill in progress). Past the entry window the bar is taken
            # as it is: exits still run, entries are already refused as late.
            self._core_status = {
                "waiting_for": missing, "bar": open_time,
                "note": f"universe incomplete ({len(stored)}/{len(self.data_symbols)} stored)",
            }
            return
        if missing:
            log.warning(
                "4h bar %s evaluated without %s (not stored %.0fs after the close)",
                _iso(open_time), ",".join(missing), since_close,
            )
        await self._evaluate_portfolio(open_time)

    async def _load_core_features(self) -> dict[str, pd.DataFrame]:
        """4h feature frames for every universe symbol with enough history."""
        frames: dict[str, pd.DataFrame] = {}
        async with session_scope() as session:
            candles = {
                symbol: await repo.load_candles(
                    session, symbol, CORE4H_TIMEFRAME, settings.candle_history
                )
                for symbol in self.data_symbols
            }
        for symbol, df in candles.items():
            if len(df) < core4h.MIN_BARS:
                log.info(
                    "%s: %s 4h candles, need %s - not in this evaluation",
                    symbol, len(df), core4h.MIN_BARS,
                )
                continue
            try:
                # CPU-bound (supertrend loops per bar): keep the tick watchdog responsive.
                frames[symbol] = await asyncio.to_thread(build_features, df)
            except ValueError as exc:
                log.warning("%s: features failed: %s", symbol, exc)
        return frames

    async def _evaluate_portfolio(
        self, open_time: int, forced: bool = False, catch_up: bool = False
    ) -> core4h.Universe | None:
        """One decision pass over the universe for the 4h bar that opened at `open_time`.

        Returns the evaluated view (also used by the dry-run tooling), or
        None when the bar could not be evaluated. `catch_up` marks a bar
        that is already history (see _maybe_evaluate_portfolio): it is
        evaluated on the same causal frames, and a missing BTC bar skips it
        instead of holding the clock.
        """
        async with self._core_eval_lock:
            if (
                not forced
                and self._core_last_open_time is not None
                and open_time <= self._core_last_open_time
            ):
                return None
            frames = await self._load_core_features()
            view = core4h.evaluate_universe(frames, open_time, MARKET_GATE_SYMBOL)
            if not view.market_present:
                if catch_up:
                    # The gate candle is not stored (yet). The bar is marked so
                    # the clock can move on, and every open family position
                    # remembers it: _recheck_deferred_exits applies its rules
                    # as soon as the gap repair stores the candle.
                    log.warning(
                        "4h bar %s: no %s bar stored; its exit rules are deferred",
                        _iso(open_time), MARKET_GATE_SYMBOL,
                    )
                    await self._defer_all_open_positions(open_time, f"no {MARKET_GATE_SYMBOL} bar")
                    await self._mark_core_bar(open_time)
                    return None
                log.warning("4h bar %s: no %s bar stored, skipped", _iso(open_time), MARKET_GATE_SYMBOL)
                return None

            since_close = time.time() - (open_time + CORE_BAR_MS) / 1000
            late = since_close > CORE_LATE_ENTRY_SECONDS
            if len(view.ranked) < 8:
                log.warning(
                    "momentum ranking over %s symbols (%s); the study validated 12",
                    len(view.ranked), ",".join(view.ranked),
                )
            log.info(
                "4h bar %s | BTC %s EMA200 (%.2f vs %.2f, %s) | universe %s/%s | %s candidates%s",
                _iso(open_time), "above" if view.regime_ok else "below",
                view.market_close, view.market_ema200,
                "rising" if view.regime_rising else "not rising",
                len(view.ranked), len(self.data_symbols), len(view.candidates()),
                f" | {since_close / 60:.0f} min after the close" if late else "",
            )

            self._core_status = {
                "bar": open_time,
                "regime_ok": view.regime_ok,
                "regime_rising": view.regime_rising,
                "ranked": len(view.ranked),
                "candidates": [f"{s.family}:{s.symbol}" for s in view.candidates()],
                "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

            # The bar is only marked as evaluated once exits and entries have
            # all run without an exception. Marking it first (as this used to)
            # meant one network blip on the rebalance close silently skipped
            # the whole day's ranking - the clock would never come back to it.
            failed = False

            # ---- exits before entries ----
            try:
                await self._core_exits(view, frames)
            except Exception as exc:  # noqa: BLE001 - keep going, retry on the next tick
                failed = True
                log.exception("4h exits failed on bar %s: %s", _iso(open_time), exc)

            # ---- entries, best score first ----
            for candidate in view.candidates():
                if candidate.symbol not in self.symbols:
                    continue  # ranked for the others' sake, never bought
                decision = self._core_decision(candidate, view, frames[candidate.symbol])
                if late:
                    decision.tradable = False
                    decision.rejection = (
                        f"bar closed {since_close / 60:.0f} min ago; entry window passed"
                    )
                try:
                    await self._record_and_maybe_trade(
                        decision,
                        frames[candidate.symbol],
                        overrides=self._core_overrides(candidate.family),
                    )
                except Exception as exc:  # noqa: BLE001 - one symbol must not sink the rest
                    failed = True
                    log.exception(
                        "entry failed for %s %s on bar %s: %s",
                        candidate.family, candidate.symbol, _iso(open_time), exc,
                    )

            if failed:
                # Re-run on the next clock tick while the entry window is open;
                # "already holding" and the closed-position checks make a
                # repeat pass safe. After the window the bar is recorded as
                # rejected like any other late bar.
                log.warning("4h bar %s left unmarked after errors; will retry", _iso(open_time))
                return view

            await self._mark_core_bar(open_time)
            return view

    async def _mark_core_bar(self, open_time: int) -> None:
        """Record that the bar opening at `open_time` has been evaluated."""
        self._core_last_open_time = max(self._core_last_open_time or 0, open_time)
        self._core_seen = defaultdict(set, {t: s for t, s in self._core_seen.items() if t > open_time})
        async with session_scope() as session:
            await repo.set_setting(session, CORE_LAST_BAR_KEY, open_time)

    @staticmethod
    def _core_overrides(family_name: str) -> EntryOverrides:
        """Family sizing with the user's caution factor applied to the risk.

        Only the risk per trade is scaled; caps and cooldowns stay as
        validated. A factor below 1 is the same trade list at a smaller size,
        which the scaling study showed is the one reliable way to cut
        drawdown without changing the strategy.
        """
        family = core4h.FAMILIES[family_name]
        base = EntryOverrides.for_family(family)
        scale = max(0.25, min(1.5, float(settings.core4h_risk_scale or 1.0)))
        return EntryOverrides(
            risk_pct=family.risk_pct * scale,
            max_position_pct=base.max_position_pct,
            cooldown_minutes=base.cooldown_minutes,
            require_target=base.require_target,
            family=base.family,
            max_family_positions=base.max_family_positions,
        )

    async def _core_exits(self, view: core4h.Universe, frames: dict[str, pd.DataFrame]) -> None:
        """Rule exits at the bar close, then the hard stop against the close.

        Order of business for every open position:
          1. an exit that was deferred (its symbol had no candle on an
             earlier bar) is checked on that bar now that the data is in;
          2. the family rule on this bar (rank / channel / regime);
          3. an exit that is still pending because the exchange refused the
             order is retried;
          4. the hard stop against the latest close.
        Positions from the old core (no family) get the ensemble's own
        `should_exit` on the 4h close so they still wind down; the tick
        watchdog covers their stop between bars either way.
        """
        await self._recheck_deferred_exits(frames, {view.open_time: view}, view.open_time)

        # A bar that is no longer the newest one is history: its close is not
        # a price anything can be sold at, so the exit is priced at the market.
        stale = view.open_time < self._last_closed_bar()
        marks = await bus.get_marks() if stale else {}

        async with session_scope() as session:
            position_ids = [p.id for p in await repo.open_positions(session)]

        for position_id in position_ids:
            async with session_scope() as session:
                position = await session.get(Position, position_id)
                if position is None or position.status != "open":
                    continue
                symbol = position.symbol
            async with self._lock_for(symbol):
                async with session_scope() as session:
                    position = await session.get(Position, position_id)
                    if position is None or position.status != "open":
                        continue
                    family = position_family(position)
                    in_family = family in core4h.FAMILIES
                    signal = view.signal(family, symbol) if in_family else None
                    frame = frames.get(symbol)
                    close = float(frame["close"].iloc[-1]) if frame is not None else None
                    atr = float(frame["atr"].iloc[-1] or 0.0) if frame is not None else 0.0

                    if in_family and frame is not None:
                        self._track_high_water(position, frame, view.open_time)

                    if signal is not None and signal.exit:
                        price = float(marks.get(symbol) or signal.price) if stale else signal.price
                        await self._close_position(session, position, price, signal.exit_reason)
                        continue
                    if in_family and signal is None and view.market_present:
                        # This symbol had no candle for the bar: its rule
                        # could not be read. Remember the bar; it is checked
                        # as soon as the candle is stored.
                        self._defer_exit_check(position, view.open_time)

                    if position.pending_exit_reason:
                        price = close if close is not None else (
                            position.pending_exit_price or position.entry_price
                        )
                        await self._close_position(
                            session, position, float(price), position.pending_exit_reason
                        )
                        continue
                    if close is None:
                        continue
                    if not in_family and self.risk.update_trailing(position, close, atr):
                        log.info("%s trailing stop moved to %.6f", symbol, position.stop_price)
                    should_exit, reason = self.risk.should_exit(position, close, atr)
                    if should_exit:
                        await self._close_position(session, position, close, reason)

    @staticmethod
    def _track_high_water(position: Position, frame: pd.DataFrame, open_time: int) -> None:
        """Peak price since entry, from the closed bar's high (low for shorts).

        The 4h families never trail, so this is bookkeeping only: it lets the
        panel and the committee show how much open profit a trade reached
        and gave back, which the strategy's own rules never look at.
        """
        ot = frame["open_time"].to_numpy()
        pos = int(np.searchsorted(ot, open_time))
        if pos >= len(ot) or int(ot[pos]) != open_time:
            return
        row = frame.iloc[pos]
        if position.side == "long":
            high = float(row["high"])
            if high > (position.high_water or 0.0):
                position.high_water = max(high, position.entry_price)
        else:
            low = float(row["low"])
            if not position.high_water or low < position.high_water:
                position.high_water = min(low, position.entry_price)

    @staticmethod
    def _defer_exit_check(position: Position, open_time: int, why: str = "no 4h candle") -> None:
        """Remember the oldest bar whose exit rule for this position is unread.

        Never a bar older than the entry: a rule that fired before the
        position existed has nothing to say about it.
        """
        entry_bar = (position.meta or {}).get("entry_open_time")
        if entry_bar and open_time < int(entry_bar):
            return
        current = position.exit_check_bar
        if current is not None and int(current) <= open_time:
            return
        position.exit_check_bar = open_time
        log.warning(
            "%s: %s for bar %s, its %s exit rule is deferred until the data is there",
            position.symbol, why, _iso(open_time), position_family(position),
        )

    async def _defer_all_open_positions(self, open_time: int, why: str) -> None:
        """Defer the exit rules of every open 4h-family position onto `open_time`."""
        async with session_scope() as session:
            for position in await repo.open_positions(session):
                if position_family(position) in core4h.FAMILIES:
                    self._defer_exit_check(position, open_time, why)

    async def _recheck_deferred_exits(
        self,
        frames: dict[str, pd.DataFrame],
        views: dict[int, core4h.Universe],
        until_open_time: int,
    ) -> None:
        """Apply rule exits that were due on bars a position's symbol had no candle for.

        `exit_check_bar` names the first such bar. Every bar from there up to
        (excluding) `until_open_time` is evaluated on the causal frames and
        the first exit found is executed; a bar whose candle is still missing
        keeps the deferral in place.
        """
        async with session_scope() as session:
            deferred = [
                (p.id, p.symbol, int(p.exit_check_bar))
                for p in await repo.open_positions_with_deferred_exit(session)
            ]
        for position_id, symbol, first_bar in deferred:
            async with self._lock_for(symbol):
                async with session_scope() as session:
                    position = await session.get(Position, position_id)
                    if position is None or position.status != "open":
                        continue
                    family = position_family(position)
                    if family not in core4h.FAMILIES:
                        continue
                    checked_all = True
                    for bar in range(first_bar, until_open_time, CORE_BAR_MS):
                        view = views.get(bar)
                        if view is None:
                            view = core4h.evaluate_universe(frames, bar, MARKET_GATE_SYMBOL)
                            views[bar] = view
                        signal = view.signal(family, symbol) if view.market_present else None
                        if signal is None:
                            checked_all = False
                            break
                        if signal.exit:
                            log.warning(
                                "%s: deferred %s from bar %s applied now",
                                symbol, signal.exit_reason, _iso(bar),
                            )
                            position.exit_check_bar = None
                            marks = await bus.get_marks()
                            price = float(marks.get(symbol) or signal.price)
                            await self._close_position(
                                session, position, price, signal.exit_reason
                            )
                            checked_all = False  # settled either way
                            self._deferred_failures.pop(position_id, None)
                            break
                    if checked_all:
                        position.exit_check_bar = None
                        self._deferred_failures.pop(position_id, None)
                    elif position.exit_check_bar is not None:
                        # Still unreadable. Give up after a bounded number of
                        # attempts: every one rebuilds the feature frames, and
                        # the rule will be read again on the next bar anyway.
                        failures = self._deferred_failures.get(position_id, 0) + 1
                        self._deferred_failures[position_id] = failures
                        if failures >= DEFERRED_CHECK_ATTEMPTS:
                            log.error(
                                "%s: the deferred exit check from bar %s is still unreadable "
                                "after %s attempts; dropping it",
                                symbol, _iso(first_bar), failures,
                            )
                            position.exit_check_bar = None
                            self._deferred_failures.pop(position_id, None)

    async def _maybe_recheck_deferred_exits(self) -> None:
        """Between bars: run deferred exit checks once their candles have landed."""
        if self._core_last_open_time is None:
            return
        async with session_scope() as session:
            due = False
            for p in await repo.open_positions_with_deferred_exit(session):
                bar = int(p.exit_check_bar)
                latest = await repo.last_candle_time(session, p.symbol, CORE4H_TIMEFRAME)
                gate = await repo.last_candle_time(session, MARKET_GATE_SYMBOL, CORE4H_TIMEFRAME)
                if latest is not None and latest >= bar and gate is not None and gate >= bar:
                    due = True
                    break
        if not due:
            return
        async with self._core_eval_lock:
            frames = await self._load_core_features()
            await self._recheck_deferred_exits(
                frames, {}, self._core_last_open_time + CORE_BAR_MS
            )

    def _core_decision(
        self, signal: core4h.Signal, view: core4h.Universe, frame: pd.DataFrame
    ) -> CoreDecision:
        """Translate a family signal into the Decision the risk/exec path consumes."""
        row = frame.iloc[-1]
        family = core4h.FAMILIES[signal.family]
        return CoreDecision(
            symbol=signal.symbol,
            timeframe=CORE4H_TIMEFRAME,
            side="long",
            score=round(signal.score, 4),
            raw_score=round(signal.score, 4),
            regime=classify_regime(row),
            price=signal.price,
            atr=signal.atr,
            stop_price=signal.stop_price,
            target_price=0.0,          # no take-profit: rank / channel / regime exits
            expected_move_pct=0.0,
            contributions=_core_contributions(signal, view),
            reasons=[f"{family.name}: {family.research_name}"],
            tradable=signal.tradable,
            family=signal.family,
            open_time=signal.open_time,
        )

    # ------------------------------------------------------------------
    # ensemble30m core
    # ------------------------------------------------------------------
    async def _evaluate_symbol(self, symbol: str) -> None:
        async with session_scope() as session:
            candles = await repo.load_candles(
                session, symbol, settings.timeframe, settings.candle_history
            )
            htf_candles = await repo.load_candles(
                session, symbol, settings.htf_timeframe, 300
            )

        if len(candles) < 210:
            log.debug("%s: only %s candles, waiting for history", symbol, len(candles))
            return

        try:
            features = build_features(candles)
            htf_features = (
                build_features(htf_candles) if len(htf_candles) >= 60 else None
            )
        except ValueError as exc:
            log.debug("%s: %s", symbol, exc)
            return

        # --- exits are considered before entries ---
        async with self._lock_for(symbol):
            await self._manage_position(symbol, features)

        # --- ML probability ---
        ml_probability = None
        if settings.ml_enabled:
            key = f"{symbol}:{settings.timeframe}"
            if self.predictor.has(key):
                ml_probability = self.predictor.predict(
                    key, feature_matrix(features).tail(1)
                )

        decision = self.ensemble.evaluate(
            symbol, settings.timeframe, features, htf_features, ml_probability
        )

        await self._record_and_maybe_trade(decision, features)

    # ------------------------------------------------------------------
    async def _record_and_maybe_trade(
        self,
        decision: Decision,
        features: pd.DataFrame,
        overrides: EntryOverrides | None = None,
    ) -> None:
        symbol = decision.symbol
        market = "futures" if decision.side == "short" else "spot"

        # ---- 1. record the signal and run the risk gates (locked) ----
        async with self._lock_for(symbol):
            async with session_scope() as session:
                signal_row = await repo.record_signal(
                    session,
                    symbol=decision.symbol,
                    timeframe=decision.timeframe,
                    side=decision.side,
                    score=decision.score,
                    ml_probability=decision.ml_probability,
                    regime=decision.regime,
                    price=decision.price,
                    atr=decision.atr,
                    suggested_stop=decision.stop_price,
                    suggested_target=decision.target_price,
                    expected_move_pct=decision.expected_move_pct,
                    contributions=decision.contributions,
                    rejection_reason=decision.rejection or None,
                )
                signal_id = signal_row.id

                await bus.publish(
                    bus.CH_EVENTS,
                    {
                        "type": "signal",
                        "symbol": decision.symbol,
                        "side": decision.side,
                        "score": decision.score,
                        "regime": decision.regime,
                        "family": getattr(decision, "family", None),
                        "tradable": decision.tradable,
                        "rejection": decision.rejection,
                    },
                )

                if not decision.tradable:
                    return

                try:
                    symbol_info = await self.registry.get(symbol, market)
                except KeyError as exc:
                    signal_row.rejection_reason = str(exc)
                    return

                state = await self._account_state(session)
                verdict = await self.risk.evaluate_entry(
                    session, decision, state, symbol_info, self._risk_multiplier, overrides
                )
                if not verdict.approved:
                    signal_row.rejection_reason = verdict.reason
                    log.info("%s %s rejected: %s", symbol, decision.side, verdict.reason)
                    return

        # ---- 2. optional LLM second opinion: no lock held, no transaction open ----
        review: TradeReview | None = None
        usage: dict = {}
        reviewed = bool(settings.llm_enabled and settings.llm_can_veto and advisor.enabled)
        if reviewed:
            review, usage = await self._review_with_llm(decision, features)

        # ---- 3. re-validate and place the order (locked) ----
        async with self._lock_for(symbol):
            async with session_scope() as session:
                signal_row = await session.get(Signal, signal_id)
                if signal_row is None:
                    return

                multiplier = self._risk_multiplier
                if review is not None:
                    signal_row.llm_verdict = review.verdict
                    signal_row.llm_note = review.reasoning
                    await repo.record_llm_review(
                        session,
                        scope="trade",
                        symbol=decision.symbol,
                        stance=review.verdict,
                        risk_multiplier=review.size_multiplier,
                        confidence=review.confidence,
                        reasoning=review.reasoning,
                        payload={"concerns": review.concerns, "side": decision.side},
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                    )
                    if review.verdict == "veto":
                        signal_row.rejection_reason = f"LLM veto: {review.reasoning[:120]}"
                        await repo.log_event(
                            session,
                            f"Claude vetoed {decision.side} {decision.symbol}: "
                            f"{review.reasoning[:200]}",
                            level="warn",
                            source="llm",
                        )
                        return
                    if review.verdict == "reduce":
                        # The advisor can only stand the trade down: the
                        # validated sizing is the ceiling (investment policy).
                        multiplier *= min(1.0, float(review.size_multiplier or 1.0))

                if reviewed:
                    # The lock was released while Claude was thinking: the
                    # account, the gates and the position table may have moved.
                    state = await self._account_state(session)
                # Sizing again also re-checks "already holding" and every
                # global gate, so nothing that changed in between is missed.
                verdict = await self.risk.evaluate_entry(
                    session, decision, state, symbol_info, multiplier, overrides
                )
                if not verdict.approved:
                    signal_row.rejection_reason = (
                        f"reduced size not viable: {verdict.reason}"
                        if review is not None and review.verdict == "reduce"
                        else verdict.reason
                    )
                    log.info(
                        "%s %s rejected: %s", symbol, decision.side, signal_row.rejection_reason
                    )
                    return

                position = await self.executor.open_position(
                    session, decision, verdict, symbol_info, signal_id
                )
                if position is None:
                    signal_row.rejection_reason = "exchange rejected the order"
                    return

                signal_row.executed = True
                # The fill and its protective stop are on the exchange: make the
                # rows durable before anything cosmetic below can roll them back.
                await session.commit()

                family = getattr(decision, "family", None)
                await repo.log_event(
                    session,
                    f"Opened {position.side} {position.symbol} "
                    f"{position.qty} @ {position.entry_price:.6f} "
                    f"(stop {position.stop_price:.6f}, "
                    + (f"target {position.target_price:.6f})" if position.target_price else "no target)")
                    + (f" [{family}]" if family else ""),
                    level="trade",
                    source="engine",
                    data={
                        "position_id": position.id,
                        "score": decision.score,
                        "regime": decision.regime,
                        "notional": verdict.notional,
                        "strategy": family or "ensemble",
                    },
                    notify=False,
                )
                notify.emit(
                    "trade", "trade", f"پوزیشن باز شد: {position.symbol}",
                    f"{notify_texts.side_fa(position.side)} {position.qty:g} @ {position.entry_price:.6g} · "
                    f"ارزش {position.notional:.2f} {settings.quote_asset} · حد ضرر {position.stop_price:.6g}"
                    + (f" · حد سود {position.target_price:.6g}" if position.target_price else "")
                    + f" · {notify_texts.family_fa(family or 'ensemble')}",
                    {"position_id": position.id, "symbol": position.symbol},
                )
                await bus.publish(
                    bus.CH_EVENTS,
                    {
                        "type": "position_opened",
                        "symbol": position.symbol,
                        "side": position.side,
                        "qty": position.qty,
                        "entry": position.entry_price,
                        "family": family,
                    },
                )
                log.info(
                    "OPEN %s %s qty=%s entry=%.6f stop=%.6f target=%.6f score=%.2f%s",
                    position.side, position.symbol, position.qty, position.entry_price,
                    position.stop_price, position.target_price, decision.score,
                    f" [{family}]" if family else "",
                )

    async def _account_state(self, session) -> AccountState:
        marks = await bus.get_marks()
        equity, free = await self.executor.account_equity(session, marks)
        return await self.risk.account_state(
            session,
            equity,
            free,
            marks,
            quote_balance=self.executor.last_quote_balance(),
            balances_complete=self.executor.balances_complete(),
        )

    async def _review_with_llm(
        self, decision: Decision, features: pd.DataFrame
    ) -> tuple[TradeReview | None, dict]:
        """Bounded trade review. A timeout or failure means "no opinion"."""
        stop_pct = abs(decision.price - decision.stop_price) / decision.price * 100
        try:
            return await asyncio.wait_for(
                advisor.review_trade(
                    decision.symbol,
                    decision.side,
                    latest_snapshot(features),
                    decision.score,
                    decision.contributions,
                    self._market_stance,
                    decision.expected_move_pct,
                    stop_pct,
                ),
                timeout=LLM_REVIEW_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("LLM review timed out for %s; proceeding without it", decision.symbol)
        except Exception as exc:  # noqa: BLE001 - the advisor is optional
            log.warning("LLM review failed for %s: %s", decision.symbol, exc)
        return None, {}

    # ------------------------------------------------------------------
    async def _manage_position(self, symbol: str, features: pd.DataFrame) -> None:
        async with session_scope() as session:
            position = await repo.open_position_for(session, symbol)
            if position is None:
                return

            price = float(features["close"].iloc[-1])
            atr = float(features["atr"].iloc[-1] or 0)

            if self.risk.update_trailing(position, price, atr):
                log.info(
                    "%s trailing stop moved to %.6f", position.symbol, position.stop_price
                )
                await repo.log_event(
                    session,
                    f"Trailing stop for {position.symbol} moved to {position.stop_price:.6f}",
                    source="risk",
                    data={"position_id": position.id},
                )

            should_exit, reason = self.risk.should_exit(position, price, atr)
            if should_exit:
                await self._close_position(session, position, price, reason)

    async def _position_watchdog(self) -> None:
        """Tick-level stop checking, so a gap does not wait for a candle close."""
        async for channel, payload in bus.subscribe(bus.CH_TICKS):
            if self._stop.is_set():
                break
            symbol = payload.get("symbol")
            price = payload.get("price")
            if not symbol or price is None:
                continue
            try:
                async with self._lock_for(symbol):
                    async with session_scope() as session:
                        position = await repo.open_position_for(session, symbol)
                        if position is None:
                            continue
                        should_exit, reason = self.risk.should_exit(
                            position, float(price), 0.0
                        )
                        if should_exit:
                            await self._close_position(
                                session, position, float(price), reason
                            )
                            continue
                        if position.pending_exit_reason:
                            # A rule exit the exchange refused earlier: keep
                            # trying on every tick (throttled inside).
                            await self._close_position(
                                session, position, float(price), position.pending_exit_reason
                            )
            except Exception as exc:  # noqa: BLE001
                log.exception("watchdog failed for %s: %s", symbol, exc)

    async def _close_position(
        self,
        session,
        position: Position,
        price: float,
        reason: str,
        force: bool = False,
    ) -> None:
        # A failed exit is retried at most every CLOSE_RETRY_SECONDS so the
        # tick stream does not hammer the exchange; a manual command always goes.
        failed_at = (position.meta or {}).get("close_failed_at")
        if not force and failed_at and _seconds_since(failed_at) < CLOSE_RETRY_SECONDS:
            return

        try:
            symbol_info = await self.registry.get(position.symbol, position.market)
        except KeyError:
            log.error("cannot close %s: symbol info unavailable", position.symbol)
            return

        closed = await self.executor.close_position(
            session, position, price, reason, symbol_info
        )
        if closed.status != "closed":
            await self._mark_pending_exit(session, position, reason, price)
            return

        await repo.log_event(
            session,
            f"Closed {closed.side} {closed.symbol} @ {closed.exit_price:.6f} "
            f"({reason}) PnL {closed.realized_pnl:+.3f} {settings.quote_asset} "
            f"({closed.realized_pnl_pct:+.2f}%, {closed.r_multiple:+.2f}R)",
            level="trade",
            source="engine",
            data={
                "position_id": closed.id,
                "reason": reason,
                "strategy": position_family(closed),
            },
            notify=False,
        )
        closed.pending_exit_reason = None
        closed.pending_exit_at = None
        closed.pending_exit_price = 0.0
        closed.exit_check_bar = None
        closed.stop_missing = False
        won = closed.realized_pnl >= 0
        notify.emit(
            "trade", "trade",
            f"{'✅ سود' if won else '🔻 ضرر'} {closed.symbol}: {closed.realized_pnl:+.2f} {settings.quote_asset}",
            f"{notify_texts.side_fa(closed.side)} {closed.qty:g} بسته شد در {closed.exit_price:.6g} "
            f"({notify_texts.reason_fa(reason)}) · {closed.realized_pnl_pct:+.2f}٪ · "
            f"{closed.r_multiple:+.2f}R · {notify_texts.family_fa(position_family(closed))}",
            {"position_id": closed.id, "symbol": closed.symbol, "pnl": closed.realized_pnl},
        )
        await bus.publish(
            bus.CH_EVENTS,
            {
                "type": "position_closed",
                "symbol": closed.symbol,
                "pnl": closed.realized_pnl,
                "pnl_pct": closed.realized_pnl_pct,
                "reason": reason,
            },
        )
        log.info(
            "CLOSE %s %s @ %.6f (%s) pnl=%+.3f (%+.2f%%)",
            closed.side, closed.symbol, closed.exit_price, reason,
            closed.realized_pnl, closed.realized_pnl_pct,
        )

    async def _mark_pending_exit(
        self, session, position: Position, reason: str, price: float
    ) -> None:
        """An exit the exchange did not execute stays due until it is done.

        Rule exits are one-bar pulses (a rank exit only re-fires at the next
        rebalance), so without this a refused SELL would leave the position
        open for a day with nothing but its hard stop. The flag is retried by
        the tick watchdog, the heartbeat and every bar evaluation, at most
        once per CLOSE_RETRY_SECONDS.
        """
        if position.pending_exit_reason:
            return
        position.pending_exit_reason = reason[:48]
        position.pending_exit_price = price
        position.pending_exit_at = dt.datetime.now(dt.timezone.utc)
        await repo.log_event(
            session,
            f"Exit of {position.symbol} ({reason}) could not be executed; the engine "
            f"retries every {CLOSE_RETRY_SECONDS} s until it is done.",
            level="error",
            source="engine",
            data={"position_id": position.id, "reason": reason},
        )

    async def _retry_pending_exits(self) -> None:
        """Heartbeat: finish exits the exchange refused, even without ticks."""
        async with session_scope() as session:
            pending = [
                (p.id, p.symbol) for p in await repo.open_positions_with_pending_exit(session)
            ]
        if not pending:
            return
        marks = await bus.get_marks()
        for position_id, symbol in pending:
            async with self._lock_for(symbol):
                async with session_scope() as session:
                    position = await session.get(Position, position_id)
                    if position is None or position.status != "open":
                        continue
                    if not position.pending_exit_reason:
                        continue
                    price = float(
                        marks.get(symbol) or position.pending_exit_price or position.entry_price
                    )
                    await self._close_position(
                        session, position, price, position.pending_exit_reason
                    )

    async def _restore_missing_stops(self) -> None:
        """Heartbeat: a live position flagged stop_missing gets its exchange stop back."""
        if self.executor is None or settings.trading_mode == "paper":
            return
        async with session_scope() as session:
            flagged = [(p.id, p.symbol) for p in await repo.open_positions_missing_stop(session)]
        for position_id, symbol in flagged:
            async with self._lock_for(symbol):
                async with session_scope() as session:
                    position = await session.get(Position, position_id)
                    if position is None or position.status != "open" or not position.stop_missing:
                        continue
                    last = position.stop_retry_at
                    if last is not None:
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=dt.timezone.utc)
                        age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
                        if age < STOP_RESTORE_SECONDS:
                            continue
                    position.stop_retry_at = dt.datetime.now(dt.timezone.utc)
                    try:
                        symbol_info = await self.registry.get(position.symbol, position.market)
                    except KeyError:
                        continue
                    await self.executor.ensure_protective_stop(session, position, symbol_info)

    async def _close_by_id(self, position_id: int, reason: str) -> None:
        # Find the symbol first so the right per-symbol lock can be taken.
        async with session_scope() as session:
            position = await session.get(Position, position_id)
            symbol = position.symbol if position is not None else None
        if symbol is None:
            return

        marks = await bus.get_marks()
        async with self._lock_for(symbol):
            async with session_scope() as session:
                position = await session.get(Position, position_id)
                if position is None or position.status != "open":
                    return
                price = marks.get(position.symbol, position.entry_price)
                await self._close_position(session, position, price, reason, force=True)

    async def _flatten_all(self, reason: str) -> None:
        async with session_scope() as session:
            position_ids = [p.id for p in await repo.open_positions(session)]
        for position_id in position_ids:
            await self._close_by_id(position_id, reason)

    # ------------------------------------------------------------------
    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                async with session_scope() as session:
                    changed = await refresh_runtime_settings(session)
                    open_count = len(await repo.open_positions(session))
                    kill = await repo.get_setting(session, "kill_switch", False)
                    armed = await repo.get_setting(
                        session, "trading_armed", settings.trading_armed
                    )
                if changed:
                    log.info("runtime settings updated from panel: %s", ", ".join(changed))
                await self._retry_pending_exits()
                await self._restore_missing_stops()
                await bus.set_status(
                    "engine",
                    {
                        "mode": settings.trading_mode,
                        "core": settings.strategy_core,
                        "timeframe": settings.decision_timeframe,
                        "armed": bool(armed),
                        "kill_switch": bool(kill),
                        "open_positions": open_count,
                        "market_stance": self._market_stance,
                        "risk_multiplier": self._risk_multiplier,
                        "core4h": self._core_status,
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                )
                # Pick up the latest market stance produced by the scheduler.
                async with session_scope() as session:
                    review = await repo.latest_llm_review(session, "market")
                    if review is not None:
                        age = (
                            dt.datetime.now(dt.timezone.utc)
                            - (
                                review.created_at.replace(tzinfo=dt.timezone.utc)
                                if review.created_at.tzinfo is None
                                else review.created_at
                            )
                        ).total_seconds() / 60
                        if age < settings.llm_review_minutes * 3:
                            self._market_stance = review.stance
                            # The advisor may only stand the book down; the
                            # validated sizing is the ceiling (investment policy).
                            self._risk_multiplier = min(1.0, float(review.risk_multiplier or 1.0))
                        else:
                            self._market_stance = "neutral"
                            self._risk_multiplier = 1.0
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat failed: %s", exc)
            await self._sleep(20)

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
            self._stop.set()
            self._core_wake.set()
            # A portfolio evaluation that is mid-order must finish before the
            # HTTP client goes away, or a lost response re-issues the order
            # after the restart. The lock is never released: nothing may
            # start once we are here.
            try:
                await asyncio.wait_for(self._core_eval_lock.acquire(), timeout=30.0)
                # Released again: the clock and the command loop both check
                # `_stop` before they take it, and holding it would deadlock a
                # tick that is already waiting for it.
                self._core_eval_lock.release()
            except asyncio.TimeoutError:
                log.warning("shutting down with a portfolio evaluation still running")
            # Tell the user before the connections go: a stopped engine is
            # a floor of exchange-side stops only, and they should know.
            try:
                async with session_scope() as session:
                    open_count = len(await repo.open_positions(session))
                    await repo.log_event(
                        session, "Trading engine shutting down", level="warn",
                        source="engine", data={"open_positions": open_count}, notify=False,
                    )
                await asyncio.wait_for(
                    notify.send(notify.Notification(
                        "system", "warn", "موتور معامله‌گر خاموش شد",
                        f"سرویس engine متوقف شد (ری‌استارت یا توقف دستی). {open_count} پوزیشن باز "
                        "فقط با استاپ صرافی محافظت می‌شود تا موتور دوباره بالا بیاید.",
                    )),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001 - shutdown must finish regardless
                log.debug("shutdown notification skipped: %s", exc)
            await notify.flush(timeout=3.0)
            await self.client.aclose()
            await bus.close_redis()


def _core_contributions(signal: core4h.Signal, view: core4h.Universe) -> dict:
    """The signal row's `contributions`: one entry per rule, the panel's bar format.

    Scores are in -1..1 like ensemble votes so the signals page renders them
    unchanged; `family` names the strategy (its `reason`) and is what the
    panel shows as the family badge.
    """
    d = signal.detail
    regime_reason = (
        f"BTC 4h close {'above' if view.regime_ok else 'below'} EMA200 "
        f"({view.market_close:.2f} vs {view.market_ema200:.2f}), "
        f"EMA200 {'rising' if view.regime_rising else 'not rising'} over 24h"
    )
    out = {
        "family": {"score": 1.0, "weight": 1.0, "reason": signal.family},
        "btc_regime": {"score": 1.0 if view.regime_ok else -1.0, "weight": 1.0, "reason": regime_reason},
    }
    if signal.family == core4h.MOMENTUM.name:
        rank, n = d.get("rank"), max(2, int(d.get("universe") or 2))
        rank_score = 0.0 if rank is None else round(1.0 - 2.0 * (rank - 1) / (n - 1), 3)
        momentum = d.get("momentum_pct")
        out["momentum_rank"] = {
            "score": max(-1.0, min(1.0, rank_score)),
            "weight": 1.0,
            "reason": (
                f"rank {rank:.0f} of {n}" if rank is not None else "no rank yet"
            ) + (f", 7d-1d return {momentum:+.2f}%" if momentum is not None else "")
            + (" (rebalance bar)" if d.get("rebalance") else " (not a rebalance bar)"),
        }
    else:
        breakout = d.get("breakout_atr")
        out["breakout"] = {
            "score": 0.0 if breakout is None else max(-1.0, min(1.0, round(breakout, 3))),
            "weight": 1.0,
            "reason": (
                f"close {breakout:+.2f} ATR vs the previous 55-bar high {d.get('entry_high'):.6g}"
                if breakout is not None and d.get("entry_high") is not None
                else "55-bar channel not formed yet"
            ),
        }
        out["trend_filter"] = {
            "score": 1.0 if d.get("above_ema200") else -1.0,
            "weight": 1.0,
            "reason": f"close {'above' if d.get('above_ema200') else 'below'} own EMA200, ADX {d.get('adx', 0):.1f}",
        }
        if d.get("exit_low") is not None:
            out["channel_exit"] = {
                "score": 0.0,
                "weight": 1.0,
                "reason": f"exit below the previous 20-bar low {d['exit_low']:.6g}",
            }
    return out


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _seconds_since(iso: str) -> float:
    try:
        then = dt.datetime.fromisoformat(iso)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - then).total_seconds()


async def main() -> None:
    engine = TradingEngine()
    loop = asyncio.get_running_loop()
    shutdown_task: asyncio.Task | None = None

    def _on_signal() -> None:
        nonlocal shutdown_task
        # Keep a reference: a bare create_task() can be garbage collected.
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(engine.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass
    try:
        await engine.start()
    finally:
        await engine.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
