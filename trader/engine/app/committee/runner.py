"""One meeting, start to finish: collect, analyse, persist, apply, notify.

`hold_meeting(reason)` is what the scheduler calls at the configured hours
(and on the `hold_meeting` bus command). A Redis lock makes a manual trigger
during a scheduled meeting a no-op instead of a second meeting.

What gets applied automatically is exactly what the advisor may already do:
the stance and the risk multiplier for NEW entries, written as a
`llm_reviews` row with `scope="market"` so the trader picks it up on its next
heartbeat the way it picks up the advisor's reviews (app/workers/trader.py).
Proposals are stored and shown; only the user's approval on the panel
(app/api/routers/committee.py) acts on them.

A health-check failure reaches the user as a system error whether or not
the model answered; the minutes go out under the "ai" category.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets

from app.committee import analyst, collector
from app.committee.analyst import CommitteeMinutes
from app.config import settings
from app.db import repo
from app.db.models import CommitteeMeeting
from app.db.session import session_scope
from app.exchange.base import ExchangeClient
from app.llm.advisor import advisor
from app.notify import service as notify
from app.workers import bus

log = logging.getLogger(__name__)

LOCK_KEY = "gtr:committee:lock"
LOCK_TTL = 15 * 60
LAST_HELD_KEY = "committee_last_held"
# Meetings start this many minutes after the configured hour, so the 4h
# evaluation at the top of the hour and the reconciler's sweep are already in.
MEETING_MINUTE = 5
NOTIFY_TIMEOUT = 30.0

TITLE_MINUTES = "صورت‌جلسه کمیته"
TITLE_HEALTH = "کمیته: بررسی سلامت ناموفق"


# --------------------------------------------------------------------------
# schedule helpers (also used by the scheduler and the API)
# --------------------------------------------------------------------------


def next_meeting_at(after: dt.datetime, hours: list[int] | None = None) -> dt.datetime:
    """The first meeting slot strictly after `after` (UTC)."""
    slots = sorted(set(hours if hours is not None else settings.committee_hours_utc)) or [4, 16]
    base = after.astimezone(dt.timezone.utc)
    for day in (0, 1, 2):
        for hour in slots:
            slot = (base + dt.timedelta(days=day)).replace(
                hour=hour, minute=MEETING_MINUTE, second=0, microsecond=0
            )
            if slot > base:
                return slot
    return base + dt.timedelta(days=1)


def is_due(now: dt.datetime, last_held: dt.datetime | None) -> bool:
    """Never met, or a slot has passed since the last meeting."""
    return last_held is None or now >= next_meeting_at(last_held)


async def last_held(session) -> dt.datetime | None:
    stored = await repo.get_setting(session, LAST_HELD_KEY, None)
    if stored:
        try:
            stamp = dt.datetime.fromisoformat(str(stored))
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    row = await repo.latest_committee_meeting(session)
    if row is None:
        return None
    held = row.held_at
    return held if held.tzinfo else held.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# the lock
# --------------------------------------------------------------------------


async def _acquire_lock() -> str | None:
    token = secrets.token_hex(8)
    try:
        ok = await bus.get_redis().set(LOCK_KEY, token, nx=True, ex=LOCK_TTL)
        return token if ok else None
    except Exception as exc:  # noqa: BLE001 - no Redis, no double-meeting guard; still meet
        log.warning("committee lock unavailable (%s); meeting without it", exc)
        return "unlocked"


async def _release_lock(token: str) -> None:
    if token == "unlocked":
        return
    try:
        redis = bus.get_redis()
        if await redis.get(LOCK_KEY) == token:
            await redis.delete(LOCK_KEY)
    except Exception as exc:  # noqa: BLE001
        log.debug("committee lock release failed: %s", exc)


# --------------------------------------------------------------------------
# applying what the policy allows
# --------------------------------------------------------------------------


def _effective_stance(previous) -> tuple[str, float]:
    """What the trader is using right now: the latest market review while it
    is young enough (the trader's own rule), neutral / 1.0 otherwise."""
    if previous is None:
        return "neutral", 1.0
    created = previous.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    age_min = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60
    if age_min < settings.llm_review_minutes * 3:
        return previous.stance, float(previous.risk_multiplier)
    return "neutral", 1.0


async def _apply_decisions(
    minutes: CommitteeMinutes, usage: dict, reason: str
) -> tuple[list[dict], dict | None]:
    """Record the stance / multiplier as a market review; note everything else.

    Returns (applied_decisions, review) where `review` is what the trader will
    read (None when the minutes decided nothing the policy lets us apply).
    """
    stance, multiplier, rejected = analyst.parse_decisions(minutes)
    applied: list[dict] = [
        {"kind": r["kind"], "value": r["value"], "applied": False, "why": r["why"]} for r in rejected
    ]
    for decision in minutes.decisions:
        if decision.kind == "note":
            applied.append({"kind": "note", "value": decision.value, "applied": False,
                            "rationale_fa": decision.rationale_fa})
    if stance is None and multiplier is None:
        return applied, None

    rationale = " | ".join(
        d.rationale_fa for d in minutes.decisions if d.kind in ("stance", "risk_multiplier")
    )
    async with session_scope() as session:
        previous = await repo.latest_llm_review(session, "market")
        current_stance, current_multiplier = _effective_stance(previous)
        new_stance = stance or current_stance
        new_multiplier = multiplier if multiplier is not None else current_multiplier
        review = await repo.record_llm_review(
            session,
            scope="market",
            stance=new_stance,
            risk_multiplier=new_multiplier,
            confidence=minutes.confidence,
            reasoning=("[committee] " + rationale)[:1200],
            payload={
                "source": "committee",
                "reason": reason,
                "decided": {"stance": stance, "risk_multiplier": multiplier},
                "carried": {"stance": current_stance, "risk_multiplier": current_multiplier},
            },
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )
    for decision in minutes.decisions:
        if decision.kind == "stance" and stance is not None:
            applied.append({"kind": "stance", "value": stance, "applied": True,
                            "changed": stance != current_stance, "rationale_fa": decision.rationale_fa})
        elif decision.kind == "risk_multiplier" and multiplier is not None:
            applied.append({"kind": "risk_multiplier", "value": multiplier, "applied": True,
                            "changed": multiplier != current_multiplier, "rationale_fa": decision.rationale_fa})
    await bus.publish(
        bus.CH_EVENTS,
        {"type": "market_review", "stance": new_stance, "risk_multiplier": new_multiplier,
         "source": "committee"},
    )
    return applied, {"id": review.id, "stance": new_stance, "risk_multiplier": new_multiplier}


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


def _health_body(briefing: dict) -> str:
    checks = [c for c in (briefing.get("health") or {}).get("checks", []) if c.get("ok") is False]
    lines = [f"• {c.get('label_fa') or c.get('code')}: {c.get('detail', '')}" for c in checks]
    return "بررسی سلامت کمیته این موارد را قرمز دید:\n" + "\n".join(lines) + \
        "\n\nتا رفع مشکل، ورود جدید عاقلانه نیست؛ پوزیشن‌های باز با استاپ سمت صرافی محافظت می‌شوند."


def _minutes_body(minutes: CommitteeMinutes, briefing: dict, meeting_id: int) -> str:
    body = minutes.summary_fa.strip()
    if minutes.risks:
        first = minutes.risks[0]
        body += f"\n\n⚠️ {first.title_fa}: {first.detail_fa}"
    if minutes.proposals:
        body += f"\n\n{len(minutes.proposals)} پیشنهاد منتظر تایید توست (صفحه‌ی کمیته، جلسه #{meeting_id})."
    return body


# --------------------------------------------------------------------------
# the meeting
# --------------------------------------------------------------------------


async def hold_meeting(
    reason: str = "scheduled", client: ExchangeClient | None = None
) -> CommitteeMeeting | None:
    """Collect, analyse, persist, apply, notify. None when a meeting is already running."""
    token = await _acquire_lock()
    if token is None:
        log.info("committee meeting (%s) skipped: another meeting is in progress", reason)
        return None
    try:
        return await _hold(reason, client)
    finally:
        await _release_lock(token)


async def _hold(reason: str, client: ExchangeClient | None) -> CommitteeMeeting:
    now = dt.datetime.now(dt.timezone.utc)
    log.info("committee meeting starts (%s)", reason)

    briefing = await collector.build(client)
    health = briefing.get("health") or {}
    health_ok = bool(health.get("ok", True))
    if not health_ok:
        notify.emit("system", "error", TITLE_HEALTH, _health_body(briefing),
                    {"url": "/committee", "failed": health.get("failed", [])})

    minutes, usage = await analyst.analyse(briefing, reason)
    provider = str(usage.get("provider") or "template")
    model = str(usage.get("model") or "")
    if minutes is None:
        why = advisor.status().get("last_error") or ("advisor disabled" if not advisor.enabled else "no answer")
        log.warning("committee: no minutes from the model (%s); publishing the briefing", why)
        minutes = analyst.fallback_minutes(briefing, why=str(why))
        provider, model = "template", ""
        applied, review = [], None
    else:
        applied, review = await _apply_decisions(minutes, usage, reason)

    tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
    high_risks = [r for r in minutes.risks if r.severity == "high"]
    async with session_scope() as session:
        row = await repo.record_committee_meeting(
            session,
            held_at=now,
            reason=reason,
            briefing=briefing,
            minutes=minutes.model_dump(),
            provider=provider,
            model=model,
            tokens=tokens,
            applied_decisions=applied,
            proposal_status={},
            health_ok=health_ok,
            delivery={},
        )
        await repo.set_setting(session, LAST_HELD_KEY, now.isoformat())
        summary_line = (
            f"Committee meeting #{row.id} held ({reason}) via {provider}"
            + (f"/{model}" if model else "")
            + f": {len(minutes.risks)} risk(s), {len(minutes.decisions)} decision(s), "
            f"{len(minutes.proposals)} proposal(s), health {'ok' if health_ok else 'FAILED ' + ','.join(health.get('failed', []))}"
            + (f"; stance {review['stance']} x{review['risk_multiplier']:.2f}" if review else "")
        )
        await repo.log_event(
            session, summary_line,
            level="warn" if (not health_ok or high_risks) else "info",
            source="committee",
            data={
                "meeting_id": row.id, "reason": reason, "provider": provider, "model": model,
                "tokens": tokens, "health_failed": health.get("failed", []),
                "applied": applied, "proposals": len(minutes.proposals),
            },
            notify=False,
        )
        meeting_id = row.id

    level = "warn" if (high_risks or not health_ok) else "info"
    note = notify.Notification(
        "ai", level, TITLE_MINUTES, _minutes_body(minutes, briefing, meeting_id),
        {"url": "/committee", "meeting_id": meeting_id},
    )
    try:
        delivery = await asyncio.wait_for(notify.send(note), timeout=NOTIFY_TIMEOUT)
    except asyncio.TimeoutError:
        delivery = {"error": f"no channel answered within {NOTIFY_TIMEOUT:.0f}s"}
    async with session_scope() as session:
        stored = await repo.committee_meeting(session, meeting_id)
        if stored is not None:
            stored.delivery = dict(delivery)
            row = stored

    await bus.publish(
        bus.CH_EVENTS,
        {"type": "committee", "meeting_id": meeting_id, "reason": reason,
         "health_ok": health_ok, "provider": provider, "proposals": len(minutes.proposals)},
    )
    log.info(
        "committee meeting #%s done via %s: health %s, %s proposal(s), delivery %s",
        meeting_id, provider, "ok" if health_ok else "FAILED", len(minutes.proposals), delivery,
    )
    return row
