"""User notifications: one `emit`, every channel, never in the way of trading.

`emit()` schedules the delivery on the running loop and returns at once; the
delivery task loads the user's preferences, drops duplicates, decides whether
the phone should stay quiet, and fans out to every channel with its own
timeout. A channel that fails logs a warning and nothing else - an order must
never wait for Telegram, and a broken push subscription must never raise
into the trading loop.

`notify_event()` is the hook `repo.log_event` calls, so anything written to
the audit trail reaches the phone with no further plumbing; places with
richer context (a position open/close, the IP watchdog) call `emit` directly
and pass `notify=False` to `log_event` to avoid a double.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
import time
import zoneinfo
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.notify import telegram, texts, webpush

log = logging.getLogger(__name__)

CHANNEL_TIMEOUT = 10.0
DEDUPE_SECONDS = 300
PREFS_KEY = "notify_prefs"
PREFS_TTL = 30.0
LEVELS = ("info", "warn", "error", "trade")

DEFAULT_PREFS: dict[str, Any] = {
    "categories": {category: True for category in texts.CATEGORIES},
    "quiet_hours": {"enabled": False, "start": "23:00", "end": "07:00"},
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Notification:
    category: str
    level: str
    title: str
    body: str
    data: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return hashlib.sha1(f"{self.title}\n{self.body}".encode("utf-8")).hexdigest()

    @property
    def tag(self) -> str:
        """Collapses an identical notification on the device instead of stacking it."""
        return f"gtr-{self.dedupe_key[:12]}"

    @property
    def url(self) -> str:
        """Where a tap lands: an explicit `data["url"]`, else the category's page."""
        explicit = self.data.get("url")
        if isinstance(explicit, str) and explicit.startswith("/"):
            return explicit
        return texts.CATEGORY_URL.get(self.category, "/")


class Deduper:
    """Identical title+body within `window` seconds is sent once.

    Per process and in memory on purpose: a restart is itself something the
    user is told about, and a table would add a write to every event.
    """

    def __init__(self, window: float = DEDUPE_SECONDS) -> None:
        self.window = window
        self._seen: dict[str, float] = {}

    def is_duplicate(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._seen.get(key)
        if last is not None and now - last < self.window:
            return True
        self._seen[key] = now
        if len(self._seen) > 512:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self.window}
        return False


_deduper = Deduper()
_tasks: set[asyncio.Task] = set()
_prefs_cache: tuple[float, dict] = (0.0, {})


# --------------------------------------------------------------------------
# preferences
# --------------------------------------------------------------------------


def normalise_prefs(raw: Any) -> dict:
    """Defaults merged with whatever is stored; bad values fall back quietly."""
    prefs = {
        "categories": dict(DEFAULT_PREFS["categories"]),
        "quiet_hours": dict(DEFAULT_PREFS["quiet_hours"]),
    }
    if not isinstance(raw, dict):
        return prefs
    categories = raw.get("categories")
    if isinstance(categories, dict):
        for category in texts.CATEGORIES:
            if category in categories:
                prefs["categories"][category] = bool(categories[category])
    quiet = raw.get("quiet_hours")
    if isinstance(quiet, dict):
        prefs["quiet_hours"]["enabled"] = bool(quiet.get("enabled", False))
        for key in ("start", "end"):
            value = quiet.get(key)
            if isinstance(value, str) and _HHMM.match(value):
                prefs["quiet_hours"][key] = value
    return prefs


async def load_prefs(force: bool = False) -> dict:
    """Preferences from the settings table, cached for PREFS_TTL per process."""
    global _prefs_cache
    at, prefs = _prefs_cache
    if not force and prefs and time.monotonic() - at < PREFS_TTL:
        return prefs
    try:
        async with session_scope() as session:
            raw = await repo.get_setting(session, PREFS_KEY, None)
    except Exception as exc:  # noqa: BLE001 - the database is not a reason to lose a notification
        log.debug("notification preferences unavailable: %s", exc)
        return prefs or normalise_prefs(None)
    prefs = normalise_prefs(raw)
    _prefs_cache = (time.monotonic(), prefs)
    return prefs


async def save_prefs(session, patch: dict) -> dict:
    """Merge `patch` into the stored preferences and return the result."""
    current = normalise_prefs(await repo.get_setting(session, PREFS_KEY, None))
    merged = {
        "categories": {**current["categories"], **(patch.get("categories") or {})},
        "quiet_hours": {**current["quiet_hours"], **(patch.get("quiet_hours") or {})},
    }
    prefs = normalise_prefs(merged)
    await repo.set_setting(session, PREFS_KEY, prefs)
    invalidate_prefs()
    return prefs


def invalidate_prefs() -> None:
    global _prefs_cache
    _prefs_cache = (0.0, {})


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(prefs: dict, now: dt.datetime | None = None) -> bool:
    """Whether local time (settings.tz) falls inside the user's quiet window."""
    quiet = prefs.get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    try:
        tz = zoneinfo.ZoneInfo(settings.tz or "UTC")
    except Exception:  # noqa: BLE001 - an unknown TZ must not break delivery
        tz = dt.timezone.utc
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    current = local.hour * 60 + local.minute
    start, end = _minutes(quiet["start"]), _minutes(quiet["end"])
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end  # window spans midnight


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------


def emit(category: str, level: str, title: str, body: str, data: dict | None = None) -> asyncio.Task | None:
    """Fire and forget. Safe to call from anywhere on the event loop."""
    note = Notification(
        category if category in texts.CATEGORIES else "system",
        level if level in LEVELS else "info",
        title, body, dict(data or {}),
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("notification dropped, no event loop: %s", title)
        return None
    task = loop.create_task(send(note))
    _tasks.add(task)  # a bare create_task() can be garbage collected
    task.add_done_callback(_tasks.discard)
    return task


async def send(note: Notification, dedupe: bool = True, force: bool = False) -> dict[str, str]:
    """Deliver now and report per channel. Never raises.

    `force` ignores the category switch and quiet hours (the test button).
    """
    try:
        prefs = await load_prefs()
        if not force and not prefs["categories"].get(note.category, True):
            return {"skipped": "category off"}
        if dedupe and _deduper.is_duplicate(note.dedupe_key):
            return {"skipped": "duplicate"}
        # Quiet hours: nothing is lost, the phone just does not buzz - except
        # for an error, which is exactly the thing that must wake the user.
        silent = note.level == "info" or (
            not force and note.level != "error" and in_quiet_hours(prefs)
        )
        results = await asyncio.gather(
            _guard("telegram", telegram.send(note, silent)),
            _guard("push", webpush.send_all(note, silent)),
        )
        return dict(results)
    except Exception as exc:  # noqa: BLE001 - the caller may be the trading loop
        log.warning("notification '%s' failed: %s", note.title, exc)
        return {"error": str(exc)[:200]}


async def _guard(name: str, coro) -> tuple[str, str]:
    """One channel, one timeout, no exception escapes."""
    try:
        return name, str(await asyncio.wait_for(coro, timeout=CHANNEL_TIMEOUT))
    except asyncio.TimeoutError:
        log.warning("%s notification timed out after %ss", name, CHANNEL_TIMEOUT)
        return name, "timeout"
    except Exception as exc:  # noqa: BLE001
        log.warning("%s notification failed: %s", name, exc)
        return name, f"error: {str(exc)[:160]}"


async def flush(timeout: float = 5.0) -> None:
    """Wait for in-flight deliveries (shutdown), bounded."""
    pending = [task for task in _tasks if not task.done()]
    if not pending:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        log.debug("%s notification(s) still pending at shutdown", len(pending))


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------


def notify_event(level: str, source: str, message: str, data: dict | None = None) -> None:
    """The `repo.log_event` hook: classify the row and emit. Never raises."""
    try:
        classified = texts.classify_event(level, source, message, data)
    except Exception as exc:  # noqa: BLE001 - a bad regex must not break the audit trail
        log.debug("could not classify event '%s': %s", message[:80], exc)
        return
    if classified is None:
        return
    emit(classified.category, level, classified.title, classified.body, data)


async def send_test() -> dict[str, str]:
    """A test message on every channel, bypassing preferences and dedupe."""
    note = Notification("system", "info", texts.TEST_TITLE, texts.TEST_BODY)
    results = await asyncio.gather(
        _guard("telegram", telegram.send(note, silent=False)),
        _guard("push", webpush.send_all(note, silent=False)),
    )
    return dict(results)
