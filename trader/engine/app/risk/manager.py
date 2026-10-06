"""Risk manager - the last gate before any order is sent.

Everything that can stop a trade lives here, so there is exactly one place to
audit when asking "why did (or didn't) it trade?".

Layers, in the order they are checked:
  1. Kill switch / armed state / trading mode
  2. Account-level drawdown (daily and total)
  3. Concurrency and per-symbol exposure
  4. Cooldown after a loss
  5. Position sizing from ATR stop distance
  6. Exchange filters (min notional, lot size)

What the max-drawdown kill switch measures depends on the strategy core:

  * ensemble30m - mark-to-market equity against its flow-adjusted peak, as
    the panel shows it.
  * portfolio4h - CLOSED equity (cash plus realised PnL, i.e. equity minus
    open profit) against the peak of closed equity, which is scaled - never
    shifted - by a deposit or withdrawal, so the reading follows trading and
    not the size of the account. The channel family holds
    winners for weeks with no target; its open profit routinely gives back
    20-30% of a peak before the channel exit fires, and in the study the
    mark-to-market rule stopped the engine in the middle of its best windows.
    A realised loss still
    trips it. The daily-loss pause stays mark-to-market for both cores.

Per-family sizing (risk %, position cap, cooldown, no target) is passed by
the caller as `EntryOverrides`; the account-level gates are shared.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo
from app.db.models import Position
from app.exchange.symbols import SymbolInfo
from app.strategies import core4h
from app.strategies.ensemble import Decision

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryOverrides:
    """Per-call sizing rules; None falls back to the configured setting.

    `require_target=False` is for families that run without a take-profit
    (the fee-hurdle check compares the target with the round-trip fee, and
    there is no target to compare).
    """

    risk_pct: float | None = None
    max_position_pct: float | None = None
    cooldown_minutes: int | None = None
    require_target: bool = True
    # The family opening the position (None for the ensemble core). With it
    # the cooldown counts only that family's losses and the per-family cap
    # below applies on top of the account-wide one.
    family: str | None = None
    max_family_positions: int | None = None

    @classmethod
    def for_family(cls, family: core4h.Family) -> "EntryOverrides":
        return cls(
            risk_pct=family.risk_pct,
            max_position_pct=family.max_position_pct,
            cooldown_minutes=family.cooldown_min,
            require_target=False,
            family=family.name,
            max_family_positions=family.max_positions,
        )


def position_family(position: Position) -> str:
    """Which family opened the position ("ensemble" for the old core)."""
    return str((position.meta or {}).get("strategy") or "ensemble")


def is_core4h_position(position: Position) -> bool:
    return position_family(position) in core4h.FAMILIES


@dataclass
class RiskVerdict:
    approved: bool
    reason: str = ""
    qty: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    size_multiplier: float = 1.0


@dataclass
class AccountState:
    equity: float
    free_balance: float
    peak_equity: float
    open_positions: int
    day_start_equity: float
    day_net_flow: float = 0.0  # deposits - withdrawals since 00:00 UTC
    closed_equity: float = 0.0       # equity minus open profit
    # Best closed equity seen, as a level in current capital terms. A capital
    # flow scales it in proportion (repo.rescale_closed_peak), so the drawdown
    # ratio below answers to trading results and not to deposits.
    closed_peak_equity: float = 0.0
    # Money on (or gone from) the exchange that the capital ledger does not
    # explain yet: a deposit or withdrawal awaiting classification (signed).
    # Non-zero makes the reading `suspect` - the peaks were not raised from
    # it and the flow-sensitive gates stand down.
    unexplained_balance: float = 0.0
    # False when the valuation behind this reading could not see every wallet
    # (a failed Simple Earn read). The equity is then short by an unknown
    # amount, which is not the same as being short by a known flow.
    balances_complete: bool = True

    @property
    def suspect(self) -> bool:
        return self.unexplained_balance != 0.0 or not self.balances_complete

    @property
    def attributable_equity(self) -> float:
        """Equity in the same terms as the ledger: the unclassified money removed."""
        return self.equity - self.unexplained_balance

    @property
    def attributable_closed_equity(self) -> float:
        return self.closed_equity - self.unexplained_balance

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(
            0.0, (self.peak_equity - self.attributable_equity) / self.peak_equity * 100
        )

    @property
    def closed_drawdown_pct(self) -> float:
        if self.closed_peak_equity <= 0:
            return 0.0
        return max(
            0.0,
            (self.closed_peak_equity - self.attributable_closed_equity)
            / self.closed_peak_equity
            * 100,
        )

    @property
    def kill_drawdown_pct(self) -> float:
        """The drawdown the kill switch acts on for the configured core."""
        return self.closed_drawdown_pct if settings.core_is_portfolio4h else self.drawdown_pct

    @property
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        # a deposit made today is not profit, a withdrawal is not a loss
        return (
            (self.attributable_equity - self.day_net_flow - self.day_start_equity)
            / self.day_start_equity
            * 100
        )


class RiskManager:
    """Stateless with respect to the caller: every check reads current state."""

    def __init__(self) -> None:
        # The stand-down of the flow-sensitive gates is logged once per
        # episode, not on every evaluation while a flow is in flight.
        self._suspect_logged = False

    async def account_state(
        self,
        session: AsyncSession,
        equity: float,
        free_balance: float,
        marks: dict[str, float] | None = None,
        quote_balance: float | None = None,
        balances_complete: bool = True,
    ) -> AccountState:
        # Same formula as api.metrics.performance_summary, so the drawdown the
        # panel shows is the one the kill switch acts on. `peak_equity` is
        # flow-adjusted: a withdrawal lowers it instead of reading as a loss.
        base = await repo.capital_base(session)
        if base <= 0:
            base = settings.initial_capital_usdt
        open_count = len(await repo.open_positions(session))

        # Closed equity: strip the open profit at the current marks. Without
        # marks every open position is valued at its entry, which is what a
        # "nothing is open" valuation means anyway.
        unrealized = await repo.unrealized_pnl(session, marks or {})
        closed_equity = equity - unrealized

        # A reading taken with an unclassified deposit or withdrawal on the
        # exchange may not raise either peak: `peaks_for_reading` measures it
        # against the peaks as they stand instead.
        unexplained = await repo.unexplained_balance(session, quote_balance)
        # A reading taken without a trusted Simple Earn book is missing an
        # unknown part of the account, so it may not raise a peak either.
        suspect = unexplained != 0.0 or not balances_complete
        peak, closed_peak = await repo.peaks_for_reading(
            session, equity=equity, closed_equity=closed_equity, suspect=suspect
        )
        peak = max(peak, base)

        midnight = dt.datetime.now(dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        first_today, day_flow = await repo.day_open(session, midnight)
        # With no clean snapshot yet today, the day opened at the current
        # attributable equity minus whatever was added (or plus whatever was
        # taken) since midnight.
        day_start = (
            first_today.equity
            if first_today is not None
            else max(0.0, equity - unexplained - day_flow)
        )
        return AccountState(
            equity=equity,
            free_balance=free_balance,
            peak_equity=peak,
            open_positions=open_count,
            day_start_equity=day_start,
            day_net_flow=day_flow,
            closed_equity=closed_equity,
            closed_peak_equity=closed_peak,
            unexplained_balance=unexplained,
            balances_complete=balances_complete,
        )

    # ------------------------------------------------------------------
    async def check_global(
        self, session: AsyncSession, state: AccountState
    ) -> tuple[bool, str]:
        """Account-wide gates that stop all new entries."""
        kill = await repo.get_setting(session, "kill_switch", False)
        if kill:
            return False, "kill switch engaged"

        armed = await repo.get_setting(session, "trading_armed", settings.trading_armed)
        if settings.trading_mode == "live" and not armed:
            return False, "live trading not armed"

        paused_until = await repo.get_setting(session, "paused_until", None)
        if paused_until:
            try:
                until = dt.datetime.fromisoformat(paused_until)
                if until > dt.datetime.now(dt.timezone.utc):
                    return False, f"paused until {until:%Y-%m-%d %H:%M} UTC"
            except ValueError:
                pass

        if state.suspect:
            # A deposit or withdrawal is on the exchange but not in the ledger
            # yet: the drawdown and daily-loss figures are off by exactly its
            # size until the reconciler records it, so neither gate may act
            # on them (an unrecorded deposit would otherwise read as a
            # drawdown and fire the kill switch). The remaining gates still apply.
            if not self._suspect_logged:
                if not state.balances_complete:
                    log.warning(
                        "drawdown and daily-loss gates stand down: the account "
                        "could not be valued in full (Simple Earn unreadable)"
                    )
                else:
                    log.warning(
                        "drawdown and daily-loss gates stand down: unexplained balance "
                        "%+.2f %s awaiting classification as a capital flow",
                        state.unexplained_balance, settings.quote_asset,
                    )
                self._suspect_logged = True
            return await self._check_capacity(state)
        self._suspect_logged = False

        drawdown = state.kill_drawdown_pct
        if drawdown >= settings.max_drawdown_pct:
            basis = "closed-equity" if settings.core_is_portfolio4h else "equity"
            await repo.set_setting(session, "kill_switch", True)
            await repo.log_event(
                session,
                f"Kill switch: {basis} drawdown {drawdown:.1f}% reached the "
                f"{settings.max_drawdown_pct}% limit. Manual re-arm required.",
                level="error",
                source="risk",
                data={
                    "equity": state.equity,
                    "closed_equity": state.closed_equity,
                    "peak_equity": state.peak_equity,
                    "closed_peak_equity": state.closed_peak_equity,
                },
            )
            return False, f"max {basis} drawdown {drawdown:.1f}% reached"

        if state.daily_pnl_pct <= -settings.max_daily_loss_pct:
            tomorrow = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
            ).replace(hour=0, minute=0, second=0, microsecond=0)
            await repo.set_setting(session, "paused_until", tomorrow.isoformat())
            await repo.log_event(
                session,
                f"Daily loss limit hit ({state.daily_pnl_pct:.2f}%). "
                f"Trading paused until {tomorrow:%Y-%m-%d} UTC.",
                level="warn",
                source="risk",
            )
            return False, f"daily loss {state.daily_pnl_pct:.2f}% hit the limit"

        return await self._check_capacity(state)

    @staticmethod
    async def _check_capacity(state: AccountState) -> tuple[bool, str]:
        """The gates that do not depend on where a peak or a day started."""
        if state.open_positions >= settings.max_concurrent_positions:
            return False, (
                f"{state.open_positions} open positions "
                f"(max {settings.max_concurrent_positions})"
            )

        min_equity = max(settings.initial_capital_usdt * 0.15, 12.0)
        if state.equity < min_equity:
            return False, f"equity {state.equity:.2f} below working minimum {min_equity:.2f}"

        return True, ""

    # ------------------------------------------------------------------
    async def evaluate_entry(
        self,
        session: AsyncSession,
        decision: Decision,
        state: AccountState,
        symbol_info: SymbolInfo,
        size_multiplier: float = 1.0,
        overrides: EntryOverrides | None = None,
    ) -> RiskVerdict:
        """Size a position, or explain why it cannot be opened."""
        rules = overrides or EntryOverrides()
        risk_pct = settings.risk_per_trade_pct if rules.risk_pct is None else rules.risk_pct
        position_cap = (
            settings.max_position_pct if rules.max_position_pct is None else rules.max_position_pct
        )
        cooldown_minutes = (
            settings.cooldown_minutes_after_loss
            if rules.cooldown_minutes is None
            else rules.cooldown_minutes
        )

        # "Already holding" first: a signal for a coin already in the book is
        # not an entry attempt, so it must not read as a capacity refusal in
        # the signal log (and needs none of the account gates evaluated).
        existing = await repo.open_position_for(session, decision.symbol)
        if existing is not None:
            return RiskVerdict(False, f"already holding {decision.symbol}")

        ok, reason = await self.check_global(session, state)
        if not ok:
            return RiskVerdict(False, reason)

        if rules.family and rules.max_family_positions:
            held = await repo.open_positions_count(session, family=rules.family)
            if held >= rules.max_family_positions:
                return RiskVerdict(
                    False,
                    f"{held} {rules.family} positions open "
                    f"(family max {rules.max_family_positions})",
                )

        cooldown = await self._cooldown_remaining(
            session, decision.symbol, cooldown_minutes, rules.family
        )
        if cooldown:
            return RiskVerdict(False, f"cooldown for another {cooldown} min")

        blocked = self._session_blocked()
        if blocked:
            return RiskVerdict(False, blocked)

        if decision.side == "short" and not (settings.futures_enabled and settings.exchange == "binance"):
            # Only Binance futures can short; on a spot-only exchange a "short"
            # would sell coins the user holds outside the bot.
            return RiskVerdict(False, "shorts require Binance futures, which is disabled")

        stop_distance = abs(decision.price - decision.stop_price)
        if stop_distance <= 0:
            return RiskVerdict(False, "invalid stop distance")

        # --- position size from risk, not from a fixed fraction of equity ---
        multiplier = max(0.25, min(1.5, size_multiplier))
        risk_amount = state.equity * (risk_pct / 100) * multiplier
        raw_qty = risk_amount / stop_distance

        leverage = settings.futures_leverage if decision.side == "short" else 1.0
        max_notional = min(
            state.equity * (position_cap / 100) * leverage,
            state.free_balance * 0.98 * leverage,
        )
        if raw_qty * decision.price > max_notional:
            raw_qty = max_notional / decision.price

        qty = symbol_info.round_qty(raw_qty)
        notional = qty * decision.price

        tradable, why = symbol_info.is_tradable(qty, decision.price)
        if not tradable:
            # Try once at the exchange minimum - but only if the extra risk is
            # still inside the per-trade budget.
            # Round *up*: flooring lands below min_notional whenever one lot
            # step is worth more than the 2% buffer (BTC at a 5 USDT minimum).
            min_qty = symbol_info.ceil_qty(
                max(symbol_info.min_qty, symbol_info.min_notional * 1.02 / decision.price)
            )
            ok_min, why_min = symbol_info.is_tradable(min_qty, decision.price)
            if not ok_min:
                return RiskVerdict(False, f"cannot size at exchange minimum: {why_min}")
            implied_risk = min_qty * stop_distance
            budget = state.equity * (risk_pct / 100) * 1.6
            if implied_risk <= budget and min_qty * decision.price <= max_notional:
                qty = min_qty
                notional = qty * decision.price
                risk_amount = implied_risk
            else:
                return RiskVerdict(False, f"cannot size within risk budget: {why}")

        # Fees must not eat the edge. A family without a take-profit has no
        # target to hold against the fee; its exit is a rule, not a price.
        if rules.require_target:
            fee_cost = notional * settings.round_trip_fee_pct / 100
            gross_target = abs(decision.target_price - decision.price) * qty
            if gross_target <= fee_cost * 2.5:
                return RiskVerdict(
                    False,
                    f"target {gross_target:.3f} USDT vs fees {fee_cost:.3f} USDT - not worth it",
                )

        return RiskVerdict(
            approved=True,
            qty=qty,
            notional=notional,
            risk_amount=risk_amount,
            stop_price=symbol_info.round_price(decision.stop_price),
            target_price=(
                symbol_info.round_price(decision.target_price) if decision.target_price else 0.0
            ),
            size_multiplier=multiplier,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _session_blocked(now: dt.datetime | None = None) -> str:
        """Weekend and dead-hour entries lost in every regime window tested.

        Exits are never blocked - this only stops new positions.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        if settings.skip_weekend_entries and now.weekday() >= 5:
            return "no new entries on weekends (UTC)"
        if settings.skip_entry_hours_utc and now.hour in settings.skip_entry_hours_utc:
            return f"no new entries at {now.hour:02d}:xx UTC"
        return ""

    @staticmethod
    async def _cooldown_remaining(
        session: AsyncSession,
        symbol: str,
        minutes: int | None = None,
        family: str | None = None,
    ) -> int:
        """Minutes left of the cooldown after a loss on `symbol`.

        The 4h families were validated one at a time, each with its own
        loss history, so a family's cooldown only counts that family's
        losses: a momentum exit at a loss does not block the turtle entry
        the simulator would have taken on the same bar.
        """
        minutes = settings.cooldown_minutes_after_loss if minutes is None else minutes
        if minutes <= 0:
            return 0
        last_loss = await repo.last_loss_time(session, symbol, family=family)
        if last_loss is None:
            return 0
        if last_loss.tzinfo is None:
            last_loss = last_loss.replace(tzinfo=dt.timezone.utc)
        elapsed = (dt.datetime.now(dt.timezone.utc) - last_loss).total_seconds() / 60
        remaining = minutes - elapsed
        return int(remaining) if remaining > 0 else 0

    # ------------------------------------------------------------------
    @staticmethod
    def should_exit(position: Position, price: float, atr: float) -> tuple[bool, str]:
        """Stop, target and trailing checks for an open position.

        A 4h-core position only has its hard stop here: its target-less exits
        (rank, channel, BTC regime) are decided on the closed 4h bar by the
        portfolio evaluation, and the 48-hour "went nowhere" time stop would
        cut channel trades that are meant to sit for weeks.
        """
        long = position.side == "long"

        if long and price <= position.stop_price:
            return True, "stop_loss"
        if not long and price >= position.stop_price:
            return True, "stop_loss"

        if is_core4h_position(position):
            return False, ""

        if position.target_price:
            if long and price >= position.target_price:
                return True, "take_profit"
            if not long and price <= position.target_price:
                return True, "take_profit"

        # Time stop: a swing trade that has gone nowhere is dead money.
        age_hours = (
            dt.datetime.now(dt.timezone.utc)
            - (
                position.opened_at.replace(tzinfo=dt.timezone.utc)
                if position.opened_at.tzinfo is None
                else position.opened_at
            )
        ).total_seconds() / 3600
        if age_hours > 48:
            move = abs(price - position.entry_price) / position.entry_price * 100
            if move < settings.round_trip_fee_pct * 2:
                return True, "time_stop"

        return False, ""

    @staticmethod
    def update_trailing(position: Position, price: float, atr: float) -> bool:
        """Move the stop up (or down for shorts). Returns True if it changed."""
        if not settings.trailing_enabled or atr <= 0:
            return False
        if is_core4h_position(position):
            # Neither 4h family trails: the study's numbers are for a fixed
            # initial stop and a rule-based exit.
            return False

        initial_risk = abs(position.entry_price - position.initial_stop)
        if initial_risk <= 0:
            return False

        long = position.side == "long"
        gain = (price - position.entry_price) if long else (position.entry_price - price)
        r_multiple = gain / initial_risk

        if r_multiple < settings.trailing_activate_r:
            return False

        if long:
            position.high_water = max(position.high_water or price, price)
            new_stop = position.high_water - atr * settings.trailing_atr_mult
            # never trail below break-even once activated
            new_stop = max(new_stop, position.entry_price * 1.0005)
            if new_stop > position.stop_price:
                position.stop_price = new_stop
                position.trail_active = True
                return True
        else:
            position.high_water = min(position.high_water or price, price)
            new_stop = position.high_water + atr * settings.trailing_atr_mult
            new_stop = min(new_stop, position.entry_price * 0.9995)
            if new_stop < position.stop_price:
                position.stop_price = new_stop
                position.trail_active = True
                return True

        return False
