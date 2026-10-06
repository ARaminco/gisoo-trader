"""Public-IP watchdog.

Exchange API keys should be restricted to this machine's public IP. When the
address changes (home connections do, after a modem restart) every signed
request fails - entries, protective stops and the reconcile sweep alike -
until the user adds the new address to the key's allow-list. So the
scheduler checks the address every five minutes and raises an error-level
notification the moment it differs from the last one seen.

The lookup goes through EXCHANGE_HTTP_PROXY when one is set, because that
proxy's address is the one the exchange sees.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import logging

import httpx

from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.notify import service

log = logging.getLogger(__name__)

SOURCES = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")
LOOKUP_TIMEOUT = 8.0
INTERVAL_SECONDS = 300

KEY_IP = "public_ip"
KEY_SEEN = "public_ip_seen_at"
KEY_CHANGED = "public_ip_changed_at"
KEY_PREVIOUS = "public_ip_previous"

CHANGE_TITLE = "IP عمومی عوض شد - کلید صرافی را به‌روز کن"
CHANGE_BODY = (
    "IP این سیستم از {old} به {new} تغییر کرد. اگر کلید API صرافی به IP محدود شده، فقط از IP قبلی "
    "پذیرفته می‌شود؛ تا IP جدید را به لیست مجاز کلید اضافه نکنی هیچ سفارشی - حتی بستن پوزیشن - به "
    "صرافی نمی‌رسد.\nدر پنل صرافی، بخش مدیریت کلید API، نشانی {new} را به IPهای مجاز اضافه کن."
)


async def fetch_public_ip() -> str | None:
    """First source that answers with a valid address, or None."""
    async with httpx.AsyncClient(
        timeout=LOOKUP_TIMEOUT,
        proxy=settings.exchange_http_proxy or None,
        headers={"User-Agent": "GisooTrader/0.1"},
    ) as client:
        for url in SOURCES:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return str(ipaddress.ip_address(response.text.strip()))
            except Exception as exc:  # noqa: BLE001 - try the next source
                log.debug("public IP lookup via %s failed: %s", url, exc)
    return None


async def current(session) -> dict:
    """What the API exposes: the last address seen and when."""
    return {
        "public_ip": await repo.get_setting(session, KEY_IP, None),
        "public_ip_seen_at": await repo.get_setting(session, KEY_SEEN, None),
        "public_ip_changed_at": await repo.get_setting(session, KEY_CHANGED, None),
        "public_ip_previous": await repo.get_setting(session, KEY_PREVIOUS, None),
    }


async def check_once() -> dict:
    """Look the address up, persist it, and alert on a change."""
    ip = await fetch_public_ip()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if ip is None:
        log.warning("public IP could not be determined from any source")
        return {"public_ip": None, "changed": False}

    async with session_scope() as session:
        previous = await repo.get_setting(session, KEY_IP, None)
        await repo.set_setting(session, KEY_IP, ip)
        await repo.set_setting(session, KEY_SEEN, now)
        changed = previous is not None and previous != ip
        if previous is None:
            await repo.log_event(
                session, f"Public IP recorded: {ip}", source="system",
                data={"public_ip": ip}, notify=False,
            )
            log.info("public IP recorded: %s", ip)
        elif changed:
            await repo.set_setting(session, KEY_CHANGED, now)
            await repo.set_setting(session, KEY_PREVIOUS, previous)
            await repo.log_event(
                session,
                f"Public IP changed from {previous} to {ip} - update the exchange API key "
                "allow-list or every signed request will fail",
                level="error",
                source="system",
                data={"public_ip": ip, "previous": previous},
                notify=False,
            )
            log.error("public IP changed: %s -> %s", previous, ip)
    if changed:
        service.emit(
            "system", "error", CHANGE_TITLE, CHANGE_BODY.format(old=previous, new=ip),
            {"public_ip": ip, "previous": previous},
        )
    return {"public_ip": ip, "previous": previous, "changed": changed}
