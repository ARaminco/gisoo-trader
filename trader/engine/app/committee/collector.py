"""The briefing: everything the committee looks at, as numbers, with no LLM.

`build()` reuses the market-watch builder (app/api/routers/watch.py) for the
regime, the momentum ranks, the armed breakouts, the open positions with
their exit-rule status and the risk gates - the same evaluation the Signals
page shows - and adds what a review needs on top:

  * account      equity, capital base, deployed %, idle USDT, Simple Earn
                 balance, unexplained balance
  * performance  since inception and over the last 7 / 30 days, next to the
                 policy's expectations (win rate 35-50%, full stops ~ -1R,
                 kill switch at 20% closed-equity drawdown, daily 5% pause)
  * health       every protective stop live on the exchange, clock offset, public
                 IP unchanged, API key still "trade only, IP restricted",
                 Simple Earn auto-subscribe off, no unexplained balance,
                 the last 4h evaluation on time, the exchange sweep fresh,
                 the engine heartbeat fresh, notifications deliverable
  * stances      the advisor's market reviews of the last 24 hours

Every exchange call here is a signed GET; nothing is ever sent that could
move money or change the key. Exchange failures never fail the briefing:
the affected check is reported as `ok: null` with the error text.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import statistics
import time
from typing import Any

from app.api import metrics as metrics_mod
from app.api.routers import watch as watch_mod
from app.config import settings
from app.db import repo
from app.db.models import Position
from app.db.session import session_scope
from app.exchange.base import ExchangeClient
from app.exchange.registry import make_client
from app.notify import ipwatch, telegram
from app.strategies import core4h
from app.util.runtime_settings import refresh_runtime_settings
from app.workers import bus

log = logging.getLogger(__name__)

EXCHANGE_TIMEOUT = 25.0
# Binance rejects a signature whose timestamp is more than recvWindow (5 s)
# off; the client alerts at 2 s. The committee flags anything above 1 s.
CLOCK_LIMIT_MS = 1000
# The engine evaluates within a minute or two of a 4h close; a bar closed
# longer ago than this without an evaluation is a miss.
EVALUATION_GRACE_S = 15 * 60
SYNC_STALE_S = 10 * 60
IP_CHANGE_WINDOW = dt.timedelta(hours=24)
STANCE_HISTORY = dt.timedelta(hours=24)
TOP_MOMENTUM = 5
# A breakout counts as "armed" for the minutes when the close is within this
# many ATRs of the 55-bar high (every regime-on symbol is technically armed).
NEAR_BREAKOUT_ATR = 1.5

# What the tests say a healthy live run looks like (docs/risk-policy.md
# sections 1, 2, 4 and 5). The analyst compares the live windows to these.
EXPECTATIONS: dict[str, Any] = {
    "win_rate_min": 0.35,
    "win_rate_max": 0.50,
    "median_winner_pct": 8.0,
    "full_stop_r": -1.0,
    "avg_loss_r_limit": -1.3,
    "stop_if_win_rate_below": 0.25,
    "trades_for_verdict": 30,
    "worst_two_month_window_pct": [-6.0, -4.0],
    "typical_drawdown_pct": [10.0, 16.0],
    "kill_switch_closed_drawdown_pct": settings.max_drawdown_pct,
    "daily_pause_pct": settings.max_daily_loss_pct,
    "max_positions": settings.max_concurrent_positions,
    "risk_scale_in_force": settings.core4h_risk_scale,
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _r(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return round(out, digits)


def _check(
    code: str, label_fa: str, ok: bool | None, severity: str, detail: str, **data: Any
) -> dict:
    return {
        "code": code,
        "label_fa": label_fa,
        "ok": ok,
        "severity": severity,
        "detail": detail[:300],
        "data": data,
    }


async def _guarded(name: str, coro, timeout: float = EXCHANGE_TIMEOUT) -> tuple[Any, str | None]:
    """(result, None) or (None, error text). Never raises."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout), None
    except asyncio.TimeoutError:
        return None, f"{name}: no answer within {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 - a health check must report, not crash
        return None, f"{name}: {str(exc) or type(exc).__name__}"[:200]


# --------------------------------------------------------------------------
# performance
# --------------------------------------------------------------------------


def _window(rows: list[Position]) -> dict:
    """Trade statistics of one window, in the terms the policy uses."""
    if not rows:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": None, "net_pnl": 0.0,
            "avg_r": None, "avg_loss_r": None, "worst_r": None, "median_winner_pct": None,
            "avg_winner_pct": None, "full_stops": 0, "fees": 0.0, "by_family": {},
        }
    wins = [p for p in rows if p.realized_pnl > 0]
    losses = [p for p in rows if p.realized_pnl <= 0]
    winner_pcts = [p.realized_pnl_pct for p in wins]
    loss_rs = [p.r_multiple for p in losses]
    families: dict[str, dict] = {}
    for p in rows:
        family = str((p.meta or {}).get("strategy") or "ensemble")
        entry = families.setdefault(family, {"trades": 0, "wins": 0, "net_pnl": 0.0})
        entry["trades"] += 1
        entry["wins"] += 1 if p.realized_pnl > 0 else 0
        entry["net_pnl"] = round(entry["net_pnl"] + p.realized_pnl, 4)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _r(len(wins) / len(rows), 3),
        "net_pnl": _r(sum(p.realized_pnl for p in rows), 4),
        "avg_r": _r(statistics.fmean(p.r_multiple for p in rows), 3),
        "avg_loss_r": _r(statistics.fmean(loss_rs), 3) if loss_rs else None,
        "worst_r": _r(min(p.r_multiple for p in rows), 3),
        "median_winner_pct": _r(statistics.median(winner_pcts), 3) if winner_pcts else None,
        "avg_winner_pct": _r(statistics.fmean(winner_pcts), 3) if winner_pcts else None,
        "full_stops": sum(1 for p in rows if (p.exit_reason or "") in ("stop_loss", "exchange_stop")),
        "fees": _r(sum(p.fees_paid or 0.0 for p in rows), 4),
        "by_family": families,
    }


# --------------------------------------------------------------------------
# health checks
# --------------------------------------------------------------------------


def _stop_checks(positions: list[dict], open_orders: list[dict] | None, error: str | None) -> dict:
    live = [p for p in positions if p.get("side") == "long"]
    if settings.trading_mode == "paper":
        return _check("exchange_stops", "استاپ‌ها روی صرافی", None, "high",
                      "paper mode: no exchange orders", positions=len(live))
    if not live:
        return _check("exchange_stops", "استاپ‌ها روی صرافی", True, "high",
                      "no open positions", positions=0)
    if open_orders is None:
        return _check("exchange_stops", "استاپ‌ها روی صرافی", None, "high",
                      error or "open orders unavailable", positions=len(live))
    stops = [
        o for o in open_orders
        if str(o.get("type", "")).upper() in ("STOP_LOSS_LIMIT", "STOP_LOSS")
        and str(o.get("side", "")).upper() == "SELL"
    ]
    missing: list[str] = []
    rows: list[dict] = []
    for p in live:
        wanted = str(p.get("stop_order_id") or "")
        match = next((o for o in stops if str(o.get("orderId")) == wanted), None) if wanted else None
        if match is None:
            match = next((o for o in stops if o.get("symbol") == p["symbol"]), None)
        if match is None:
            missing.append(p["symbol"])
            rows.append({"symbol": p["symbol"], "present": False})
            continue
        qty = float(match.get("origQty") or 0.0)
        qty_ok = abs(qty - float(p.get("qty") or 0.0)) <= max(1e-9, float(p.get("qty") or 0.0) * 0.02)
        rows.append({
            "symbol": p["symbol"],
            "present": True,
            "order_id": match.get("orderId"),
            "stop_price": _r(match.get("stopPrice"), 8),
            "qty": qty,
            "qty_ok": qty_ok,
            "matches_record": bool(wanted) and str(match.get("orderId")) == wanted,
        })
        if not qty_ok:
            missing.append(f"{p['symbol']} (qty {qty} vs {p.get('qty')})")
    ok = not missing
    detail = (
        f"{len(live)}/{len(live)} positions protected by a live stop"
        if ok else "no live stop for: " + ", ".join(missing)
    )
    return _check("exchange_stops", "استاپ‌ها روی صرافی", ok, "high", detail,
                  positions=len(live), stops=rows, open_orders=len(open_orders))


def _restrictions_check(data: dict | None, error: str | None) -> dict:
    label = "مجوزهای کلید API"
    if settings.trading_mode == "paper":
        return _check("api_restrictions", label, None, "high", "paper mode: no key")
    if data is None:
        return _check("api_restrictions", label, None, "high", error or "unavailable")
    withdrawals = bool(data.get("enableWithdrawals"))
    ip_restrict = bool(data.get("ipRestrict"))
    spot = bool(data.get("enableSpotAndMarginTrading"))
    futures = bool(data.get("enableFutures"))
    transfer = bool(data.get("permitsUniversalTransfer"))
    problems = []
    if withdrawals:
        problems.append("withdrawals ENABLED")
    if not ip_restrict:
        problems.append("not IP restricted")
    if not spot:
        problems.append("spot trading disabled")
    if futures:
        problems.append("futures enabled")
    if transfer:
        problems.append("universal transfer enabled")
    severity = "high" if (withdrawals or not ip_restrict or not spot) else "medium"
    detail = "trade-only, IP restricted, no withdrawals" if not problems else "; ".join(problems)
    return _check(
        "api_restrictions", label, not problems, severity, detail,
        enable_withdrawals=withdrawals, ip_restrict=ip_restrict,
        enable_spot=spot, enable_futures=futures, permits_universal_transfer=transfer,
    )


def _earn_check(rows: list[dict] | None, error: str | None) -> tuple[dict, float | None, bool | None]:
    """Simple Earn is counted as capital, so what matters is that it can be read.

    Auto-subscribe used to fail this check, on the reasoning that money swept
    out of the spot wallet was money the engine could not trade. The engine now
    values Earn as part of the account and redeems what an entry needs, so the
    sweep itself is harmless. What is not harmless is an Earn book the engine
    cannot read or cannot redeem: the first makes every valuation short by an
    unknown amount, the second strands the capital.
    """
    label = "Simple Earn: قابل خواندن و بازخرید"
    if settings.trading_mode == "paper":
        return _check("earn_readable", label, None, "high", "paper mode: no key"), None, None
    if rows is None:
        return (
            _check("earn_readable", label, False, "high",
                   f"positions unreadable: {error or 'unavailable'} - the account "
                   "cannot be valued in full and the risk gates stand down"),
            None,
            None,
        )
    total = sum(float(r.get("totalAmount") or 0.0) for r in rows)
    auto = any(bool(r.get("autoSubscribe")) for r in rows)
    locked = [r for r in rows if not bool(r.get("canRedeem", True))]
    detail = (
        f"{len(rows)} flexible {settings.quote_asset} position(s), {total:.2f} "
        f"{settings.quote_asset} counted as capital, "
        f"autoSubscribe={'ON' if auto else 'off'}"
        if rows else f"no flexible {settings.quote_asset} position"
    )
    if locked:
        detail += f"; {len(locked)} cannot be redeemed"
    return (
        _check("earn_readable", label, not locked, "high", detail,
               positions=len(rows), total_usdt=_r(total, 4), auto_subscribe=auto,
               not_redeemable=len(locked)),
        _r(total, 4),
        auto,
    )


def _clock_check(offset_ms: int | None, error: str | None) -> dict:
    label = "ساعت سیستم"
    if offset_ms is None:
        return _check("clock", label, None, "medium", error or "unavailable")
    ok = abs(int(offset_ms)) <= CLOCK_LIMIT_MS
    return _check("clock", label, ok, "medium", f"offset {int(offset_ms):+d} ms vs the exchange",
                  offset_ms=int(offset_ms), limit_ms=CLOCK_LIMIT_MS)


def _ip_check(stored: dict, fresh: str | None, now: dt.datetime) -> dict:
    label = "IP عمومی"
    current = stored.get("public_ip")
    changed_at = stored.get("public_ip_changed_at")
    recent_change = False
    if changed_at:
        try:
            stamp = _aware(dt.datetime.fromisoformat(str(changed_at)))
            recent_change = stamp is not None and now - stamp < IP_CHANGE_WINDOW
        except ValueError:
            recent_change = False
    if fresh and current and fresh != current:
        return _check("public_ip", label, False, "high",
                      f"address changed: {current} -> {fresh} (the key's allow-list must follow)",
                      current=fresh, previous=current)
    if recent_change:
        return _check("public_ip", label, False, "high",
                      f"address changed in the last 24h ({stored.get('public_ip_previous')} -> {current})",
                      current=current, previous=stored.get("public_ip_previous"), changed_at=changed_at)
    if not fresh and not current:
        return _check("public_ip", label, None, "high", "public address unknown")
    return _check("public_ip", label, True, "high", f"{fresh or current} unchanged",
                  current=fresh or current, changed_at=changed_at)


def _evaluation_check(last_open_ms: int | None, now: dt.datetime) -> dict:
    label = "ارزیابی ۴ساعته"
    bar_ms = watch_mod.BAR_MS
    now_ms = int(now.timestamp() * 1000)
    # The newest bar whose close is at least EVALUATION_GRACE_S old.
    expected_open = (now_ms - EVALUATION_GRACE_S * 1000) // bar_ms * bar_ms - bar_ms
    if last_open_ms is None:
        return _check("last_evaluation", label, False, "medium", "the engine has not evaluated any 4h bar yet",
                      expected_bar=watch_mod._iso(expected_open))
    ok = int(last_open_ms) >= expected_open
    behind = int((expected_open - int(last_open_ms)) // bar_ms) if not ok else 0
    detail = (
        f"last evaluated bar {watch_mod._iso(int(last_open_ms))}"
        + ("" if ok else f" - {behind} bar(s) behind {watch_mod._iso(expected_open)}")
    )
    return _check("last_evaluation", label, ok, "medium", detail,
                  last_evaluated_bar=watch_mod._iso(int(last_open_ms)),
                  expected_bar=watch_mod._iso(expected_open), bars_behind=behind)


def _sync_check(sync, now: dt.datetime) -> dict:
    label = "همگام‌سازی با صرافی"
    if settings.trading_mode == "paper":
        return _check("exchange_sync", label, None, "medium", "paper mode")
    last = _aware(sync.last_rest_sync)
    age = (now - last).total_seconds() if last else None
    ok = bool(sync.connected) and age is not None and age <= SYNC_STALE_S
    detail = (
        f"connected, last sweep {int(age)}s ago" if ok
        else f"connected={bool(sync.connected)}, last sweep {'never' if age is None else f'{int(age)}s ago'}"
        + (f", last error: {sync.last_error[:120]}" if sync.last_error else "")
    )
    return _check("exchange_sync", label, ok, "medium", detail,
                  connected=bool(sync.connected), age_s=None if age is None else int(age))


def _engine_check(engine: dict) -> dict:
    label = "موتور معامله"
    online = bool(engine.get("online"))
    age = engine.get("heartbeat_age_s")
    detail = f"heartbeat {age}s old" if age is not None else "no heartbeat"
    return _check("engine_online", label, online, "high", detail, heartbeat_age_s=age)


async def _notify_check(session, last_meeting) -> dict:
    label = "اعلان‌ها"
    tg = telegram.configured() and bool(await telegram.chat_id())
    devices = await repo.count_push_subscriptions(session)
    configured = tg or devices > 0
    delivery = dict(last_meeting.delivery or {}) if last_meeting is not None else {}
    delivered = any(str(v).startswith("sent") for v in delivery.values())
    if not configured:
        return _check("notifications", label, False, "medium",
                      "no channel: Telegram not ready and no push device", telegram=tg, push_devices=devices)
    if last_meeting is None or not delivery:
        return _check("notifications", label, True, "medium",
                      f"channels ready (telegram={'yes' if tg else 'no'}, push devices={devices}); no delivery recorded yet",
                      telegram=tg, push_devices=devices)
    detail = ", ".join(f"{k}: {v}" for k, v in delivery.items())
    return _check("notifications", label, delivered, "medium",
                  ("last minutes delivered - " if delivered else "last minutes NOT delivered - ") + detail,
                  telegram=tg, push_devices=devices, last_delivery=delivery)


# --------------------------------------------------------------------------
# the briefing
# --------------------------------------------------------------------------


async def _exchange_facts(client: ExchangeClient | None) -> dict:
    """The four signed reads, each guarded; {} in paper mode."""
    if settings.trading_mode == "paper":
        return {}
    own = client is None
    client = client or make_client()
    try:
        if not client.has_credentials:
            return {"error": "no API key configured"}
        offset, offset_err = await _guarded("time", client.sync_time())
        orders, orders_err = await _guarded("open orders", client.open_orders())
        facts = {
            "exchange": client.title,
            "exchange_fa": client.title_fa,
            "stop_orders_supported": client.supports_stop_orders,
            "offset_ms": offset, "offset_error": offset_err,
            "open_orders": orders, "open_orders_error": orders_err,
        }
        # Binance-only reads: absent elsewhere, so their checks are left out.
        if client.supports_key_permissions:
            restrictions, restrictions_err = await _guarded("api restrictions", client.api_restrictions())
            facts.update(restrictions=restrictions, restrictions_error=restrictions_err)
        if client.supports_earn:
            earn, earn_err = await _guarded(
                "simple earn", client.simple_earn_flexible_position(settings.quote_asset)
            )
            facts.update(earn=earn, earn_error=earn_err)
        return facts
    finally:
        if own:
            await client.aclose()


def _compact_positions(watch_positions: list[dict], db_positions: list[Position]) -> list[dict]:
    stop_ids = {p.id: (p.meta or {}).get("stop_order_id") for p in db_positions}
    out = []
    for row in watch_positions:
        exit_rule = dict(row.get("exit") or {})
        out.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "family": row.get("family"),
            "side": row.get("side"),
            "qty": row.get("qty"),
            "entry_price": row.get("entry_price"),
            "mark": row.get("mark"),
            "stop_price": row.get("stop_price"),
            "stop_distance_pct": _r(row.get("stop_distance_pct"), 2),
            "notional": _r(row.get("notional"), 2),
            "unrealized_pnl": _r(row.get("unrealized_pnl"), 4),
            "unrealized_pct": _r(row.get("unrealized_pct"), 2),
            "opened_at": _iso(_aware(row.get("opened_at"))) if isinstance(row.get("opened_at"), dt.datetime) else row.get("opened_at"),
            "exit_rule": exit_rule.get("rule"),
            "exit_now": bool(exit_rule.get("exit_now")),
            "exit_reason": exit_rule.get("exit_reason") or "",
            "rank": exit_rule.get("rank"),
            "hold_n": exit_rule.get("hold_n"),
            "at_risk": bool(exit_rule.get("at_risk")),
            "exit_low": exit_rule.get("exit_low"),
            "exit_low_distance_pct": _r(exit_rule.get("distance_pct"), 2),
            "regime_on": bool(exit_rule.get("regime_on", True)),
            "stop_order_id": stop_ids.get(row["id"]),
        })
    return out


def _compact_market(watch: dict) -> dict:
    regime = watch.get("market_regime") or {}
    momentum = [
        {
            "symbol": m["symbol"], "rank": m.get("rank"), "momentum_pct": _r(m.get("momentum_pct"), 2),
            "ret_7d_pct": _r(m.get("ret_7d_pct"), 2), "ret_1d_pct": _r(m.get("ret_1d_pct"), 2),
            "close": m.get("close"), "held": m.get("held"), "tradable": m.get("tradable"),
            "in_top3": m.get("in_top3"), "exit_now": m.get("exit_now"),
        }
        for m in watch.get("momentum", [])
        if not m.get("stale") and m.get("rank") is not None
    ][:TOP_MOMENTUM]
    breakouts = [
        {
            "symbol": b["symbol"], "distance_pct": _r(b.get("distance_pct"), 2),
            "distance_atr": _r(b.get("distance_atr"), 2), "adx": b.get("adx"),
            "entry_high": b.get("entry_high"), "close": b.get("close"),
            "held": b.get("held"), "triggered": b.get("triggered"),
        }
        for b in watch.get("breakouts", [])
        if b.get("armed") and not b.get("stale")
        and (b.get("triggered") or (b.get("distance_atr") is not None and b["distance_atr"] <= NEAR_BREAKOUT_ATR))
    ][:TOP_MOMENTUM]
    return {
        "btc_regime": {
            "regime_on": regime.get("regime_on"),
            "ema_rising": regime.get("ema_rising"),
            "close": regime.get("close"),
            "ema200": regime.get("ema200"),
            "distance_pct": _r(regime.get("distance_pct"), 2),
            "regime_bars": regime.get("regime_bars"),
            "regime_since": regime.get("regime_since"),
        } if regime else None,
        "evaluated_bar": watch.get("evaluated_bar"),
        "data_lag_bars": watch.get("data_lag_bars"),
        "next_evaluation_at": watch.get("next_evaluation_at"),
        "next_rebalance_at": watch.get("next_rebalance_at"),
        "top_momentum": momentum,
        "armed_breakouts": breakouts,
        "candidates": watch.get("candidates", []),
        "universe": len(watch.get("ranked") or []),
    }


def _compact_gates(gates: dict | None) -> dict | None:
    if not gates:
        return None
    return {
        "ready": gates.get("ready"),
        "blocking": gates.get("blocking", []),
        "armed": gates.get("armed"),
        "kill_switch": gates.get("kill_switch"),
        "paused_until": gates.get("paused_until"),
        "session_block": gates.get("session_block"),
        "open_positions": gates.get("open_positions"),
        "max_positions": gates.get("max_positions"),
        "drawdown_pct": _r(gates.get("drawdown_pct"), 2),
        "max_drawdown_pct": gates.get("max_drawdown_pct"),
        "today_pnl_pct": _r(gates.get("today_pnl_pct"), 2),
        "max_daily_loss_pct": gates.get("max_daily_loss_pct"),
        "funds_ok": gates.get("funds_ok"),
        "cooldowns": gates.get("cooldowns", {}),
    }


async def build(client: ExchangeClient | None = None) -> dict:
    """The briefing dict. Read-only everywhere; never raises on exchange trouble."""
    started = time.monotonic()
    now = _now()

    watch = await watch_mod.watch(_="committee")
    facts = await _exchange_facts(client)
    marks = await bus.get_marks()
    status = await bus.get_status()
    fresh_ip, _ = await _guarded("public ip", ipwatch.fetch_public_ip(), timeout=12.0)

    async with session_scope() as session:
        await refresh_runtime_settings(session)
        sync = await repo.get_sync_state(session)
        db_positions = await repo.open_positions(session)
        equity, free = await watch_mod._equity_and_free(session, marks)
        summary = await metrics_mod.performance_summary(session, equity, free, marks)
        all_closed = await repo.closed_positions(session)
        closed_7d = [p for p in all_closed if _aware(p.closed_at) and _aware(p.closed_at) >= now - dt.timedelta(days=7)]
        closed_30d = [p for p in all_closed if _aware(p.closed_at) and _aware(p.closed_at) >= now - dt.timedelta(days=30)]
        first_trade = _aware(all_closed[0].closed_at) if all_closed else None
        pauses_30d = await repo.count_events_like(
            session, "Daily loss limit hit%", now - dt.timedelta(days=30), source="risk"
        )
        stance_rows = await repo.llm_reviews_since(session, now - STANCE_HISTORY, "market")
        last_meeting = await repo.latest_committee_meeting(session)
        last_eval = await repo.get_setting(session, watch_mod.CORE_LAST_BAR_KEY, None)
        ip_stored = await ipwatch.current(session)
        notify_check = await _notify_check(session, last_meeting)
        risk_scale = float(await repo.get_setting(session, "core4h_risk_scale", settings.core4h_risk_scale))
        capital_base = await repo.capital_base(session)

    positions = _compact_positions(watch.get("positions", []), db_positions)
    open_notional = sum((p.get("mark") or p.get("entry_price") or 0.0) * (p.get("qty") or 0.0) for p in positions)
    earn_check, earn_total, earn_auto = _earn_check(facts.get("earn"), facts.get("earn_error"))
    live_trading = settings.trading_mode != "paper"
    # Exchanges without resting stop orders: the engine itself is the floor,
    # which _engine_check below already watches.
    stop_check = (
        [_stop_checks(positions, facts.get("open_orders"), facts.get("open_orders_error"))]
        if not live_trading or facts.get("stop_orders_supported", True) else []
    )
    key_check = (
        [_restrictions_check(facts.get("restrictions"), facts.get("restrictions_error"))]
        if not live_trading or "restrictions" in facts or "restrictions_error" in facts else []
    )
    earn_checks = [earn_check] if not live_trading or "earn" in facts or "earn_error" in facts else []

    checks = [
        *stop_check,
        _clock_check(facts.get("offset_ms"), facts.get("offset_error")),
        _ip_check(ip_stored, fresh_ip, now),
        *key_check,
        *earn_checks,
        _check(
            "unexplained_balance", "موجودی توضیح‌داده‌نشده",
            float(sync.balance_unexplained or 0.0) == 0.0, "medium",
            "0.00" if not sync.balance_unexplained else
            f"{float(sync.balance_unexplained):+.2f} {settings.quote_asset} since {_iso(_aware(sync.balance_unexplained_since))}",
            amount=_r(sync.balance_unexplained, 4),
        ),
        _evaluation_check(int(last_eval) if last_eval else None, now),
        _sync_check(sync, now),
        _engine_check(watch.get("engine") or {}),
        notify_check,
    ]
    if facts.get("error"):
        checks.insert(0, _check("api_key", "کلید API", False, "high", facts["error"]))
    failed = [c["code"] for c in checks if c["ok"] is False]
    unknown = [c["code"] for c in checks if c["ok"] is None]

    scheduler_status = status.get("scheduler") or {}
    llm_status = scheduler_status.get("llm") or {}

    return {
        "generated_at": _iso(now),
        "tz": settings.tz,
        "mode": settings.trading_mode,
        "core": settings.strategy_core,
        "build_ms": int((time.monotonic() - started) * 1000),
        "account": {
            "equity": _r(equity, 4),
            "closed_equity": _r(summary["closed_equity"], 4),
            "capital_base": _r(capital_base, 4),
            "idle_usdt": _r(free, 4),
            "open_notional": _r(open_notional, 4),
            "deployed_pct": _r(open_notional / equity * 100, 2) if equity else 0.0,
            "unrealized_pnl": _r(summary["unrealized_pnl"], 4),
            "total_pnl": _r(summary["total_pnl"], 4),
            "total_pnl_pct": _r(summary["total_pnl_pct"], 2),
            "today_pnl_pct": _r(summary["today_pnl_pct"], 2),
            "peak_equity": _r(summary["peak_equity"], 4),
            "drawdown_pct": _r(summary["drawdown_pct"], 2),
            "closed_peak_equity": _r(summary["closed_peak_equity"], 4),
            "closed_drawdown_pct": _r(summary["closed_drawdown_pct"], 2),
            "unexplained_balance": _r(sync.balance_unexplained, 4),
            "earn_usdt": earn_total,
            "earn_auto_subscribe": earn_auto,
            "balances": {k: _r(v, 6) for k, v in (sync.balances or {}).items()},
        },
        "positions": positions,
        "market": _compact_market(watch),
        "gates": _compact_gates(watch.get("gates")),
        "settings": {
            "armed": (watch.get("gates") or {}).get("armed"),
            "core4h_risk_scale": risk_scale,
            "max_positions": settings.max_concurrent_positions,
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "max_drawdown_pct": settings.max_drawdown_pct,
            "families": {
                f.name: {"risk_pct": _r(f.risk_pct * risk_scale, 3), "atr_stop": f.atr_stop}
                for f in core4h.FAMILIES.values()
            },
            "llm": {
                "enabled": llm_status.get("enabled"),
                "provider": llm_status.get("provider"),
                "fallback": llm_status.get("fallback_provider"),
                "calls_today": llm_status.get("calls_today"),
                "last_error": llm_status.get("last_error"),
            },
            "committee_hours_utc": list(settings.committee_hours_utc),
        },
        "performance": {
            "since": _iso(first_trade),
            "inception": _window(all_closed),
            "last_7d": _window(closed_7d),
            "last_30d": _window(closed_30d),
            "daily_pauses_30d": pauses_30d,
            "expectations": {**EXPECTATIONS, "risk_scale_in_force": risk_scale},
        },
        "health": {
            "ok": not failed,
            "failed": failed,
            "unknown": unknown,
            "checks": checks,
        },
        "stance_history": [
            {
                "at": _iso(_aware(r.created_at)),
                "stance": r.stance,
                "risk_multiplier": _r(r.risk_multiplier, 2),
                "confidence": _r(r.confidence, 2),
                "source": (r.payload or {}).get("source", "advisor"),
                "reasoning": (r.reasoning or "")[:160],
            }
            for r in stance_rows
        ],
        "last_meeting": {
            "id": last_meeting.id,
            "held_at": _iso(_aware(last_meeting.held_at)),
            "reason": last_meeting.reason,
            "provider": last_meeting.provider,
            "health_ok": last_meeting.health_ok,
        } if last_meeting is not None else None,
    }
