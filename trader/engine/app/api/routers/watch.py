"""What the 4h core is watching right now: GET /api/watch.

The Signals page needs to answer one question the raw `signals` rows never
can: "what is the bot looking at, and why did it do nothing?". This router
evaluates the universe on the last CLOSED 4h bar with the pure rule
functions the engine runs (app/strategies/core4h.py) - the forming bar is
never read, so nothing here looks ahead of the engine - and adds:

  * the timing of the next evaluation (every 4h close; the daily momentum
    rebalance is the bar that OPENS at 00:00 UTC, i.e. the 04:00 UTC close),
  * the BTC regime with a short sparkline,
  * the momentum ranking and the turtle channels for every symbol,
  * the open positions with the exit rule of the family that opened them,
  * the risk gates exactly as the risk manager would apply them right now,
  * the last signal rows, normalised for display.

The evaluation is cached in Redis for CACHE_TTL seconds under a key that
carries the latest stored 4h open time, so the panel's 30 s polling costs at
most one feature build per minute and a new bar is picked up as soon as the
market-data worker stores it. Everything that moves between bars - gates,
marks, positions, recent signals - is read on every request; those are
cheap queries.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import time
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api import metrics as metrics_mod
from app.api.auth import current_user
from app.api.schemas import SignalOut
from app.config import CORE4H_TIMEFRAME, MARKET_GATE_SYMBOL, settings
from app.db import repo
from app.db.models import Candle, Position
from app.db.session import session_scope
from app.features.build import build_features
from app.risk.manager import RiskManager, position_family
from app.strategies import core4h
from app.util.runtime_settings import refresh_runtime_settings
from app.util.timeframes import tf_seconds
from app.workers import bus

log = logging.getLogger(__name__)

router = APIRouter(tags=["watch"])

BAR_MS = tf_seconds(CORE4H_TIMEFRAME) * 1000
# Bars loaded per ranked symbol. The momentum lookback needs 43, the turtle
# channel 56 and the ATR 15; 400 keeps a symbol's own EMA200 within a
# fraction of a percent of the engine's full-history value.
HISTORY_BARS = 400
# The BTC gate decides everything, so it is read over the history the engine
# loads: an EMA200 seeded only 400 bars back still carries ~2% of its seed.
HISTORY_BARS_MARKET = max(HISTORY_BARS, settings.candle_history)
CACHE_TTL = 60
CACHE_KEY = "gtr:watch:v1:{open_time}"
# Exchange filters (min notional / lot size) for the sizing hint. Fetched in
# the background at most every FILTERS_TTL; a failure is remembered for
# FILTERS_FAIL_TTL so an unreachable exchange never slows the page down.
FILTERS_KEY = "gtr:watch:filters"
FILTERS_TTL = 12 * 3600
FILTERS_FAIL_TTL = 600
DEFAULT_MIN_NOTIONAL = 5.0
# Written by the engine after every evaluation (app/workers/trader.py,
# CORE_LAST_BAR_KEY). The trader module is not imported: it would pull the
# exchange client and the LLM advisor into the API process.
CORE_LAST_BAR_KEY = "core4h_last_open_time"
ENGINE_STALE_SECONDS = 120
SPARK_POINTS = 10
RECENT_SIGNALS = 30

_filters_task: asyncio.Task | None = None


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.timezone.utc).isoformat()


def _f(value: Any, digits: int = 6) -> float | None:
    """JSON-safe float: None for None / NaN / inf, native float otherwise."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return round(out, digits)


def _pct_of(value: float | None, base: float | None, digits: int = 3) -> float | None:
    if value is None or not base:
        return None
    return _f((value - base) / base * 100, digits)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# candles stored per symbol
# --------------------------------------------------------------------------


async def _bars_stored(session) -> tuple[dict[str, dict], dict[str, tuple[int, int]]]:
    """(display rows, {symbol: (count, latest open_time)}) for the 4h universe."""
    stmt = (
        select(Candle.symbol, func.count(), func.max(Candle.open_time))
        .where(Candle.timeframe == CORE4H_TIMEFRAME, Candle.symbol.in_(settings.data_symbols))
        .group_by(Candle.symbol)
    )
    found = {
        str(symbol): (int(count), int(latest))
        for symbol, count, latest in (await session.execute(stmt)).all()
    }
    rows = {}
    for symbol in settings.data_symbols:
        count, latest = found.get(symbol, (0, None))
        rows[symbol] = {
            "bars": count,
            "latest_open_time": _iso(latest),
            "ready": count >= core4h.MIN_BARS,
        }
    return rows, found


# --------------------------------------------------------------------------
# the cached evaluation of the last closed bar
# --------------------------------------------------------------------------


def _build_all(candles: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """CPU-bound feature build for every symbol; runs in a worker thread."""
    frames: dict[str, pd.DataFrame] = {}
    for symbol, df in candles.items():
        try:
            frames[symbol] = build_features(df)
        except ValueError as exc:
            log.warning("%s: features failed: %s", symbol, exc)
    return frames


def _regime_block(btc: pd.DataFrame, view: core4h.Universe, open_time: int) -> dict:
    open_times = btc["open_time"].to_numpy()
    pos = int(np.searchsorted(open_times, open_time))
    pos = min(pos, len(btc) - 1)
    window = btc.iloc[max(0, pos - SPARK_POINTS + 1): pos + 1]

    # How long the gate has been in its current state (bars and since when).
    regime = core4h.regime_series(btc)
    states = regime.to_numpy(dtype=bool)
    index = regime.index.to_numpy()
    r_pos = int(np.searchsorted(index, open_time))
    r_pos = min(r_pos, len(states) - 1)
    first = r_pos
    while first > 0 and states[first - 1] == states[r_pos]:
        first -= 1

    return {
        "symbol": MARKET_GATE_SYMBOL,
        "close": _f(view.market_close, 2),
        "ema200": _f(view.market_ema200, 2),
        "distance_pct": _pct_of(view.market_close, view.market_ema200),
        "regime_on": bool(view.regime_ok),
        "ema_rising": bool(view.regime_rising),
        "regime_bars": int(r_pos - first + 1),
        "regime_since": _iso(int(index[first])),
        "sparkline": [_f(v, 2) for v in window["close"].tolist()],
        "ema_sparkline": [_f(v, 2) for v in window["ema_200"].tolist()],
        "sparkline_open_times": [_iso(int(v)) for v in window["open_time"].tolist()],
    }


async def _compute_market(open_time: int) -> dict:
    """Evaluate the universe on the bar that opened at `open_time`.

    Mirrors `TradingEngine._load_core_features` + `core4h.evaluate_universe`:
    symbols with fewer than MIN_BARS candles stay out, the others are ranked
    together, and without the BTC bar there is no view at all.
    """
    async with session_scope() as session:
        candles = {
            symbol: await repo.load_candles(
                session,
                symbol,
                CORE4H_TIMEFRAME,
                HISTORY_BARS_MARKET if symbol == MARKET_GATE_SYMBOL else HISTORY_BARS,
            )
            for symbol in settings.data_symbols
        }
    usable = {s: df for s, df in candles.items() if len(df) >= core4h.MIN_BARS}
    frames = await asyncio.to_thread(_build_all, usable)
    view = core4h.evaluate_universe(frames, open_time, MARKET_GATE_SYMBOL)

    result: dict[str, Any] = {
        "open_time": open_time,
        "bar_open_time": _iso(open_time),
        "bar_close_time": _iso(open_time + BAR_MS),
        "computed_at": _now_iso(),
        "market_present": bool(view.market_present),
        "ranked": list(view.ranked),
        "regime": None,
        "momentum": [],
        "breakouts": [],
        "symbols": {},
        "candidates": [],
    }
    if not view.market_present:
        return result

    result["regime"] = _regime_block(frames[MARKET_GATE_SYMBOL], view, open_time)

    # The two legs of the momentum score, with the panel's own alignment:
    # returns are measured in bars on the union of every symbol's open times.
    closes = {
        symbol: f.drop_duplicates("open_time").set_index("open_time")["close"]
        for symbol, f in frames.items()
    }
    panel = pd.DataFrame(closes).sort_index()
    bars_per_day = int(core4h.DAY_MS / BAR_MS)
    ret_long = panel / panel.shift(core4h.LOOKBACK_DAYS * bars_per_day) - 1
    ret_short = panel / panel.shift(core4h.SKIP_DAYS * bars_per_day) - 1
    on_bar = open_time in panel.index

    active = set(settings.active_symbols)
    momentum_rows: list[dict] = []
    breakout_rows: list[dict] = []
    symbols: dict[str, dict] = {}

    for symbol in frames:
        m = view.signal(core4h.MOMENTUM.name, symbol)
        t = view.signal(core4h.TURTLE.name, symbol)
        tradable = symbol in active
        if m is None or t is None:
            # No bar for this open time yet (the worker has not stored it):
            # it still took part in the others' ranking, as in the engine.
            momentum_rows.append({"symbol": symbol, "stale": True, "tradable": tradable, "rank": None})
            breakout_rows.append({"symbol": symbol, "stale": True, "tradable": tradable})
            continue

        close, atr = m.price, m.atr
        d = m.detail
        rank = d.get("rank")
        rank_int = int(rank) if rank is not None else None
        in_top3 = rank_int is not None and rank_int <= core4h.TOP_N
        in_top5 = rank_int is not None and rank_int <= core4h.HOLD_N
        momentum_rows.append(
            {
                "symbol": symbol,
                "stale": False,
                "tradable": tradable,
                "rank": rank_int,
                "universe": int(d.get("universe") or len(frames)),
                "ret_7d_pct": _f(ret_long.at[open_time, symbol] * 100, 3) if on_bar else None,
                "ret_1d_pct": _f(ret_short.at[open_time, symbol] * 100, 3) if on_bar else None,
                "momentum_pct": _f(d.get("momentum_pct"), 3),
                "in_top3": in_top3,
                "in_top5": in_top5,
                # The score momentum_arrays gives the top three: 1.0 / 0.9 / 0.8.
                "signal_score": _f(1.0 - (rank_int - 1) * 0.1, 2) if in_top3 else 0.0,
                "close": _f(close),
                "atr": _f(atr),
                "atr_pct": _pct_of(close + atr, close) if atr > 0 else None,
                "stop_price": _f(close - core4h.MOMENTUM.atr_stop * atr) if atr > 0 else None,
                "signal_now": m.side == "long",
                "exit_now": bool(m.exit),
                "exit_reason": m.exit_reason,
            }
        )

        td = t.detail
        entry_high = td.get("entry_high")
        exit_low = td.get("exit_low")
        market_on = bool(td.get("market_on"))
        above_ema = bool(td.get("above_ema200"))
        breakout_rows.append(
            {
                "symbol": symbol,
                "stale": False,
                "tradable": tradable,
                "close": _f(close),
                "atr": _f(atr),
                "atr_pct": _pct_of(close + atr, close) if atr > 0 else None,
                "entry_high": _f(entry_high),
                "exit_low": _f(exit_low),
                # Positive: the close is this far below the breakout level.
                "distance_pct": _pct_of(entry_high, close) if entry_high is not None else None,
                "distance_atr": _f((entry_high - close) / atr, 3) if entry_high is not None and atr > 0 else None,
                "exit_low_distance_pct": _pct_of(close, exit_low) if exit_low else None,
                "above_ema200": above_ema,
                "market_on": market_on,
                "adx": _f(td.get("adx"), 1),
                # Every entry condition but the close above the channel high.
                "armed": bool(market_on and above_ema and atr > 0 and entry_high is not None),
                "triggered": bool(entry_high is not None and close > entry_high),
                "signal_now": t.side == "long",
                "score": _f(t.score, 3),
                "stop_price": _f(close - core4h.TURTLE.atr_stop * atr) if atr > 0 else None,
                "exit_now": bool(t.exit),
                "exit_reason": t.exit_reason,
            }
        )

        symbols[symbol] = {
            "close": _f(close),
            "atr": _f(atr),
            "rank": rank_int,
            "universe": int(d.get("universe") or len(frames)),
            "btc_ok": bool(d.get("btc_ok")),
            "entry_high": _f(entry_high),
            "exit_low": _f(exit_low),
            "momentum_exit": bool(m.exit),
            "momentum_exit_reason": m.exit_reason,
            "turtle_exit": bool(t.exit),
            "turtle_exit_reason": t.exit_reason,
        }

    momentum_rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["symbol"]))
    breakout_rows.sort(
        key=lambda r: (
            r.get("distance_pct") is None,
            r.get("distance_pct") if r.get("distance_pct") is not None else 0.0,
            r["symbol"],
        )
    )
    result["momentum"] = momentum_rows
    result["breakouts"] = breakout_rows
    result["symbols"] = symbols
    result["candidates"] = [
        {
            "family": c.family,
            "symbol": c.symbol,
            "score": _f(c.score, 3),
            "price": _f(c.price),
            "stop_price": _f(c.stop_price),
        }
        for c in view.candidates()
    ]
    return result


async def _cached_market(open_time: int) -> dict:
    key = CACHE_KEY.format(open_time=open_time)
    try:
        raw = await bus.get_redis().get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - the cache is an optimisation
        log.debug("watch cache read failed: %s", exc)
    started = time.monotonic()
    market = await _compute_market(open_time)
    market["compute_ms"] = int((time.monotonic() - started) * 1000)
    try:
        await bus.get_redis().set(key, json.dumps(market), ex=CACHE_TTL)
    except Exception as exc:  # noqa: BLE001
        log.debug("watch cache write failed: %s", exc)
    return market


# --------------------------------------------------------------------------
# exchange filters (background, long-lived cache)
# --------------------------------------------------------------------------


async def _refresh_filters() -> None:
    """Fetch min notional / lot size for the universe into Redis."""
    from app.exchange.registry import make_client
    from app.exchange.symbols import SymbolRegistry

    payload: dict[str, dict] = {}
    ttl = FILTERS_FAIL_TTL
    client = make_client()
    try:
        registry = SymbolRegistry(client)
        await asyncio.wait_for(registry.refresh("spot"), timeout=20)
        for symbol in settings.data_symbols:
            try:
                info = await registry.get(symbol)
            except KeyError:
                continue
            payload[symbol] = {
                "min_notional": info.min_notional,
                "min_qty": info.min_qty,
                "step_size": info.step_size,
            }
        ttl = FILTERS_TTL
    except Exception as exc:  # noqa: BLE001 - the hint falls back to the default
        log.info("exchange filters unavailable for the watch page: %s", exc)
    finally:
        await client.aclose()
    try:
        await bus.get_redis().set(FILTERS_KEY, json.dumps(payload), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        log.debug("filters cache write failed: %s", exc)


async def _symbol_filters() -> dict[str, dict]:
    """Cached filters, or {} while the first background fetch runs."""
    global _filters_task
    try:
        raw = await bus.get_redis().get(FILTERS_KEY)
        if raw is not None:
            return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.debug("filters cache read failed: %s", exc)
        return {}
    if _filters_task is None or _filters_task.done():
        _filters_task = asyncio.create_task(_refresh_filters())
    return {}


# --------------------------------------------------------------------------
# account, gates, positions
# --------------------------------------------------------------------------


async def _equity_and_free(session, marks: dict[str, float]) -> tuple[float, float]:
    """(equity, free quote balance) the way /api/overview values the account."""
    if settings.trading_mode == "paper":
        from app.execution.executor import PaperExecutor

        return await PaperExecutor().account_equity(session, marks)

    sync = await repo.get_sync_state(session)
    balances = sync.balances or {}
    quote = settings.quote_asset
    equity = float(balances.get(quote, 0.0))
    for asset, amount in balances.items():
        if asset == quote:
            continue
        mark = marks.get(f"{asset}{quote}")
        if mark:
            equity += float(amount) * mark
    free = float(balances.get(quote, 0.0))
    if equity <= 0:
        latest = await repo.latest_equity(session)
        equity = latest.equity if latest else settings.initial_capital_usdt
        free = latest.free_balance if latest else equity
    return equity, free


def _sizing(
    family: core4h.Family,
    rows: list[dict],
    equity: float,
    free: float,
    filters: dict[str, dict],
) -> dict | None:
    """The smallest order the family would size at the current ATRs.

    Same arithmetic as RiskManager.evaluate_entry (risk / stop distance,
    capped by the family's position cap and the free balance), including the
    fallback to the exchange minimum when it stays inside 1.6x the risk
    budget. Lot rounding is skipped: this is a hint, not the gate.
    """
    worst: dict | None = None
    for row in rows:
        price, atr = row.get("close"), row.get("atr")
        if not row.get("tradable") or not price or not atr:
            continue
        stop_distance = family.atr_stop * atr
        risk_amount = equity * family.risk_pct / 100
        cap = min(equity * family.max_position_pct / 100, free * 0.98)
        notional = min(risk_amount / stop_distance * price, cap)
        min_notional = float(filters.get(row["symbol"], {}).get("min_notional", DEFAULT_MIN_NOTIONAL))
        ok = notional >= min_notional
        if not ok:
            at_minimum = min_notional * 1.02
            implied_risk = at_minimum / price * stop_distance
            ok = implied_risk <= risk_amount * 1.6 and at_minimum <= cap
        if worst is None or notional < worst["notional"]:
            worst = {
                "family": family.name,
                "symbol": row["symbol"],
                "notional": round(notional, 2),
                "min_notional": min_notional,
                "risk_amount": round(risk_amount, 3),
                "ok": bool(ok),
            }
    return worst


async def _gates(
    session,
    marks: dict[str, float],
    positions: list[Position],
    market: dict,
    engine: dict,
) -> dict:
    """Every gate RiskManager.check_global / evaluate_entry would apply now."""
    now = dt.datetime.now(dt.timezone.utc)
    kill = bool(await repo.get_setting(session, "kill_switch", False))
    armed = bool(await repo.get_setting(session, "trading_armed", settings.trading_armed))
    paused_until = await repo.get_setting(session, "paused_until", None)
    paused_active = False
    paused_text = ""
    if paused_until:
        try:
            until = dt.datetime.fromisoformat(str(paused_until))
            if until.tzinfo is None:
                until = until.replace(tzinfo=dt.timezone.utc)
            paused_active = until > now
            paused_text = f"paused until {until:%Y-%m-%d %H:%M} UTC"
        except ValueError:
            pass

    equity, free = await _equity_and_free(session, marks)
    summary = await metrics_mod.performance_summary(session, equity, free, marks)
    session_block = RiskManager._session_blocked(now)
    min_equity = max(settings.initial_capital_usdt * 0.15, 12.0)
    max_positions = int(settings.max_concurrent_positions)
    open_count = len(positions)
    filters = await _symbol_filters()
    tradable_filters = [
        float(filters.get(s, {}).get("min_notional", DEFAULT_MIN_NOTIONAL))
        for s in settings.active_symbols
    ]
    min_notional = max(tradable_filters) if tradable_filters else DEFAULT_MIN_NOTIONAL
    drawdown = (
        summary["closed_drawdown_pct"] if settings.core_is_portfolio4h else summary["drawdown_pct"]
    )

    cooldowns: dict[str, int] = {}
    for symbol in settings.active_symbols:
        # Only the turtle family has a cooldown, and since the engine scopes it
        # to that family's own losses (a momentum loss no longer blocks a
        # breakout entry), this page must read it the same way.
        remaining = await RiskManager._cooldown_remaining(
            session, symbol, core4h.TURTLE.cooldown_min, core4h.TURTLE.name
        )
        if remaining:
            cooldowns[symbol] = int(remaining)

    blocking: list[dict] = []
    if kill:
        blocking.append({"code": "kill_switch", "detail": "kill switch engaged"})
    if settings.trading_mode == "live" and not armed:
        blocking.append({"code": "not_armed", "detail": "live trading not armed"})
    if paused_active:
        blocking.append({"code": "paused", "detail": paused_text})
    if drawdown >= settings.max_drawdown_pct:
        blocking.append(
            {"code": "drawdown", "detail": f"drawdown {drawdown:.1f}% reached the {settings.max_drawdown_pct}% limit"}
        )
    if summary["today_pnl_pct"] <= -settings.max_daily_loss_pct:
        blocking.append(
            {"code": "daily_loss", "detail": f"daily loss {summary['today_pnl_pct']:.2f}% hit the limit"}
        )
    if open_count >= max_positions:
        blocking.append(
            {"code": "max_positions", "detail": f"{open_count} open positions (max {max_positions})"}
        )
    if equity < min_equity:
        blocking.append(
            {"code": "min_equity", "detail": f"equity {equity:.2f} below working minimum {min_equity:.2f}"}
        )
    if session_block:
        blocking.append({"code": "session", "detail": session_block})
    if free * 0.98 < min_notional * 1.02:
        blocking.append(
            {"code": "funds", "detail": f"free balance {free:.2f} below the exchange minimum {min_notional:.2f}"}
        )
    if not market.get("market_present"):
        blocking.append({"code": "data", "detail": "waiting for candle history"})
    if not engine.get("online"):
        blocking.append({"code": "engine_offline", "detail": "trading engine heartbeat is stale"})

    return {
        "ready": not blocking,
        "blocking": blocking,
        "armed": armed,
        "mode": settings.trading_mode,
        "kill_switch": kill,
        "paused_until": paused_until if paused_active else None,
        "session_block": session_block or None,
        "skip_weekend_entries": bool(settings.skip_weekend_entries),
        "skip_entry_hours_utc": list(settings.skip_entry_hours_utc),
        "open_positions": open_count,
        "max_positions": max_positions,
        "equity": round(equity, 4),
        "free_balance": round(free, 4),
        "min_equity": round(min_equity, 2),
        "min_notional": min_notional,
        "min_notional_source": "exchange" if filters else "default",
        "funds_ok": free * 0.98 >= min_notional * 1.02,
        "drawdown_pct": drawdown,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "today_pnl_pct": summary["today_pnl_pct"],
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "cooldowns": cooldowns,
        "sizing": [
            s
            for s in (
                _sizing(core4h.MOMENTUM, market.get("momentum", []), equity, free, filters),
                _sizing(core4h.TURTLE, market.get("breakouts", []), equity, free, filters),
            )
            if s is not None
        ],
    }


def _position_row(position: Position, marks: dict[str, float], market: dict) -> dict:
    family = position_family(position)
    detail = (market.get("symbols") or {}).get(position.symbol, {})
    regime = market.get("regime") or {}
    mark = marks.get(position.symbol) or detail.get("close") or position.entry_price
    direction = 1 if position.side == "long" else -1
    unrealized = (mark - position.entry_price) * position.qty * direction
    basis = position.entry_price * position.qty
    regime_on = bool(regime.get("regime_on", True))

    exit_rule: dict[str, Any]
    if family == core4h.MOMENTUM.name:
        rank = detail.get("rank")
        exit_rule = {
            "rule": "rank",
            "rank": rank,
            "universe": detail.get("universe"),
            "hold_n": core4h.HOLD_N,
            "at_risk": rank is not None and rank > core4h.HOLD_N,
            "regime_on": regime_on,
            "exit_now": bool(detail.get("momentum_exit")),
            "exit_reason": detail.get("momentum_exit_reason") or "",
        }
    elif family == core4h.TURTLE.name:
        exit_low = detail.get("exit_low")
        exit_rule = {
            "rule": "channel",
            "exit_low": exit_low,
            "distance_pct": _pct_of(mark, exit_low) if exit_low else None,
            "regime_on": regime_on,
            "exit_now": bool(detail.get("turtle_exit")),
            "exit_reason": detail.get("turtle_exit_reason") or "",
        }
    else:
        exit_rule = {"rule": "stop", "regime_on": regime_on, "exit_now": False, "exit_reason": ""}

    return {
        "id": position.id,
        "symbol": position.symbol,
        "family": family,
        "side": position.side,
        "qty": position.qty,
        "entry_price": position.entry_price,
        "stop_price": position.stop_price,
        "mark": _f(mark),
        "notional": position.notional,
        "unrealized_pnl": round(unrealized, 4),
        "unrealized_pct": round(unrealized / basis * 100, 3) if basis else 0.0,
        "stop_distance_pct": _pct_of(mark, position.stop_price) if position.stop_price else None,
        "opened_at": position.opened_at,
        "exit": exit_rule,
    }


def _signal_row(row) -> dict:
    out = SignalOut.model_validate(row, from_attributes=True).model_dump()
    contributions = row.contributions or {}
    family = contributions.get("family", {}).get("reason") if isinstance(contributions, dict) else None
    out["family"] = family if family in core4h.FAMILIES else None
    out["timeframe"] = row.timeframe
    out["atr"] = row.atr
    out["suggested_stop"] = row.suggested_stop
    return out


def _engine_block(status: dict) -> dict:
    engine = status.get("engine", {}) or {}
    age: float | None = None
    at = engine.get("at")
    if at:
        try:
            stamp = dt.datetime.fromisoformat(str(at))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
        except ValueError:
            age = None
    core = engine.get("core4h") or {}
    if isinstance(core.get("bar"), (int, float)):
        core = {**core, "bar_iso": _iso(int(core["bar"]))}
    return {
        "online": age is not None and age <= ENGINE_STALE_SECONDS,
        "heartbeat_age_s": None if age is None else int(age),
        "core": engine.get("core"),
        "core4h": core,
    }


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


@router.get("/api/watch")
async def watch(_: str = Depends(current_user)) -> dict:
    now_ms = int(time.time() * 1000)
    current_open = now_ms // BAR_MS * BAR_MS
    next_close = current_open + BAR_MS
    wall_last_closed = current_open - BAR_MS
    rebalance_open = current_open
    while rebalance_open % core4h.DAY_MS:
        rebalance_open += BAR_MS

    marks = await bus.get_marks()
    status = await bus.get_status()
    engine = _engine_block(status)

    async with session_scope() as session:
        await refresh_runtime_settings(session)
        bars, found = await _bars_stored(session)
        last_evaluated = await repo.get_setting(session, CORE_LAST_BAR_KEY, None)
        positions = await repo.open_positions(session)
        recent_rows = await repo.recent_signals(session, RECENT_SIGNALS)

    timing = {
        "core": settings.strategy_core,
        "is_portfolio4h": settings.core_is_portfolio4h,
        "mode": settings.trading_mode,
        "now": _iso(now_ms),
        "next_evaluation_at": _iso(next_close),
        "seconds_to_next": max(0, (next_close - now_ms) // 1000),
        # The bar closing next opened at 00:00 UTC: the daily momentum rebalance.
        "is_rebalance_bar": current_open % core4h.DAY_MS == 0,
        "next_rebalance_at": _iso(rebalance_open + BAR_MS),
        "last_evaluated_bar": _iso(int(last_evaluated)) if last_evaluated else None,
        "last_evaluated_close": _iso(int(last_evaluated) + BAR_MS) if last_evaluated else None,
        "min_bars": core4h.MIN_BARS,
        "bars_stored": bars,
        "engine": engine,
        "families": {
            f.name: {
                "risk_pct": f.risk_pct,
                "max_position_pct": f.max_position_pct,
                "atr_stop": f.atr_stop,
                "cooldown_min": f.cooldown_min,
            }
            for f in core4h.FAMILIES.values()
        },
        "recent": [_signal_row(r) for r in recent_rows],
    }

    if not settings.core_is_portfolio4h:
        return {
            **timing,
            "evaluated_bar": None,
            "data_lag_bars": None,
            "market_regime": None,
            "momentum": [],
            "breakouts": [],
            "candidates": [],
            "positions": [_position_row(p, marks, {}) for p in positions],
            "gates": None,
        }

    latest_btc = found.get(MARKET_GATE_SYMBOL, (0, None))[1]
    # Only closed candles are ever stored, so the latest BTC bar is a closed
    # one; the clock cap only matters if the worker's clock runs ahead.
    open_time = min(latest_btc, wall_last_closed) if latest_btc else 0
    market = await _cached_market(open_time) if open_time else {"market_present": False}

    held = {p.symbol: position_family(p) for p in positions}
    momentum = [{**row, "held": held.get(row["symbol"])} for row in market.get("momentum", [])]
    breakouts = [
        {**row, "held": held.get(row["symbol"]), "mark": _f(marks.get(row["symbol"]))}
        for row in market.get("breakouts", [])
    ]

    async with session_scope() as session:
        gates = await _gates(session, marks, positions, market, engine)

    return {
        **timing,
        "evaluated_bar": market.get("bar_open_time"),
        "evaluated_bar_close": market.get("bar_close_time"),
        "data_lag_bars": int((wall_last_closed - open_time) // BAR_MS) if open_time else None,
        "computed_at": market.get("computed_at"),
        "compute_ms": market.get("compute_ms"),
        "history_ready": bool(market.get("market_present")),
        "ranked": market.get("ranked", []),
        "market_regime": market.get("regime"),
        "momentum": momentum,
        "breakouts": breakouts,
        "candidates": market.get("candidates", []),
        "positions": [_position_row(p, marks, market) for p in positions],
        "gates": gates,
    }
