"""Runtime settings propagation.

The panel persists edits in the `settings` table and applies them to the API
process only. Every worker holds its own copy of the `settings` singleton, so
the engine and the scheduler call `refresh_runtime_settings` on startup and
from their heartbeat, and pick an override up within one heartbeat (20 s).

`symbols` is deliberately not on the list: the universe is validated and the
kline streams are subscribed at startup, so a change there needs a restart
(as does anything not in the panel's editable set).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo

log = logging.getLogger(__name__)

# Keep in step with app.api.schemas.SettingsPatch (minus `symbols`).
RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "risk_per_trade_pct", "max_concurrent_positions", "max_daily_loss_pct",
        "max_drawdown_pct", "min_signal_score", "min_expected_move_pct",
        "max_position_pct", "atr_stop_mult", "atr_target_mult", "trailing_enabled",
        "cooldown_minutes_after_loss", "ml_enabled", "ml_min_probability",
        "llm_enabled", "llm_can_veto", "llm_review_minutes",
        "futures_enabled", "futures_leverage",
        "min_atr_ratio", "skip_weekend_entries", "core4h_risk_scale",
    }
)


def _coerce(key: str, value: Any) -> Any:
    """Cast a stored JSON value to the type of the field it overrides."""
    current = getattr(settings, key)
    if isinstance(current, bool):  # before int: bool is an int subclass
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(value)
    return value if current is None else type(current)(value)


async def refresh_runtime_settings(session: AsyncSession) -> list[str]:
    """Apply panel overrides from the settings table onto this process.

    Returns the keys whose value changed, so the caller can log them.
    """
    stored = await repo.all_settings(session)
    changed: list[str] = []
    for key in sorted(RUNTIME_KEYS):
        if key not in stored or stored[key] is None or not hasattr(settings, key):
            continue
        try:
            value = _coerce(key, stored[key])
        except (TypeError, ValueError) as exc:
            log.warning("ignoring runtime setting %s=%r: %s", key, stored[key], exc)
            continue
        if getattr(settings, key) != value:
            # Same mechanism the API uses: bypass validation on the frozen-ish
            # pydantic instance, the panel already validated the value.
            object.__setattr__(settings, key, value)
            changed.append(key)
    return changed
