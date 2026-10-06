"""Web Push channel: VAPID-signed pushes to every browser that subscribed.

The VAPID key pair is generated once (API startup) and kept in the settings
table so every worker signs with the same key the browsers subscribed to.
Workers only *read* the pair: two processes generating at the same moment
would leave half the subscriptions signed with the wrong key.

pywebpush is synchronous (requests), so each push runs in a thread.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from app.config import settings
from app.db import repo
from app.db.session import session_scope

if TYPE_CHECKING:
    from app.notify.service import Notification

log = logging.getLogger(__name__)

PRIVATE_KEY = "vapid_private_key"
PUBLIC_KEY = "vapid_public_key"
PUSH_TIMEOUT = 8.0
PUSH_TTL = 3600  # seconds the push service keeps it for an offline phone


@dataclass(frozen=True)
class VapidKeys:
    private_pem: str
    public_key: str  # raw uncompressed point, base64url - what the browser needs


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_vapid_keys() -> VapidKeys:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return VapidKeys(pem, public_key_from_pem(pem))


def public_key_from_pem(pem: str) -> str:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("VAPID key is not an EC key")
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(point)


def ensure_vapid_keys_sync(stored: dict[str, Any] | None = None) -> VapidKeys:
    """The stored pair when it is usable, otherwise a fresh one. Pure: no I/O."""
    private = (stored or {}).get(PRIVATE_KEY)
    if private:
        try:
            return VapidKeys(str(private), public_key_from_pem(str(private)))
        except Exception as exc:  # noqa: BLE001 - a corrupt row must not brick push
            log.warning("stored VAPID key unusable (%s); generating a new pair", exc)
    return generate_vapid_keys()


_keys: VapidKeys | None = None
_vapid: Vapid | None = None


async def ensure_vapid_keys(session) -> VapidKeys:
    """API startup: load the pair, or generate and store it."""
    global _keys, _vapid
    stored = {
        PRIVATE_KEY: await repo.get_setting(session, PRIVATE_KEY, None),
        PUBLIC_KEY: await repo.get_setting(session, PUBLIC_KEY, None),
    }
    keys = ensure_vapid_keys_sync(stored)
    if keys.private_pem != stored[PRIVATE_KEY] or keys.public_key != stored[PUBLIC_KEY]:
        await repo.set_setting(session, PRIVATE_KEY, keys.private_pem)
        await repo.set_setting(session, PUBLIC_KEY, keys.public_key)
        log.info("VAPID key pair stored")
    _keys, _vapid = keys, None
    return keys


async def load_vapid_keys(session) -> VapidKeys | None:
    """Workers: the stored pair, or None until the API has generated it."""
    global _keys
    if _keys is not None:
        return _keys
    private = await repo.get_setting(session, PRIVATE_KEY, None)
    if not private:
        return None
    try:
        _keys = VapidKeys(str(private), public_key_from_pem(str(private)))
    except Exception as exc:  # noqa: BLE001
        log.warning("stored VAPID key unusable: %s", exc)
        return None
    return _keys


def _signer(keys: VapidKeys) -> Vapid:
    global _vapid
    if _vapid is None:
        _vapid = Vapid.from_pem(keys.private_pem.encode("ascii"))
    return _vapid


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def payload_for(note: "Notification", silent: bool) -> str:
    return json.dumps(
        {
            "title": note.title,
            "body": note.body,
            "category": note.category,
            "level": note.level,
            "url": note.url,
            "tag": note.tag,
            "silent": silent,
        },
        ensure_ascii=False,
    )


def _push_one(subscription: dict, payload: str, keys: VapidKeys) -> None:
    """Blocking: runs in a thread. Raises WebPushException on a non-2xx."""
    session = None
    if settings.notify_http_proxy:
        session = requests.Session()
        session.proxies = {"http": settings.notify_http_proxy, "https": settings.notify_http_proxy}
    webpush(
        subscription_info=subscription,
        data=payload,
        vapid_private_key=_signer(keys),
        vapid_claims={"sub": settings.vapid_subject},
        ttl=PUSH_TTL,
        timeout=PUSH_TIMEOUT,
        requests_session=session,
    )


async def _deliver(row_id: int, subscription: dict, payload: str, keys: VapidKeys) -> str:
    try:
        await asyncio.to_thread(_push_one, subscription, payload, keys)
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            # The browser dropped the subscription; keep the table honest.
            async with session_scope() as session:
                await repo.delete_push_subscription(session, subscription["endpoint"])
            log.info("push subscription %s is gone (%s); removed", row_id, status)
            return "gone"
        async with session_scope() as session:
            await repo.mark_push_result(session, row_id, ok=False)
        log.warning("push to subscription %s failed: %s", row_id, str(exc)[:200])
        return "failed"
    except Exception as exc:  # noqa: BLE001 - network, encryption, anything
        async with session_scope() as session:
            await repo.mark_push_result(session, row_id, ok=False)
        log.warning("push to subscription %s failed: %s", row_id, str(exc)[:200])
        return "failed"
    async with session_scope() as session:
        await repo.mark_push_result(session, row_id, ok=True)
    return "sent"


async def send_all(note: "Notification", silent: bool = False) -> str:
    """Push to every stored subscription, in parallel. Returns a summary."""
    async with session_scope() as session:
        keys = await load_vapid_keys(session)
        rows = await repo.push_subscriptions(session)
        targets = [
            (row.id, {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}})
            for row in rows
        ]
    if keys is None:
        return "skipped: no VAPID keys yet"
    if not targets:
        return "skipped: no subscriptions"
    payload = payload_for(note, silent)
    outcomes = await asyncio.gather(
        *(_deliver(row_id, subscription, payload, keys) for row_id, subscription in targets)
    )
    sent = outcomes.count("sent")
    failed = outcomes.count("failed")
    gone = outcomes.count("gone")
    return f"sent {sent}/{len(targets)}" + (f", failed {failed}" if failed else "") + (
        f", removed {gone}" if gone else ""
    )
