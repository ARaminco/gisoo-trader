"""Toman exchange rates from the NetArz FX API (https://netarz.ir/fx-api).

Optional and read-only: the panel shows the account's value in Toman next to
USDT. Nothing in the trading path depends on it, so every failure here comes
back as "unavailable" instead of an exception.

Docs: https://netarz.ir/docs/fx · examples: https://github.com/netarz/fx-api-examples
Server calls are checked against the IPs allow-listed on the NetArz app; the
API answers 403 `ip_not_allowed` with the IP it saw, which is passed through
so the panel can tell the user exactly which address to add.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Rates refresh every few minutes upstream; the free plan is delayed anyway.
CACHE_SECONDS = 180

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def configured() -> bool:
    return bool(settings.netarz_fx_key.strip())


async def toman_rate(code: str | None = None) -> dict[str, Any]:
    """{"available": bool, "code", "buy", "sell", "mid", "as_of", "plan", "error", "ip"}.

    Prices are Toman per ONE unit of `code` (the API's `unit` divided out).
    """
    code = (code or settings.netarz_fx_code or "USD").upper()
    if not configured():
        return {"available": False, "code": code, "error": "not_configured"}

    hit = _cache.get(code)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]

    # IPv4 on purpose: the key is locked to the IPv4 address the user listed.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(10.0, connect=5.0),
            # Direct, never through a proxy: the key is locked to this server's IP.
            headers={
                "Authorization": f"Bearer {settings.netarz_fx_key.strip()}",
                "User-Agent": "GisooTrader/0.1",
            },
        ) as client:
            response = await client.get(
                f"{settings.netarz_fx_base_url.rstrip('/')}/rates", params={"codes": code}
            )
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("netarz fx unavailable: %s", exc)
        return {"available": False, "code": code, "error": "unreachable"}

    if not isinstance(body, dict):
        return {"available": False, "code": code, "error": f"http_{response.status_code}"}
    if response.status_code >= 400:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        result = {
            "available": False,
            "code": code,
            "error": error.get("code") or f"http_{response.status_code}",
            "ip": error.get("ip"),
        }
        log.warning("netarz fx refused: %s", result["error"])
        return result

    rows = body.get("data") if isinstance(body.get("data"), list) else []
    row = next((r for r in rows if isinstance(r, dict) and str(r.get("code", "")).upper() == code), None)
    if row is None:
        return {"available": False, "code": code, "error": "unknown_code"}
    try:
        unit = float(row.get("unit") or 1) or 1.0
    except (TypeError, ValueError):
        unit = 1.0

    def per_unit(key: str) -> float | None:
        value = row.get(key)
        return float(value) / unit if value is not None else None

    meta = body.get("meta") or {}
    result = {
        "available": True,
        "code": code,
        "buy": per_unit("buy"),
        "sell": per_unit("sell"),
        "mid": per_unit("mid"),
        "as_of": meta.get("as_of"),
        "plan": meta.get("plan"),
        "delayed_minutes": meta.get("delayed_minutes"),
    }
    _cache[code] = (time.monotonic(), result)
    return result
