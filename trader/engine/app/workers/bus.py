"""Redis pub/sub used to move events between the workers and the API.

Channels:
  gtr:ticks      latest mark price per symbol (also cached as a hash)
  gtr:candles    a candle closed - the trader loop wakes up on this
  gtr:events     anything the panel should show live
  gtr:commands   panel -> engine (close a position, re-arm, flatten all)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger(__name__)

CH_TICKS = "gtr:ticks"
CH_CANDLES = "gtr:candles"
CH_EVENTS = "gtr:events"
CH_COMMANDS = "gtr:commands"

KEY_MARKS = "gtr:marks"
KEY_STATUS = "gtr:status"

_redis: aioredis.Redis | None = None
_closed = False  # set by close_redis(); subscribers stop reconnecting


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _redis


async def publish(channel: str, payload: dict[str, Any]) -> None:
    try:
        await get_redis().publish(channel, json.dumps(payload, default=str))
    except Exception as exc:  # noqa: BLE001 - the bus must never break trading
        log.warning("publish to %s failed: %s", channel, exc)


async def subscribe(*channels: str) -> AsyncIterator[tuple[str, dict]]:
    """Yield (channel, payload), reconnecting on failure until close_redis()."""
    while not _closed:
        pubsub = None
        try:
            pubsub = get_redis().pubsub()
            await pubsub.subscribe(*channels)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    yield message["channel"], json.loads(message["data"])
                except (json.JSONDecodeError, KeyError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _closed:
                return
            log.warning("subscription dropped: %s", exc)
            await asyncio.sleep(3)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001 - already gone
                    pass


async def set_mark(symbol: str, price: float) -> None:
    try:
        await get_redis().hset(KEY_MARKS, symbol, price)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_mark failed: %s", exc)


async def get_marks() -> dict[str, float]:
    try:
        raw = await get_redis().hgetall(KEY_MARKS)
        return {k: float(v) for k, v in raw.items()}
    except Exception as exc:  # noqa: BLE001
        log.debug("get_marks failed: %s", exc)
        return {}


async def set_status(component: str, payload: dict) -> None:
    try:
        await get_redis().hset(KEY_STATUS, component, json.dumps(payload, default=str))
        await get_redis().expire(KEY_STATUS, 600)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_status failed: %s", exc)


async def get_status() -> dict[str, dict]:
    try:
        raw = await get_redis().hgetall(KEY_STATUS)
        out = {}
        for key, value in raw.items():
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("get_status failed: %s", exc)
        return {}


async def close_redis() -> None:
    global _redis, _closed
    _closed = True
    if _redis is not None:
        await _redis.aclose()
    _redis = None
