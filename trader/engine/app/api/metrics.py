"""Performance metrics, computed once and reused by every endpoint.

Capital flows are subtracted before any percentage is taken, so adding money to
the account never shows up as profit.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo


async def trade_stats(session: AsyncSession, since: dt.datetime | None = None) -> dict:
    # `status = 'closed'` and the time window are applied in SQL; only the
    # matching rows are hydrated.
    positions = await repo.closed_positions(session, since)

    if not positions:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
            "net_pnl": 0.0, "avg_r": 0.0, "expectancy": 0.0, "fees": 0.0,
            "best": 0.0, "worst": 0.0,
        }

    wins = [p for p in positions if p.realized_pnl > 0]
    losses = [p for p in positions if p.realized_pnl <= 0]
    gross_profit = sum(p.realized_pnl for p in wins)
    gross_loss = abs(sum(p.realized_pnl for p in losses))
    net = sum(p.realized_pnl for p in positions)

    return {
        "trades": len(positions),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(positions),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0.0
        ),
        "net_pnl": net,
        "avg_r": sum(p.r_multiple for p in positions) / len(positions),
        "expectancy": net / len(positions),
        "fees": sum(p.fees_paid or 0 for p in positions),
        "best": max(p.realized_pnl for p in positions),
        "worst": min(p.realized_pnl for p in positions),
    }


async def performance_summary(
    session: AsyncSession, equity: float, free_balance: float, marks: dict[str, float]
) -> dict:
    """Everything the dashboard header needs.

    Same rules as risk.manager.account_state, so the drawdown the panel shows
    is the one the kill switch acts on: a reading taken while a deposit or
    withdrawal is on the exchange but not yet in the ledger (`balance_unexplained`
    on the sync state, which is where the API's equity comes from) does not
    raise the peak and is measured with the unclassified money removed.
    """
    base = await repo.capital_base(session)
    if base <= 0:
        base = settings.initial_capital_usdt

    sync = await repo.get_sync_state(session)
    unexplained = float(sync.balance_unexplained or 0.0)
    suspect = unexplained != 0.0
    known_equity = equity - unexplained

    peak = max(await repo.peak_equity(session), base)
    if not suspect:
        peak = max(peak, equity)
    drawdown = (peak - known_equity) / peak * 100 if peak > 0 else 0.0

    midnight = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # a deposit made today is not profit made today
    first_today, day_flow = await repo.day_open(session, midnight)
    # With no clean snapshot yet (first day, or a fresh restart) the day
    # opened at whatever the base was *before* today's deposits - otherwise
    # seeding the initial capital this morning reads as a 100% loss.
    day_start = first_today.equity if first_today is not None else max(0.0, base - day_flow)

    open_positions = await repo.open_positions(session)
    unrealized = 0.0
    for position in open_positions:
        mark = marks.get(position.symbol, position.entry_price)
        direction = 1 if position.side == "long" else -1
        unrealized += (mark - position.entry_price) * position.qty * direction

    total = await trade_stats(session)
    today = await trade_stats(session, midnight)

    total_pnl = known_equity - base
    today_pnl = known_equity - day_start - day_flow

    # Closed equity and its peak: what the 4h core's kill switch watches.
    closed_equity = equity - unrealized
    known_closed = closed_equity - unexplained
    closed_peak = await repo.closed_peak_equity(session)
    if not suspect:
        closed_peak = max(closed_peak, closed_equity)
    closed_drawdown = (closed_peak - known_closed) / closed_peak * 100 if closed_peak > 0 else 0.0

    return {
        "capital_base": round(base, 4),
        "equity": round(equity, 4),
        "free_balance": round(free_balance, 4),
        "unrealized_pnl": round(unrealized, 4),
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl / base * 100, 4) if base else 0.0,
        "today_pnl": round(today_pnl, 4),
        "today_pnl_pct": round(today_pnl / day_start * 100, 4) if day_start else 0.0,
        "peak_equity": round(peak, 4),
        "drawdown_pct": round(max(0.0, drawdown), 4),
        "closed_equity": round(closed_equity, 4),
        "closed_peak_equity": round(closed_peak, 4),
        "closed_drawdown_pct": round(max(0.0, closed_drawdown), 4),
        # Non-zero while a deposit/withdrawal awaits classification; the
        # panel shows it as pending and the figures above exclude it.
        "balance_unexplained": round(unexplained, 4),
        "balance_unexplained_since": sync.balance_unexplained_since,
        "open_positions": len(open_positions),
        "trades_total": total["trades"],
        "trades_today": today["trades"],
        "win_rate": round(total["win_rate"], 4),
        "profit_factor": round(min(total["profit_factor"], 999.0), 3),
        "avg_r": round(total["avg_r"], 3),
    }
