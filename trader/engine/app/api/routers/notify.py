"""Notification endpoints: push devices, preferences, channel status, test."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import current_user
from app.api.schemas import (
    NotifyPrefs, NotifyPrefsPatch, PushSubscribeIn, PushUnsubscribeIn,
)
from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.notify import ipwatch, telegram, texts, webpush
from app.notify import service as notify

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notify", tags=["notify"])

PUSH_NOT_READY = "کلید Web Push هنوز ساخته نشده است؛ سرویس api را دوباره راه‌اندازی کن"


@router.get("/vapid-public-key")
async def vapid_public_key(_: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        keys = await webpush.load_vapid_keys(session)
    if keys is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=PUSH_NOT_READY)
    return {"public_key": keys.public_key}


@router.post("/subscribe")
async def subscribe(payload: PushSubscribeIn, user: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        row = await repo.upsert_push_subscription(
            session, payload.endpoint, payload.keys.p256dh, payload.keys.auth,
            (payload.user_agent or "")[:255] or None,
        )
        count = await repo.count_push_subscriptions(session)
        await repo.log_event(
            session,
            f"Push notifications enabled on a device by {user} ({count} device(s) now)",
            source="notify",
            data={"subscription_id": row.id, "user_agent": payload.user_agent},
            notify=False,
        )
    return {"ok": True, "id": row.id, "devices": count}


@router.delete("/subscribe")
async def unsubscribe(payload: PushUnsubscribeIn, user: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        removed = await repo.delete_push_subscription(session, payload.endpoint)
        count = await repo.count_push_subscriptions(session)
        if removed:
            await repo.log_event(
                session,
                f"Push notifications disabled on a device by {user} ({count} device(s) left)",
                source="notify",
                notify=False,
            )
    return {"ok": True, "removed": removed, "devices": count}


@router.get("/preferences", response_model=NotifyPrefs)
async def get_preferences(_: str = Depends(current_user)) -> NotifyPrefs:
    async with session_scope() as session:
        raw = await repo.get_setting(session, notify.PREFS_KEY, None)
    return NotifyPrefs(**notify.normalise_prefs(raw))


@router.patch("/preferences", response_model=NotifyPrefs)
async def patch_preferences(
    payload: NotifyPrefsPatch, user: str = Depends(current_user)
) -> NotifyPrefs:
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to update")
    unknown = set((patch.get("categories") or {})) - set(texts.CATEGORIES)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown category: {', '.join(sorted(unknown))}")
    async with session_scope() as session:
        prefs = await notify.save_prefs(session, patch)
        await repo.log_event(
            session,
            f"Notification preferences updated by {user}: "
            + ", ".join(f"{k}={v}" for k, v in patch.items()),
            source="notify",
            data=patch,
            notify=False,
        )
    return NotifyPrefs(**prefs)


@router.get("/status")
async def notify_status(_: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        devices = await repo.count_push_subscriptions(session)
        keys = await webpush.load_vapid_keys(session)
        ip = await ipwatch.current(session)
        raw_prefs = await repo.get_setting(session, notify.PREFS_KEY, None)
    return {
        "telegram": await telegram.status(),
        "push": {"devices": devices, "vapid_ready": keys is not None},
        "preferences": notify.normalise_prefs(raw_prefs),
        "contact": settings.vapid_subject,
        "tz": settings.tz,
        **ip,
    }


@router.post("/test")
async def send_test(user: str = Depends(current_user)) -> dict:
    """A test message on every channel, awaited so the panel can show the result."""
    results = await notify.send_test()
    async with session_scope() as session:
        await repo.log_event(
            session, f"Test notification sent by {user}: {results}",
            source="notify", data=results, notify=False,
        )
    return results
