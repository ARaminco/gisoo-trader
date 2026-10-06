"""Telegram channel: Bot API over httpx, HTML parse mode.

The chat id is either TELEGRAM_CHAT_ID from the environment or, when that is
empty, discovered: the scheduler polls `getUpdates` every minute until the
user writes anything to the bot (usually /start), stores the chat id in the
settings table and answers with a confirmation.
"""
from __future__ import annotations

import html
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.notify import texts

if TYPE_CHECKING:
    from app.notify.service import Notification

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CHAT_KEY = "telegram_chat_id"
CHAT_TITLE_KEY = "telegram_chat_title"
MAX_TEXT = 4000  # Telegram caps a message at 4096 characters
NEGATIVE_CACHE_SECONDS = 30.0

_chat_cache: tuple[float, str | None] = (0.0, None)
_update_offset: int | None = None


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.telegram_bot_token)


def _client() -> httpx.AsyncClient:
    # Telegram is blocked on many home connections in the region: honour the
    # notification proxy so the message still leaves.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=5.0),
        proxy=settings.notify_http_proxy or None,
        headers={"User-Agent": "GisooTrader/0.1"},
    )


async def _call(method: str, payload: dict[str, Any]) -> Any:
    if not configured():
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    async with _client() as client:
        response = await client.post(
            API.format(token=settings.telegram_bot_token, method=method), json=payload
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not body.get("ok"):
        raise TelegramError(
            f"{method} -> {response.status_code}: {body.get('description') or response.text[:200]}"
        )
    return body.get("result")


# --------------------------------------------------------------------------
# chat id
# --------------------------------------------------------------------------


async def chat_id() -> str | None:
    """Configured chat id, or the discovered one. A hit is cached for good."""
    global _chat_cache
    if settings.telegram_chat_id:
        return settings.telegram_chat_id
    at, cached = _chat_cache
    if cached:
        return cached
    if at and time.monotonic() - at < NEGATIVE_CACHE_SECONDS:
        return None
    async with session_scope() as session:
        stored = await repo.get_setting(session, CHAT_KEY, None)
    value = str(stored) if stored else None
    _chat_cache = (time.monotonic(), value)
    return value


def invalidate_chat_cache() -> None:
    global _chat_cache
    _chat_cache = (0.0, None)


async def discover_chat_id() -> str | None:
    """One `getUpdates` poll; the newest chat that wrote to the bot wins.

    Stores the id, tells the user the link works, and writes an audit row.
    Returns the chat id, or None when nobody has written yet.
    """
    global _update_offset
    updates = await _call(
        "getUpdates",
        {"timeout": 0, "offset": _update_offset, "allowed_updates": ["message", "channel_post"]},
    )
    found: tuple[str, str] | None = None
    for update in updates or []:
        _update_offset = int(update.get("update_id", 0)) + 1
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is None:
            continue
        title = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username")
            or ""
        )
        found = (str(chat["id"]), title)
    if found is None:
        return None

    chat, title = found
    async with session_scope() as session:
        await repo.set_setting(session, CHAT_KEY, chat)
        await repo.set_setting(session, CHAT_TITLE_KEY, title)
        await repo.log_event(
            session,
            f"Telegram connected to chat {chat}" + (f" ({title})" if title else ""),
            source="notify",
            data={"chat_id": chat, "title": title},
            notify=False,
        )
    invalidate_chat_cache()
    try:
        await send_text(chat, texts.CONNECTED_TEXT)
    except TelegramError as exc:
        log.warning("telegram welcome message failed: %s", exc)
    log.info("telegram chat id discovered: %s", chat)
    return chat


async def status() -> dict[str, Any]:
    """What the panel shows: configured? which chat? how was it found?"""
    if not configured():
        return {"configured": False, "chat_id": None, "chat_title": None, "source": None}
    if settings.telegram_chat_id:
        return {
            "configured": True, "chat_id": settings.telegram_chat_id,
            "chat_title": None, "source": "env",
        }
    async with session_scope() as session:
        stored = await repo.get_setting(session, CHAT_KEY, None)
        title = await repo.get_setting(session, CHAT_TITLE_KEY, None)
    return {
        "configured": True,
        "chat_id": str(stored) if stored else None,
        "chat_title": title or None,
        "source": "discovered" if stored else None,
    }


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def render(note: "Notification") -> str:
    emoji = texts.CATEGORY_EMOJI.get(note.category, "")
    prefix = "🚨 " if note.level == "error" else ""
    title = html.escape(note.title)
    body = html.escape(note.body)
    text = f"{prefix}{emoji} <b>{title}</b>\n{body}".strip()
    return text[:MAX_TEXT]


async def send_text(chat: str, text: str, silent: bool = False) -> None:
    await _call(
        "sendMessage",
        {
            "chat_id": chat,
            "text": text[:MAX_TEXT],
            "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": True},
        },
    )


async def send(note: "Notification", silent: bool = False) -> str:
    """Deliver one notification. Returns a short status for the caller's log."""
    if not configured():
        return "skipped: no token"
    chat = await chat_id()
    if not chat:
        return "skipped: waiting for /start"
    await send_text(chat, render(note), silent)
    return "sent"
