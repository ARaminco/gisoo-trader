"""The investment committee's endpoints: minutes, a manual meeting, proposals.

The meeting itself runs in the scheduler (app/committee/runner.py); this
router only reads the minutes, queues a manual meeting through the command
bus, and records the user's verdict on proposals. An approval applies
nothing but the whitelist below - a caution-factor change inside 0.25-1.5, a
bounded pause of new entries, a recorded sleeve target - and every verdict
is an audit row plus a notification.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.auth import current_user
from app.committee import runner as committee
from app.config import settings
from app.db import repo
from app.db.models import CommitteeMeeting
from app.db.session import session_scope
from app.notify import service as notify
from app.workers import bus

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/committee", tags=["committee"])

SCHEDULER_STALE_S = 120
RISK_SCALE_MIN, RISK_SCALE_MAX = 0.25, 1.5
PAUSE_HOURS_MIN, PAUSE_HOURS_MAX, PAUSE_HOURS_DEFAULT = 1.0, 168.0, 24.0
SLEEVE_TARGET_KEY = "committee_sleeve_target"

ACTION_FA = {
    "core4h_risk_scale": "تغییر ضریب احتیاط",
    "add_capital": "افزودن سرمایه",
    "sleeve_target": "هدف اسلیو",
    "pause_bot": "توقف موقت ورودها",
    "other": "سایر",
}


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def _meeting_out(row: CommitteeMeeting, full: bool = False) -> dict:
    minutes = row.minutes or {}
    briefing = row.briefing or {}
    statuses = row.proposal_status or {}
    account = {k: v for k, v in (briefing.get("account") or {}).items() if k != "balances"}
    out = {
        "id": row.id,
        "held_at": row.held_at,
        "reason": row.reason,
        "provider": row.provider,
        "model": row.model,
        "tokens": row.tokens,
        "health_ok": row.health_ok,
        "delivery": row.delivery or {},
        "summary_fa": minutes.get("summary_fa", ""),
        "market_view_fa": minutes.get("market_view_fa", ""),
        "positions_view_fa": minutes.get("positions_view_fa", ""),
        "risks": minutes.get("risks", []),
        "decisions": minutes.get("decisions", []),
        "confidence": minutes.get("confidence"),
        "proposals": [
            {**proposal, "index": index, "status": statuses.get(str(index))}
            for index, proposal in enumerate(minutes.get("proposals", []))
        ],
        "applied_decisions": row.applied_decisions or [],
        "health": briefing.get("health") or {"ok": True, "failed": [], "checks": []},
        "account": account,
        "positions": briefing.get("positions", []),
        "market": briefing.get("market"),
        "gates": briefing.get("gates"),
        "performance": briefing.get("performance"),
        "stance_history": briefing.get("stance_history", []),
        "settings": briefing.get("settings"),
    }
    if full:
        out["briefing"] = briefing
    return out


async def _scheduler_online() -> bool:
    status_map = await bus.get_status()
    at = (status_map.get("scheduler") or {}).get("at")
    if not at:
        return False
    try:
        stamp = dt.datetime.fromisoformat(str(at))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() <= SCHEDULER_STALE_S


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


@router.get("/minutes")
async def minutes(limit: int = Query(20, ge=1, le=100), _: str = Depends(current_user)) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    async with session_scope() as session:
        rows = await repo.committee_meetings(session, limit)
        last = await committee.last_held(session)
    return {
        "enabled": settings.committee_enabled,
        "hours_utc": list(settings.committee_hours_utc),
        "meeting_minute": committee.MEETING_MINUTE,
        "tz": settings.tz,
        "last_held_at": last,
        "next_meeting_at": committee.next_meeting_at(now) if settings.committee_enabled else None,
        "scheduler_online": await _scheduler_online(),
        "meetings": [_meeting_out(row) for row in rows],
    }


@router.get("/minutes/{meeting_id}")
async def minute(meeting_id: int, _: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        row = await repo.committee_meeting(session, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="صورت‌جلسه پیدا نشد")
    return _meeting_out(row, full=True)


# --------------------------------------------------------------------------
# a manual meeting
# --------------------------------------------------------------------------


@router.post("/meet")
async def meet(user: str = Depends(current_user)) -> dict:
    """Queue a meeting on the scheduler. The minutes appear in the list when done."""
    if not settings.committee_enabled:
        raise HTTPException(status_code=400, detail="کمیته خاموش است (COMMITTEE_ENABLED=false)")
    if not await _scheduler_online():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="زمان‌بند آفلاین است؛ جلسه بدون آن برگزار نمی‌شود",
        )
    await bus.publish(bus.CH_COMMANDS, {"action": "hold_meeting", "by": user})
    async with session_scope() as session:
        await repo.log_event(
            session, f"Committee meeting requested by {user}", source="committee",
            data={"by": user}, notify=False,
        )
    return {"queued": True}


# --------------------------------------------------------------------------
# proposals
# --------------------------------------------------------------------------


async def _apply(session, row: CommitteeMeeting, index: int, proposal: dict, user: str) -> dict:
    """The whitelist. Anything not listed is recorded, never executed."""
    action = proposal.get("action")
    params = proposal.get("params") or {}
    now = dt.datetime.now(dt.timezone.utc)

    if action == "core4h_risk_scale":
        value = params.get("risk_scale")
        try:
            value = round(float(value), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="پیشنهاد مقدار ضریب احتیاط ندارد")
        if not RISK_SCALE_MIN <= value <= RISK_SCALE_MAX:
            raise HTTPException(status_code=400, detail="ضریب احتیاط باید بین 0.25 و 1.5 باشد")
        before = float(await repo.get_setting(session, "core4h_risk_scale", settings.core4h_risk_scale))
        await repo.set_setting(session, "core4h_risk_scale", value)
        # This process now; the workers read the settings table on their next
        # heartbeat (app/util/runtime_settings.py, within ~20 s).
        object.__setattr__(settings, "core4h_risk_scale", value)
        return {
            "applied": True, "setting": "core4h_risk_scale", "before": before, "value": value,
            "note_fa": f"ضریب احتیاط از {before:g} به {value:g} تغییر کرد؛ موتور تا ۲۰ ثانیه دیگر آن را می‌خواند.",
        }

    if action == "pause_bot":
        hours = params.get("hours")
        try:
            hours = float(hours) if hours is not None else PAUSE_HOURS_DEFAULT
        except (TypeError, ValueError):
            hours = PAUSE_HOURS_DEFAULT
        if not PAUSE_HOURS_MIN <= hours <= PAUSE_HOURS_MAX:
            raise HTTPException(status_code=400, detail="مدت توقف باید بین 1 و 168 ساعت باشد")
        until = now + dt.timedelta(hours=hours)
        await repo.set_setting(session, "paused_until", until.isoformat())
        return {
            "applied": True, "setting": "paused_until", "value": until.isoformat(), "hours": hours,
            "note_fa": f"ورود جدید تا {hours:g} ساعت متوقف شد؛ پوزیشن‌های باز و استاپ‌ها سر جایشان می‌مانند.",
        }

    if action == "sleeve_target":
        record = {
            **{k: v for k, v in params.items() if v is not None},
            "recorded_at": now.isoformat(), "meeting_id": row.id, "index": index, "by": user,
        }
        await repo.set_setting(session, SLEEVE_TARGET_KEY, record)
        return {
            "applied": False, "recorded": True, "setting": SLEEVE_TARGET_KEY, "value": record,
            "note_fa": "ثبت شد؛ اسلیو هنوز فعال نیست و پولی جابه‌جا نمی‌شود.",
        }

    # add_capital / other: the user acts outside the system.
    return {
        "applied": False, "acknowledged": True,
        "note_fa": "ثبت شد؛ این کار خارج از ربات انجام می‌شود (مثلاً واریز روی صرافی و ثبت در صفحه‌ی سرمایه).",
    }


async def _verdict(meeting_id: int, index: int, verb: Literal["approve", "decline"], user: str) -> dict:
    async with session_scope() as session:
        row = await repo.committee_meeting(session, meeting_id)
        if row is None:
            raise HTTPException(status_code=404, detail="صورت‌جلسه پیدا نشد")
        proposals = (row.minutes or {}).get("proposals") or []
        if index < 0 or index >= len(proposals):
            raise HTTPException(status_code=404, detail="پیشنهاد پیدا نشد")
        existing = (row.proposal_status or {}).get(str(index))
        if existing:
            raise HTTPException(status_code=409, detail="این پیشنهاد قبلاً تعیین تکلیف شده است")
        proposal = proposals[index]
        action = str(proposal.get("action") or "other")

        result = await _apply(session, row, index, proposal, user) if verb == "approve" else {"applied": False}
        verdict = {
            "status": "approved" if verb == "approve" else "declined",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "by": user,
            "action": action,
            "result": result,
        }
        await repo.set_committee_proposal_status(session, row, index, verdict)
        applied = bool(result.get("applied"))
        await repo.log_event(
            session,
            f"Committee proposal #{meeting_id}/{index} ({action}) "
            f"{'APPROVED' if verb == 'approve' else 'declined'} by {user}"
            + (f": {result.get('setting')}={result.get('value')}" if applied else ""),
            level="warn" if applied else "info",
            source="committee",
            data={"meeting_id": meeting_id, "index": index, "verdict": verdict},
            notify=False,
        )
        out = _meeting_out(row)

    title = "پیشنهاد کمیته تایید شد" if verb == "approve" else "پیشنهاد کمیته رد شد"
    body = f"{ACTION_FA.get(action, action)} — {proposal.get('title_fa', '')}"
    if verb == "approve":
        body += "\n" + str(result.get("note_fa", ""))
    notify.emit("control", "warn" if applied else "info", title, body,
                {"url": "/committee", "meeting_id": meeting_id, "index": index})
    if applied and result.get("setting") == "core4h_risk_scale":
        await bus.publish(bus.CH_EVENTS, {"type": "settings", "changed": ["core4h_risk_scale"]})
    return out


@router.post("/proposals/{meeting_id}/{index}/approve")
async def approve(meeting_id: int, index: int, user: str = Depends(current_user)) -> dict:
    return await _verdict(meeting_id, index, "approve", user)


@router.post("/proposals/{meeting_id}/{index}/decline")
async def decline(meeting_id: int, index: int, user: str = Depends(current_user)) -> dict:
    return await _verdict(meeting_id, index, "decline", user)
