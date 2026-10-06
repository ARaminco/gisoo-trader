"""Redis-backed state behind the login system.

- brute-force lockouts (per client IP and per username)
- access-token revocation (logout) and the global token version (logout-all)
- replay guard for one-time codes
- pending passkey (WebAuthn) challenges
- the client IP, trusting X-Forwarded-For only from the Caddy proxy
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import time

from fastapi import HTTPException, Request, status

from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.workers import bus

log = logging.getLogger(__name__)

PREFIX = "gtr:auth:"
TOKEN_VERSION_SETTING = "token_version"  # key in the settings table
_TOKEN_VERSION_CACHE = PREFIX + "token_version"

UNAVAILABLE_DETAIL = "سرویس احراز هویت در دسترس نیست؛ کمی بعد دوباره تلاش کنید"


class LoginLocked(Exception):
    """Raised when the IP or the username is inside a lockout window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


def _unavailable(exc: Exception) -> HTTPException:
    log.error("auth store (redis) unavailable: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
    )


def _user_ident(username: str) -> str:
    # Attacker-controlled text never becomes a raw key: hash and bound it.
    return hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()[:32]


def _scopes(ip: str, username: str) -> list[tuple[str, str]]:
    return [("ip", ip), ("user", _user_ident(username))]


# --------------------------------------------------------------------------
# lockouts
# --------------------------------------------------------------------------


async def lock_remaining(ip: str, username: str) -> int:
    """Seconds left on the longest active lock for this IP / username, or 0."""
    r = bus.get_redis()
    try:
        ttls = await asyncio.gather(
            *(r.ttl(f"{PREFIX}lock:{scope}:{ident}") for scope, ident in _scopes(ip, username))
        )
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    # ttl is -2 for a missing key and -1 for a key without expiry
    return max(0, *(int(t) for t in ttls))


async def ensure_not_locked(ip: str, username: str) -> None:
    remaining = await lock_remaining(ip, username)
    if remaining > 0:
        raise LoginLocked(remaining)


async def _count_failure(scope: str, ident: str) -> int:
    r = bus.get_redis()
    fail_key = f"{PREFIX}fail:{scope}:{ident}"
    lock_key = f"{PREFIX}lock:{scope}:{ident}"
    count_key = f"{PREFIX}lockcount:{scope}:{ident}"
    base = settings.login_lockout_minutes * 60
    cap = settings.login_lockout_max_hours * 3600

    failures = await r.incr(fail_key)
    if failures == 1:
        await r.expire(fail_key, base)  # the failure window
    if failures < settings.login_max_failures:
        return 0

    lockouts = await r.incr(count_key)
    await r.expire(count_key, cap)  # escalation memory outlives the lock itself
    duration = min(base * (2 ** (lockouts - 1)), cap)
    await r.set(lock_key, "1", ex=duration)
    await r.delete(fail_key)
    return duration


async def record_failure(ip: str, username: str) -> int:
    """Count one failed attempt. Returns the lock length in seconds if this
    attempt triggered a lockout, else 0."""
    try:
        results = [
            await _count_failure(scope, ident) for scope, ident in _scopes(ip, username)
        ]
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    return max(results)


async def clear_failures(ip: str, username: str) -> None:
    r = bus.get_redis()
    try:
        await r.delete(
            *(f"{PREFIX}fail:{scope}:{ident}" for scope, ident in _scopes(ip, username))
        )
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


# --------------------------------------------------------------------------
# token revocation / version
# --------------------------------------------------------------------------


async def revoke_token(jti: str, ttl_seconds: int) -> None:
    """Reject `jti` until the token would have expired anyway."""
    if ttl_seconds <= 0:
        return
    try:
        await bus.get_redis().set(f"{PREFIX}revoked:{jti}", "1", ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


async def is_revoked(jti: str) -> bool:
    try:
        return bool(await bus.get_redis().exists(f"{PREFIX}revoked:{jti}"))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


async def get_token_version() -> int:
    """Current token version: Redis cache first, the settings table behind it."""
    r = bus.get_redis()
    try:
        cached = await r.get(_TOKEN_VERSION_CACHE)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    if cached is not None:
        return int(cached)

    async with session_scope() as session:
        version = await repo.get_setting(session, TOKEN_VERSION_SETTING, None)
        if version is None:
            version = 1
            await repo.set_setting(session, TOKEN_VERSION_SETTING, version)
    version = int(version)
    await cache_token_version(version)
    return version


async def bump_token_version(session) -> int:
    """Write version+1 to the settings table. Call cache_token_version() after
    the transaction commits so the cache never runs ahead of the database."""
    current = int(await repo.get_setting(session, TOKEN_VERSION_SETTING, 1) or 1)
    await repo.set_setting(session, TOKEN_VERSION_SETTING, current + 1)
    return current + 1


async def cache_token_version(version: int) -> None:
    try:
        await bus.get_redis().set(_TOKEN_VERSION_CACHE, int(version))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


# --------------------------------------------------------------------------
# one-time code replay guard
# --------------------------------------------------------------------------


async def mark_totp_used(username: str, code: str) -> bool:
    """True if this code was not seen recently (and is now recorded)."""
    key = f"{PREFIX}totp_used:{_user_ident(username)}:{code}"
    try:
        return bool(await bus.get_redis().set(key, "1", ex=120, nx=True))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


# --------------------------------------------------------------------------
# passkey ceremony state
# --------------------------------------------------------------------------

PASSKEY_STATE_TTL = 300


def new_state_id() -> str:
    """Handle of one pending WebAuthn ceremony; the browser hands it back with the answer."""
    return secrets.token_urlsafe(24)


async def store_passkey_state(kind: str, state_id: str, data: dict) -> None:
    """Park a challenge (plus RP ID / username) for `kind` = register | login."""
    key = f"{PREFIX}passkey:{kind}:{state_id}"
    try:
        await bus.get_redis().set(key, json.dumps(data), ex=PASSKEY_STATE_TTL, nx=True)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


async def pop_passkey_state(kind: str, state_id: str) -> dict | None:
    """Fetch and delete in one transaction: a challenge is answered once."""
    key = f"{PREFIX}passkey:{kind}:{state_id}"
    try:
        async with bus.get_redis().pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            value, _ = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    if not value:
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# client IP behind the proxy
# --------------------------------------------------------------------------

_RESOLVE_TTL = 30.0
_resolved: dict[str, tuple[float, frozenset[str]]] = {}


def _resolve_host(host: str) -> frozenset[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return frozenset()
    return frozenset(info[4][0] for info in infos)


async def _trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in settings.trusted_proxies:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass  # not an IP or CIDR - a hostname such as "caddy"
        cached = _resolved.get(entry)
        now = time.monotonic()
        if cached is None or now - cached[0] > _RESOLVE_TTL:
            addresses = await asyncio.to_thread(_resolve_host, entry)
            _resolved[entry] = (now, addresses)
        else:
            addresses = cached[1]
        for address in addresses:
            try:
                networks.append(ipaddress.ip_network(address))
            except ValueError:
                continue
    return networks


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


async def is_trusted_proxy(peer: str) -> bool:
    address = _parse_ip(peer)
    if address is None:
        return False
    return any(address in network for network in await _trusted_networks())


async def client_ip(request: Request) -> str:
    """The real client address.

    X-Forwarded-For is honoured only when the direct peer is a trusted proxy,
    and then only its LAST entry - the one the proxy itself appended. Anything
    before it was supplied by the client and can say whatever it likes.
    """
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and await is_trusted_proxy(peer):
        candidate = _parse_ip(forwarded.split(",")[-1])
        if candidate is not None:
            return str(candidate)
    parsed = _parse_ip(peer)
    return str(parsed) if parsed is not None else peer
