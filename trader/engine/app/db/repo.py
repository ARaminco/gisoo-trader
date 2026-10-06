"""Data-access helpers. Everything the workers and API need from the database."""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Sequence

import pandas as pd
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Candle, CapitalFlow, CommitteeMeeting, EquitySnapshot, EventLog, ExchangeSync,
    LLMReview, MLModel, Order, Passkey, Position, PushSubscription, Setting, Signal,
    utcnow,
)

# --------------------------------------------------------------------------
# candles
# --------------------------------------------------------------------------


async def upsert_candles(session: AsyncSession, rows: Sequence[dict], *, overwrite: bool = True) -> int:
    """Insert candles, overwriting any that already exist unless `overwrite=False`.

    Only closed candles are ever stored (see `data.ingest`), so an overwrite is
    a re-fetch of the same bar, never a partial one. History backfill does not
    overwrite: a connector may serve older bars from a coarser source (Nobitex
    builds them from shifted 4h candles) and must not replace exact ones.
    """
    if not rows:
        return 0
    stmt = pg_insert(Candle).values(list(rows))
    if not overwrite:
        await session.execute(stmt.on_conflict_do_nothing(
            index_elements=[Candle.symbol, Candle.timeframe, Candle.open_time]
        ))
        return len(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Candle.symbol, Candle.timeframe, Candle.open_time],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "quote_volume": stmt.excluded.quote_volume,
            "trades": stmt.excluded.trades,
            "taker_buy_base": stmt.excluded.taker_buy_base,
        },
    )
    await session.execute(stmt)
    return len(rows)


async def load_candles(
    session: AsyncSession, symbol: str, timeframe: str, limit: int = 500
) -> pd.DataFrame:
    """Most recent `limit` candles as a DataFrame, oldest first."""
    stmt = (
        select(
            Candle.open_time, Candle.open, Candle.high, Candle.low,
            Candle.close, Candle.volume, Candle.quote_volume,
            Candle.trades, Candle.taker_buy_base,
        )
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()
    if not rows:
        return pd.DataFrame(
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "quote_volume", "trades", "taker_buy_base",
            ]
        )
    df = pd.DataFrame(rows, columns=list(rows[0]._fields))
    df = df.iloc[::-1].reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


async def last_candle_time(
    session: AsyncSession, symbol: str, timeframe: str
) -> int | None:
    stmt = select(func.max(Candle.open_time)).where(
        Candle.symbol == symbol, Candle.timeframe == timeframe
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def prune_candles(
    session: AsyncSession, symbol: str, timeframe: str, keep: int
) -> None:
    """Drop candles older than the newest `keep` so the table stays small."""
    cutoff_stmt = (
        select(Candle.open_time)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .offset(keep)
        .limit(1)
    )
    cutoff = (await session.execute(cutoff_stmt)).scalar_one_or_none()
    if cutoff is not None:
        await session.execute(
            delete(Candle).where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time < cutoff,
            )
        )


# --------------------------------------------------------------------------
# positions & orders
# --------------------------------------------------------------------------


async def open_positions(session: AsyncSession, mode: str | None = None) -> list[Position]:
    stmt = select(Position).where(Position.status == "open").order_by(Position.opened_at)
    if mode:
        stmt = stmt.where(Position.mode == mode)
    return list((await session.execute(stmt)).scalars().all())


async def unresolved_positions(session: AsyncSession) -> list[Position]:
    """Open rows plus legacy "error" rows: everything reconciliation must still settle."""
    stmt = (
        select(Position)
        .where(Position.status.in_(("open", "error")))
        .order_by(Position.opened_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def open_position_for(session: AsyncSession, symbol: str) -> Position | None:
    stmt = select(Position).where(Position.status == "open", Position.symbol == symbol)
    return (await session.execute(stmt)).scalars().first()


async def open_positions_count(session: AsyncSession, family: str | None = None) -> int:
    """Open positions, optionally only those opened by one strategy family."""
    stmt = select(func.count()).select_from(Position).where(Position.status == "open")
    if family:
        stmt = stmt.where(Position.meta["strategy"].as_string() == family)
    return int((await session.execute(stmt)).scalar_one())


async def open_positions_with_pending_exit(session: AsyncSession) -> list[Position]:
    """Open rows whose rule exit could not be executed yet (see trader._close_position)."""
    stmt = (
        select(Position)
        .where(Position.status == "open", Position.pending_exit_reason.is_not(None))
        .order_by(Position.opened_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def open_positions_missing_stop(session: AsyncSession) -> list[Position]:
    """Open rows whose exchange-side protective stop has to be (re-)placed."""
    stmt = (
        select(Position)
        .where(Position.status == "open", Position.stop_missing.is_(True))
        .order_by(Position.opened_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def open_positions_with_deferred_exit(session: AsyncSession) -> list[Position]:
    """Open rows carrying a bar whose exit rule could not be read yet."""
    stmt = (
        select(Position)
        .where(Position.status == "open", Position.exit_check_bar.is_not(None))
        .order_by(Position.opened_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def recent_positions(session: AsyncSession, limit: int = 100) -> list[Position]:
    stmt = select(Position).order_by(Position.opened_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def closed_positions(
    session: AsyncSession, since: dt.datetime | None = None
) -> list[Position]:
    """Closed round trips, filtered in SQL rather than after hydrating every row."""
    stmt = select(Position).where(Position.status == "closed")
    if since is not None:
        stmt = stmt.where(Position.closed_at >= since)
    result = await session.execute(stmt.order_by(Position.closed_at))
    return list(result.scalars().all())


async def closed_positions_since(
    session: AsyncSession, since: dt.datetime
) -> list[Position]:
    return await closed_positions(session, since)


async def realized_pnl_total(
    session: AsyncSession,
    mode: str | None = None,
    exclude_mode: str | None = None,
    since: dt.datetime | None = None,
) -> float:
    """Sum of realized PnL over closed positions, aggregated in the database."""
    stmt = select(func.coalesce(func.sum(Position.realized_pnl), 0.0)).where(
        Position.status == "closed"
    )
    if mode:
        stmt = stmt.where(Position.mode == mode)
    if exclude_mode:
        stmt = stmt.where(Position.mode != exclude_mode)
    if since is not None:
        stmt = stmt.where(Position.closed_at >= since)
    return float((await session.execute(stmt)).scalar_one())


async def open_notional(session: AsyncSession, exclude_mode: str | None = None) -> float:
    """Entry value locked in positions the exchange still holds (open or unsettled)."""
    stmt = select(
        func.coalesce(func.sum(Position.entry_price * Position.qty), 0.0)
    ).where(Position.status.in_(("open", "error")))
    if exclude_mode:
        stmt = stmt.where(Position.mode != exclude_mode)
    return float((await session.execute(stmt)).scalar_one())


async def last_loss_time(
    session: AsyncSession, symbol: str, family: str | None = None
) -> dt.datetime | None:
    """When `symbol` last closed at a loss; with `family`, only that family's trades."""
    stmt = (
        select(Position.closed_at)
        .where(
            Position.symbol == symbol,
            Position.status == "closed",
            Position.realized_pnl < 0,
        )
        .order_by(Position.closed_at.desc())
        .limit(1)
    )
    if family:
        stmt = stmt.where(Position.meta["strategy"].as_string() == family)
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_order(session: AsyncSession, **kwargs: Any) -> Order:
    order = Order(**kwargs)
    session.add(order)
    await session.flush()
    return order


async def update_order_status(
    session: AsyncSession,
    exchange_order_id: str | int | None,
    status: str,
    filled_qty: float | None = None,
    avg_price: float | None = None,
    fee: float | None = None,
) -> Order | None:
    """Reflect what the exchange did with an order we placed (cancel, fill).

    Without this the protective-stop rows would stay NEW forever, so the
    orders table would show live-looking stops for positions long closed.
    """
    if not exchange_order_id:
        return None
    stmt = select(Order).where(Order.exchange_order_id == str(exchange_order_id))
    order = (await session.execute(stmt)).scalars().first()
    if order is None:
        return None
    order.status = status
    if filled_qty is not None:
        order.filled_qty = filled_qty
    if avg_price is not None:
        order.avg_price = avg_price
    if fee is not None:
        order.fee = fee
    await session.flush()
    return order


async def recent_orders(session: AsyncSession, limit: int = 100) -> list[Order]:
    stmt = select(Order).order_by(Order.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------


async def record_signal(session: AsyncSession, **kwargs: Any) -> Signal:
    signal = Signal(**kwargs)
    session.add(signal)
    await session.flush()
    return signal


async def recent_signals(session: AsyncSession, limit: int = 60) -> list[Signal]:
    stmt = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# equity
# --------------------------------------------------------------------------


async def add_equity_snapshot(session: AsyncSession, **kwargs: Any) -> EquitySnapshot:
    snap = EquitySnapshot(**kwargs)
    session.add(snap)
    await session.flush()
    return snap


async def latest_equity(
    session: AsyncSession, clean_only: bool = False
) -> EquitySnapshot | None:
    """Most recent snapshot; `clean_only` skips readings taken with a flow in flight."""
    stmt = select(EquitySnapshot).order_by(EquitySnapshot.taken_at.desc()).limit(1)
    if clean_only:
        stmt = stmt.where(EquitySnapshot.suspect.is_not(True))
    return (await session.execute(stmt)).scalars().first()


async def peak_equity(session: AsyncSession) -> float:
    """Flow-adjusted peak equity.

    Each snapshot is restated in *current* capital terms by adding the net
    deposits/withdrawals that happened after it, so a withdrawal lowers the
    peak and a deposit raises it instead of being read as PnL. The seeded
    "initial" flow is the baseline the snapshots already contain, not a later
    movement, so it is left out (a snapshot taken moments before the seed row
    would otherwise be doubled).

    Suspect snapshots are left out too. They were taken while the money was
    already on the exchange but not yet in `capital_flows`, so their equity
    contains a flow the ledger attributes to a later moment; restating them
    adds it a second time, and a large deposit then reads as a drawdown big
    enough to fire the kill switch on a profitable account.
    """
    flows = (
        await session.execute(
            select(CapitalFlow.occurred_at, CapitalFlow.kind, CapitalFlow.amount)
            .where(CapitalFlow.source != "initial")
            .order_by(CapitalFlow.occurred_at)
        )
    ).all()
    signed = [(t, a if k == "deposit" else -a) for t, k, a in flows]
    bounds: list[dt.datetime | None] = [None, *[t for t, _ in signed], None]
    peak = 0.0
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        stmt = select(func.max(EquitySnapshot.equity)).where(
            EquitySnapshot.suspect.is_not(True)
        )
        if lo is not None:
            stmt = stmt.where(EquitySnapshot.taken_at > lo)
        if hi is not None:
            stmt = stmt.where(EquitySnapshot.taken_at <= hi)
        segment_peak = (await session.execute(stmt)).scalar_one_or_none()
        if segment_peak is None:
            continue
        later = 0.0 if hi is None else sum(v for t, v in signed if t >= hi)
        peak = max(peak, float(segment_peak) + later)
    return peak


CLOSED_PNL_PEAK_KEY = "closed_pnl_peak"
# The closed-equity high-water mark, stored as a *level* in current capital
# terms. See `closed_peak_equity` for why it is not kept as PnL over the base.
CLOSED_PEAK_KEY = "closed_peak_equity"


async def unrealized_pnl(session: AsyncSession, marks: dict[str, float]) -> float:
    """Open profit at the current marks, signed, over every open position."""
    total = 0.0
    for position in await open_positions(session):
        mark = marks.get(position.symbol, position.entry_price)
        direction = 1 if position.side == "long" else -1
        total += (mark - position.entry_price) * position.qty * direction
    return total


async def closed_pnl_peak(session: AsyncSession) -> float:
    """Highest closed PnL over the capital base seen so far (0 if none).

    Closed equity is cash plus realised PnL: what the account is worth if
    nothing were open. The peak is kept as PnL *over the capital base*, so a
    deposit or withdrawal moves the peak and the current value together and
    never reads as a drawdown - the same flow adjustment `peak_equity` makes
    for the mark-to-market curve, without a new table.
    """
    value = await get_setting(session, CLOSED_PNL_PEAK_KEY, 0.0)
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


async def update_closed_pnl_peak(session: AsyncSession, closed_pnl: float) -> float:
    """Raise the stored peak if `closed_pnl` beats it. Returns the peak.

    Callers must not pass a suspect reading: go through `peaks_for_reading`,
    the one place a reading is allowed to become a peak.
    """
    peak = await closed_pnl_peak(session)
    if closed_pnl > peak:
        await set_setting(session, CLOSED_PNL_PEAK_KEY, float(closed_pnl))
        return float(closed_pnl)
    return peak


async def closed_peak_equity(session: AsyncSession) -> float:
    """Closed-equity high-water mark as a level, in current capital terms.

    The kill switch acts on `(peak - closed_equity) / peak`. Keeping the peak
    as PnL over the capital base - `base + closed_pnl_peak` - makes that ratio
    depend on the base: a flow moves numerator and denominator by different
    amounts, because the numerator is a since-inception result in *dollars*
    and only the denominator follows the base.

    Withdrawing capital therefore inflates the reading with no new loss: a 2%
    result reads as 30% once most of the base is withdrawn (or misread as
    withdrawn, as Simple Earn sweeps were), and trips a 20% kill switch.

    So the peak is a level, and `rescale_closed_peak` scales it by the same
    proportion a flow changes closed equity. The ratio then survives any
    deposit or withdrawal and moves only on trading results.

    Seeds itself from the old representation the first time it is read, so the
    reading does not jump when this lands.
    """
    stored = await get_setting(session, CLOSED_PEAK_KEY, None)
    if stored is not None:
        try:
            return max(0.0, float(stored))
        except (TypeError, ValueError):
            pass
    seed = await capital_base(session) + await closed_pnl_peak(session)
    await set_setting(session, CLOSED_PEAK_KEY, float(seed))
    return float(seed)


async def update_closed_peak(session: AsyncSession, closed_equity: float) -> float:
    """Raise the closed-equity peak if this reading beats it. Returns the peak.

    Callers must not pass a suspect reading: go through `peaks_for_reading`.
    """
    peak = await closed_peak_equity(session)
    if closed_equity > peak:
        await set_setting(session, CLOSED_PEAK_KEY, float(closed_equity))
        return float(closed_equity)
    return peak


async def rescale_closed_peak(
    session: AsyncSession, closed_before: float, amount: float
) -> float:
    """Scale the closed-equity peak by what a capital flow does to closed equity.

    `amount` is signed: positive for a deposit, negative for a withdrawal.
    Adding it instead would keep the peak level right but the *ratio* wrong,
    which is the whole point of holding the peak as a level.

    A flow that cannot be expressed as a proportion - no closed equity to
    scale from, or one that takes the account to zero - leaves the peak alone
    rather than guessing.
    """
    peak = await closed_peak_equity(session)
    if closed_before <= 0 or peak <= 0:
        return peak
    closed_after = closed_before + amount
    if closed_after <= 0:
        return peak
    scaled = peak * (closed_after / closed_before)
    await set_setting(session, CLOSED_PEAK_KEY, float(scaled))
    return float(scaled)


async def peaks_for_reading(
    session: AsyncSession, *, equity: float, closed_equity: float, suspect: bool
) -> tuple[float, float]:
    """(mark-to-market peak, closed-equity peak) for one account reading.

    Both are levels in current capital terms, so a caller divides by them
    directly. An attributable reading may raise both. A suspect one - taken
    while the exchange held money the ledger has not classified, or while a
    wallet could not be read - may raise neither: it is measured against the
    peaks as they stand and nothing is stored. The equity loop and the risk
    manager both come through here, so there is exactly one rule.
    """
    peak = await peak_equity(session)
    if suspect:
        return peak, await closed_peak_equity(session)
    return max(peak, equity), await update_closed_peak(session, closed_equity)


async def realized_pnl_running_max(
    session: AsyncSession, exclude_mode: str | None = "paper"
) -> float:
    """Highest cumulative realised PnL in closing order (never below 0)."""
    stmt = select(Position.realized_pnl).where(Position.status == "closed")
    if exclude_mode:
        stmt = stmt.where(Position.mode != exclude_mode)
    rows = (await session.execute(stmt.order_by(Position.closed_at))).scalars().all()
    running = best = 0.0
    for pnl in rows:
        running += float(pnl or 0.0)
        best = max(best, running)
    return best


async def heal_closed_peak(session: AsyncSession) -> tuple[float, float] | None:
    """Rebuild the closed-equity peak from trade history when it cannot be trusted.

    The peak is only ever raised by a reading, so a single one taken with an
    unclassified deposit on the exchange poisons it for good (a large deposit
    read as profit becomes a "peak" no trading can reach again). Closed
    equity is capital in plus realised PnL, so `base + running maximum of
    realised PnL` is the highest level the ledger can justify - a correctly
    scaled peak is always at or below it. A stored value more than twice the
    flow tolerance above that ceiling is replaced by it. Returns
    (before, after) when corrected, else None.
    """
    base = await capital_base(session)
    if base <= 0:
        return None
    stored = await closed_peak_equity(session)
    # The highest closed equity the ledger can justify: capital in, plus the
    # best cumulative realised result. A correctly scaled peak sits between
    # the base and this, whatever flows have happened.
    ceiling = base + await realized_pnl_running_max(session)
    slack = 2 * flow_tolerance(base)
    if stored <= ceiling + slack:
        return None
    await set_setting(session, CLOSED_PEAK_KEY, float(ceiling))
    return stored, ceiling


# The earlier name, kept so an out-of-tree caller does not break.
heal_closed_pnl_peak = heal_closed_peak


async def equity_curve(
    session: AsyncSession,
    since: dt.datetime,
    limit: int = 2000,
    exclude_suspect: bool = False,
) -> list[EquitySnapshot]:
    stmt = (
        select(EquitySnapshot)
        .where(EquitySnapshot.taken_at >= since)
        .order_by(EquitySnapshot.taken_at)
        .limit(limit)
    )
    if exclude_suspect:
        stmt = stmt.where(EquitySnapshot.suspect.is_not(True))
    return list((await session.execute(stmt)).scalars().all())


async def day_open(
    session: AsyncSession, midnight: dt.datetime
) -> tuple[EquitySnapshot | None, float]:
    """The first attributable snapshot of the UTC day and the net flow since it.

    Flows are counted from that snapshot's own time, not from midnight: a
    deposit recorded between midnight and the first clean snapshot is already
    inside the snapshot, and subtracting it again would read as a loss of the
    same size. With no clean snapshot yet, flows are counted from midnight.
    """
    curve = await equity_curve(session, midnight, limit=1, exclude_suspect=True)
    first = curve[0] if curve else None
    flow = await net_flow_since(session, first.taken_at if first else midnight)
    return first, flow


async def mark_snapshots_suspect(
    session: AsyncSession, since: dt.datetime, until: dt.datetime
) -> int:
    """Flag the snapshots taken in (since, until]; returns how many changed."""
    stmt = (
        update(EquitySnapshot)
        .where(
            EquitySnapshot.taken_at > since,
            EquitySnapshot.taken_at <= until,
            EquitySnapshot.suspect.is_not(True),
        )
        .values(suspect=True)
        # A bulk flag update: nothing loaded in this session needs re-syncing,
        # and the default "evaluate" would compare criteria in Python.
        .execution_options(synchronize_session=False)
    )
    return int((await session.execute(stmt)).rowcount or 0)


# --------------------------------------------------------------------------
# capital flows (deposits / withdrawals)
# --------------------------------------------------------------------------


async def add_capital_flow(session: AsyncSession, **kwargs: Any) -> CapitalFlow:
    """Record a deposit or withdrawal and settle the peaks around it.

    Both peaks are invariant to flows by construction, so nothing is re-based
    here: `peak_equity` restates every snapshot in current capital terms when
    it is read, and the closed-PnL peak is stored as PnL over the base, which
    a flow moves in step with closed equity. What must happen here is the
    reverse - readings taken while the money was on the exchange but not yet
    in this ledger (the reconciler's "unexplained since" window) are marked
    suspect so the restatement cannot count the flow twice, and the in-flight
    signal is cleared in the same transaction as the row.
    """
    if kwargs.get("source") != "initial":
        # Scale the closed-equity peak by what this flow does to closed equity,
        # measured before the row lands. Without it the peak keeps its level
        # but the drawdown *ratio* moves with the base, so a withdrawal reads
        # as a loss (a 30% drawdown reading on a -2% account).
        closed_before = await capital_base(session) + await realized_pnl_total(
            session, exclude_mode="paper"
        )
        amount = float(kwargs.get("amount") or 0.0)
        signed = amount if kwargs.get("kind") == "deposit" else -amount
        await rescale_closed_peak(session, closed_before, signed)

    flow = CapitalFlow(**kwargs)
    session.add(flow)
    await session.flush()
    if flow.source != "initial":
        await settle_unexplained_balance(session)
    return flow


def flow_tolerance(base: float) -> float:
    """How far the quote balance may stray from the trade history before it is a flow."""
    return max(2.0, base * 0.02)


async def expected_free_balance(session: AsyncSession) -> tuple[float, float]:
    """(quote balance the trade history predicts, tolerance).

    Capital in, plus realised PnL, minus what open positions have locked.
    Paper rows never touch the exchange balance and are left out.
    """
    base = await capital_base(session)
    realized = await realized_pnl_total(session, exclude_mode="paper")
    locked = await open_notional(session, exclude_mode="paper")
    return base + realized - locked, flow_tolerance(base)


async def unexplained_balance(
    session: AsyncSession, quote_balance: float | None = None
) -> float:
    """Signed quote-balance difference the trade history cannot explain; 0.0 when explained.

    The reconciler's persisted verdict comes first - it survives across
    processes and restarts. A live reading (`quote_balance`, the same figure
    an equity was built from) is then checked against the same expectation,
    so a flow that landed seconds ago is caught before the next sweep.
    None means there is no exchange reading (paper).
    """
    state = await get_sync_state(session)
    if state.balance_unexplained:
        return float(state.balance_unexplained)
    if quote_balance is None:
        return 0.0
    expected, tolerance = await expected_free_balance(session)
    difference = quote_balance - expected
    return difference if abs(difference) >= tolerance else 0.0


async def settle_unexplained_balance(session: AsyncSession) -> int:
    """Clear the in-flight signal; snapshots inside its window become suspect.

    Called when a flow is recorded (by the reconciler or by hand) and when a
    sweep finds the balance explained again. Returns how many snapshots were
    flagged.
    """
    state = await get_sync_state(session)
    marked = 0
    if state.balance_unexplained_since is not None:
        marked = await mark_snapshots_suspect(
            session, state.balance_unexplained_since, utcnow()
        )
    if state.balance_unexplained or state.balance_unexplained_since is not None:
        await update_sync_state(
            session, balance_unexplained=0.0, balance_unexplained_since=None
        )
    return marked


async def capital_base(session: AsyncSession) -> float:
    """Net capital the user has put in. The baseline for every % figure."""
    deposits = (
        await session.execute(
            select(func.coalesce(func.sum(CapitalFlow.amount), 0.0)).where(
                CapitalFlow.kind == "deposit"
            )
        )
    ).scalar_one()
    withdrawals = (
        await session.execute(
            select(func.coalesce(func.sum(CapitalFlow.amount), 0.0)).where(
                CapitalFlow.kind == "withdrawal"
            )
        )
    ).scalar_one()
    return float(deposits) - float(withdrawals)


async def capital_flows(session: AsyncSession, limit: int = 100) -> list[CapitalFlow]:
    stmt = select(CapitalFlow).order_by(CapitalFlow.occurred_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def capital_flow_exists(session: AsyncSession, external_id: str) -> bool:
    stmt = select(CapitalFlow.id).where(CapitalFlow.external_id == external_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def net_flow_since(session: AsyncSession, since: dt.datetime) -> float:
    """Capital added minus removed since a point in time - used to de-bias PnL.

    The seeded "initial" flow is the starting balance, not money that moved
    during the day, so it is excluded: on the first day it would otherwise
    read as a 100% deposit against a day that opened at that same balance.
    """
    rows = (
        await session.execute(
            select(CapitalFlow.kind, CapitalFlow.amount).where(
                CapitalFlow.occurred_at >= since, CapitalFlow.source != "initial"
            )
        )
    ).all()
    return sum(a if k == "deposit" else -a for k, a in rows)


# --------------------------------------------------------------------------
# exchange sync state
# --------------------------------------------------------------------------


async def get_sync_state(session: AsyncSession) -> ExchangeSync:
    state = await session.get(ExchangeSync, 1)
    if state is None:
        state = ExchangeSync(id=1)
        session.add(state)
        await session.flush()
    return state


async def update_sync_state(session: AsyncSession, **fields: Any) -> ExchangeSync:
    state = await get_sync_state(session)
    for key, value in fields.items():
        if hasattr(state, key):
            setattr(state, key, value)
    state.updated_at = utcnow()
    await session.flush()
    return state


# --------------------------------------------------------------------------
# settings (runtime overrides)
# --------------------------------------------------------------------------


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return default
    return row.value.get("v", default) if isinstance(row.value, dict) else default


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    stmt = pg_insert(Setting).values(key=key, value={"v": value})
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key], set_={"value": {"v": value}, "updated_at": utcnow()}
    )
    await session.execute(stmt)


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars().all()
    return {
        r.key: (r.value.get("v") if isinstance(r.value, dict) else r.value) for r in rows
    }


# --------------------------------------------------------------------------
# events, models, llm
# --------------------------------------------------------------------------


async def log_event(
    session: AsyncSession,
    message: str,
    level: str = "info",
    source: str = "engine",
    data: dict | None = None,
    notify: bool = True,
) -> EventLog:
    """Write an audit row and, unless `notify=False`, tell the user's phone.

    This is the one place every worker and the API record what happened, so
    hooking the notifier here means nothing is ever forgotten. Callers that
    already emitted a richer notification themselves pass `notify=False`.
    The hook is fire-and-forget and never raises.
    """
    event = EventLog(message=message, level=level, source=source, data=data or {})
    session.add(event)
    await session.flush()
    if notify:
        # Imported here: the notifier reads its preferences through this module.
        from app.notify import service as notify_service

        notify_service.notify_event(level, source, message, data or {})
    return event


async def recent_events(
    session: AsyncSession, limit: int = 200, level: str | None = None
) -> list[EventLog]:
    stmt = select(EventLog).order_by(EventLog.created_at.desc()).limit(limit)
    if level:
        stmt = stmt.where(EventLog.level == level)
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# push subscriptions (Web Push devices)
# --------------------------------------------------------------------------


async def upsert_push_subscription(
    session: AsyncSession, endpoint: str, p256dh: str, auth: str, user_agent: str | None
) -> PushSubscription:
    """Register a device, or refresh its keys if the endpoint is known."""
    stmt = pg_insert(PushSubscription).values(
        endpoint=endpoint, p256dh=p256dh, auth=auth, user_agent=user_agent
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PushSubscription.endpoint],
        set_={"p256dh": p256dh, "auth": auth, "user_agent": user_agent, "failures": 0},
    )
    await session.execute(stmt)
    row = (
        await session.execute(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )
    ).scalar_one()
    return row


async def delete_push_subscription(session: AsyncSession, endpoint: str) -> bool:
    result = await session.execute(
        delete(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    return bool(result.rowcount)


async def push_subscriptions(session: AsyncSession) -> list[PushSubscription]:
    stmt = select(PushSubscription).order_by(PushSubscription.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def count_push_subscriptions(session: AsyncSession) -> int:
    stmt = select(func.count(PushSubscription.id))
    return int((await session.execute(stmt)).scalar_one() or 0)


async def mark_push_result(session: AsyncSession, subscription_id: int, ok: bool) -> None:
    row = await session.get(PushSubscription, subscription_id)
    if row is None:
        return
    if ok:
        row.last_success_at = utcnow()
        row.failures = 0
    else:
        row.failures = int(row.failures or 0) + 1


# --------------------------------------------------------------------------
# passkeys (WebAuthn credentials)
# --------------------------------------------------------------------------


async def list_passkeys(session: AsyncSession, rp_id: str | None = None) -> list[Passkey]:
    """All enrolled keys, oldest first; `rp_id` narrows to one host."""
    stmt = select(Passkey).order_by(Passkey.created_at, Passkey.id)
    if rp_id:
        stmt = stmt.where(Passkey.rp_id == rp_id)
    return list((await session.execute(stmt)).scalars().all())


async def count_passkeys(session: AsyncSession, rp_id: str | None = None) -> int:
    stmt = select(func.count(Passkey.id))
    if rp_id:
        stmt = stmt.where(Passkey.rp_id == rp_id)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def passkey_by_credential_id(session: AsyncSession, credential_id: str) -> Passkey | None:
    stmt = select(Passkey).where(Passkey.credential_id == credential_id)
    return (await session.execute(stmt)).scalars().first()


async def add_passkey(session: AsyncSession, **kwargs: Any) -> Passkey:
    row = Passkey(**kwargs)
    session.add(row)
    await session.flush()
    return row


async def delete_passkey(session: AsyncSession, passkey_id: int) -> Passkey | None:
    """Remove one key; returns the row so the caller can name it in the audit log."""
    row = await session.get(Passkey, passkey_id)
    if row is None:
        return None
    await session.delete(row)
    await session.flush()
    return row


async def mark_passkey_used(
    session: AsyncSession, passkey_id: int, sign_count: int, backed_up: bool
) -> None:
    row = await session.get(Passkey, passkey_id)
    if row is None:
        return
    row.sign_count = int(sign_count)
    row.backed_up = bool(backed_up)
    row.last_used_at = utcnow()


async def active_model(
    session: AsyncSession, symbol: str, timeframe: str
) -> MLModel | None:
    stmt = (
        select(MLModel)
        .where(MLModel.symbol == symbol, MLModel.timeframe == timeframe, MLModel.active.is_(True))
        .order_by(MLModel.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def register_model(session: AsyncSession, **kwargs: Any) -> MLModel:
    symbol = kwargs.get("symbol", "ALL")
    timeframe = kwargs["timeframe"]
    existing = (
        await session.execute(
            select(MLModel).where(MLModel.symbol == symbol, MLModel.timeframe == timeframe)
        )
    ).scalars().all()
    for row in existing:
        row.active = False
    model = MLModel(**kwargs)
    session.add(model)
    await session.flush()
    return model


async def record_llm_review(session: AsyncSession, **kwargs: Any) -> LLMReview:
    review = LLMReview(**kwargs)
    session.add(review)
    await session.flush()
    return review


async def latest_llm_review(
    session: AsyncSession, scope: str = "market", symbol: str | None = None
) -> LLMReview | None:
    stmt = select(LLMReview).where(LLMReview.scope == scope)
    if symbol:
        stmt = stmt.where(LLMReview.symbol == symbol)
    stmt = stmt.order_by(LLMReview.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def llm_calls_today(session: AsyncSession) -> int:
    start = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    stmt = select(func.count(LLMReview.id)).where(LLMReview.created_at >= start)
    return (await session.execute(stmt)).scalar_one() or 0


# --------------------------------------------------------------------------
# investment committee
# --------------------------------------------------------------------------


async def record_committee_meeting(session: AsyncSession, **kwargs: Any) -> CommitteeMeeting:
    row = CommitteeMeeting(**kwargs)
    session.add(row)
    await session.flush()
    return row


async def committee_meetings(session: AsyncSession, limit: int = 20) -> list[CommitteeMeeting]:
    stmt = select(CommitteeMeeting).order_by(CommitteeMeeting.held_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def committee_meeting(session: AsyncSession, meeting_id: int) -> CommitteeMeeting | None:
    return await session.get(CommitteeMeeting, meeting_id)


async def latest_committee_meeting(session: AsyncSession) -> CommitteeMeeting | None:
    stmt = select(CommitteeMeeting).order_by(CommitteeMeeting.held_at.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def set_committee_proposal_status(
    session: AsyncSession, meeting: CommitteeMeeting, index: int, status: dict
) -> CommitteeMeeting:
    """Store the user's verdict on one proposal. A new dict is assigned so
    SQLAlchemy notices the change (JSON columns are not mutation-tracked)."""
    meeting.proposal_status = {**(meeting.proposal_status or {}), str(index): status}
    await session.flush()
    return meeting


async def llm_reviews_since(
    session: AsyncSession, since: dt.datetime, scope: str = "market"
) -> list[LLMReview]:
    stmt = (
        select(LLMReview)
        .where(LLMReview.scope == scope, LLMReview.created_at >= since)
        .order_by(LLMReview.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_events_like(
    session: AsyncSession, pattern: str, since: dt.datetime, source: str | None = None
) -> int:
    """How many audit rows since `since` match `pattern` (SQL LIKE)."""
    stmt = select(func.count(EventLog.id)).where(
        EventLog.created_at >= since, EventLog.message.like(pattern)
    )
    if source:
        stmt = stmt.where(EventLog.source == source)
    return int((await session.execute(stmt)).scalar_one() or 0)
