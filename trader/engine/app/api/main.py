"""FastAPI application - everything the PWA panel talks to."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api import metrics as metrics_mod
from app.api import security
from app.api.auth import current_user, decode_token
from app.api.passwords import looks_like_bcrypt
from app.api.routers.auth import router as auth_router
from app.api.routers.committee import router as committee_router
from app.api.routers.notify import router as notify_router
from app.api.routers.passkeys import router as passkeys_router
from app.api.routers.watch import router as watch_router
from app.api.schemas import (
    ArmRequest, BacktestRequest, BacktestResponse, CapitalFlowIn, CapitalFlowOut,
    CommandRequest, EquityPoint, EventOut, OverviewResponse, PositionOut,
    SettingsPatch, SignalOut,
)
from app.config import CORE4H_TIMEFRAME, MARKET_GATE_SYMBOL, settings
from app.db import repo
from app.db.models import Position
from app.db.session import dispose_db, init_db, session_scope
from app.exchange import fx as fx_mod
from app.exchange import registry as exchange_registry
from app.notify import ipwatch, webpush
from app.notify import service as notify
from app.strategies import core4h
from app.util.logging_setup import setup_logging
from app.workers import bus

log = setup_logging("api")

RUNTIME_SETTING_KEYS = set(SettingsPatch.model_fields.keys())

DEFAULT_PANEL_PASSWORD = "change-me-now"
DEFAULT_JWT_SECRET = "change-me"


def _security_warnings() -> list[str]:
    """Configuration that must not survive into a panel reachable from outside."""
    warnings = []
    if settings.panel_password_hash:
        if not looks_like_bcrypt(settings.panel_password_hash):
            warnings.append(
                "PANEL_PASSWORD_HASH is not a bcrypt hash - every login will fail. "
                "Generate one with: python -m app.api.hash_password"
            )
    elif settings.panel_password in ("", DEFAULT_PANEL_PASSWORD):
        warnings.append(
            f"PANEL_PASSWORD is still the default '{DEFAULT_PANEL_PASSWORD}': every login is refused. "
            "Set PANEL_PASSWORD_HASH (python -m app.api.hash_password) or a real PANEL_PASSWORD."
        )
    if settings.jwt_secret in (DEFAULT_JWT_SECRET, "change-me-to-a-long-random-string"):
        warnings.append("JWT_SECRET is still the default: no session token is issued until it is changed.")
    elif len(settings.jwt_secret) < 32:
        warnings.append("JWT_SECRET is shorter than 32 characters: no session token is issued until it is longer.")
    return warnings


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with session_scope() as session:
        await repo.get_sync_state(session)
        stored = await repo.get_setting(session, "trading_armed", None)
        if stored is None:
            await repo.set_setting(session, "trading_armed", settings.trading_armed)
        for message in _security_warnings():
            log.warning(message)
            await repo.log_event(session, message, level="warn", source="auth")
        # The one process that may generate the Web Push key pair (see notify.webpush).
        await webpush.ensure_vapid_keys(session)
    try:
        await security.get_token_version()  # seeds the version and warms the cache
    except HTTPException as exc:
        log.warning("token version not seeded at startup: %s", exc.detail)
    log.info("api ready (mode=%s)", settings.trading_mode)
    yield
    await notify.flush()
    await bus.close_redis()
    await dispose_db()


app = FastAPI(
    title="Gisoo Trader API",
    version="0.1.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# Caddy serves the panel and the API from one origin, so cross-origin access is
# off unless PANEL_ORIGINS lists the browser origins that need it.
if settings.panel_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.panel_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth_router)
app.include_router(passkeys_router)
app.include_router(notify_router)
app.include_router(watch_router)
app.include_router(committee_router)


@app.exception_handler(security.LoginLocked)
async def _locked_handler(_: Request, exc: security.LoginLocked) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=423,
        content={
            "detail": "به دلیل تلاش‌های ناموفق زیاد، ورود موقتاً قفل شده است",
            "retry_after_seconds": exc.retry_after,
        },
        headers={"Retry-After": str(exc.retry_after)},
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _effective_settings(session) -> dict:
    """Environment defaults with runtime overrides applied on top."""
    base = {key: getattr(settings, key, None) for key in RUNTIME_SETTING_KEYS}
    stored = await repo.all_settings(session)
    for key in RUNTIME_SETTING_KEYS:
        if key in stored and stored[key] is not None:
            base[key] = stored[key]
    return base


def _position_out(position: Position, mark: float | None) -> PositionOut:
    is_open = position.status == "open"
    direction = 1 if position.side == "long" else -1
    unrealized = 0.0
    unrealized_pct = 0.0
    if is_open and mark:
        unrealized = (mark - position.entry_price) * position.qty * direction
        basis = position.entry_price * position.qty
        unrealized_pct = unrealized / basis * 100 if basis else 0.0

    # Peak open gain and give-back. The high-water mark is maintained at bar
    # close (long: highest high, short: lowest low); an open position also
    # counts the current mark so the peak never lags the live price.
    entry = position.entry_price or 0.0
    high_water = position.high_water or 0.0
    peak_pct = 0.0
    giveback_pct = 0.0
    if entry > 0:
        candidates = [entry]
        if high_water > 0:
            candidates.append(high_water)
        if is_open and mark:
            candidates.append(mark)
        peak_price = max(candidates) if direction == 1 else min(candidates)
        # `or 0.0` folds the -0.0 a losing short would otherwise produce.
        peak_pct = ((peak_price / entry - 1) * 100 * direction) or 0.0
        if is_open:
            current_pct: float | None = unrealized_pct if mark else None
        else:
            current_pct = position.realized_pnl_pct or 0.0
        if peak_pct > 0 and current_pct is not None:
            giveback_pct = max(peak_pct - current_pct, 0.0)

    return PositionOut(
        id=position.id,
        symbol=position.symbol,
        side=position.side,
        market=position.market,
        status=position.status,
        qty=position.qty,
        entry_price=position.entry_price,
        exit_price=position.exit_price,
        mark_price=mark,
        stop_price=position.stop_price,
        target_price=position.target_price,
        notional=position.notional,
        unrealized_pnl=round(unrealized, 4),
        unrealized_pct=round(unrealized_pct, 3),
        realized_pnl=position.realized_pnl,
        realized_pnl_pct=position.realized_pnl_pct,
        r_multiple=position.r_multiple,
        fees_paid=position.fees_paid,
        trail_active=position.trail_active,
        pending_exit_reason=position.pending_exit_reason,
        stop_missing=bool(position.stop_missing),
        high_water=high_water,
        peak_pct=round(peak_pct, 3),
        giveback_pct=round(giveback_pct, 3),
        exit_reason=position.exit_reason,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        meta=position.meta or {},
    )


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "mode": settings.trading_mode}


@app.get("/api/fx")
async def toman_rate(_: str = Depends(current_user)) -> dict:
    """Toman per one unit of NETARZ_FX_CODE (default USD) from the NetArz FX API.

    Optional and display-only; `available` is false when no key is set or the
    API refused (the `error` / `ip` fields say why).
    """
    return await fx_mod.toman_rate()


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@app.get("/api/overview", response_model=OverviewResponse)
async def overview(_: str = Depends(current_user)) -> OverviewResponse:
    marks = await bus.get_marks()
    status = await bus.get_status()

    async with session_scope() as session:
        from app.execution.executor import PaperExecutor

        if settings.trading_mode == "paper":
            equity, free = await PaperExecutor().account_equity(session, marks)
        else:
            sync = await repo.get_sync_state(session)
            balances = sync.balances or {}
            equity = balances.get(settings.quote_asset, 0.0)
            for asset, amount in balances.items():
                if asset == settings.quote_asset:
                    continue
                mark = marks.get(f"{asset}{settings.quote_asset}")
                if mark:
                    equity += amount * mark
            free = balances.get(settings.quote_asset, 0.0)
            if equity <= 0:
                latest = await repo.latest_equity(session)
                equity = latest.equity if latest else settings.initial_capital_usdt
                free = latest.free_balance if latest else equity

        summary = await metrics_mod.performance_summary(session, equity, free, marks)
        effective = await _effective_settings(session)
        kill = bool(await repo.get_setting(session, "kill_switch", False))
        armed = bool(await repo.get_setting(session, "trading_armed", settings.trading_armed))
        paused = await repo.get_setting(session, "paused_until", None)
        sync_state = await repo.get_sync_state(session)

    engine_status = status.get("engine", {})

    return OverviewResponse(
        mode=settings.trading_mode,
        strategy_core=settings.strategy_core,
        armed=armed,
        kill_switch=kill,
        paused_until=paused,
        max_positions=int(effective.get("max_concurrent_positions") or settings.max_concurrent_positions),
        market_stance=engine_status.get("market_stance", "neutral"),
        risk_multiplier=float(engine_status.get("risk_multiplier", 1.0)),
        exchange_connected=sync_state.connected,
        stream_connected=sync_state.stream_connected,
        last_sync=sync_state.last_rest_sync.isoformat() if sync_state.last_rest_sync else None,
        workers=status,
        **summary,
    )


@app.get("/api/positions", response_model=list[PositionOut])
async def positions(
    status_filter: str = Query("open", alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    _: str = Depends(current_user),
) -> list[PositionOut]:
    marks = await bus.get_marks()
    async with session_scope() as session:
        if status_filter == "open":
            rows = await repo.open_positions(session)
        else:
            rows = await repo.recent_positions(session, limit)
            if status_filter != "all":
                rows = [r for r in rows if r.status == status_filter]
    return [_position_out(r, marks.get(r.symbol)) for r in rows]


@app.get("/api/signals", response_model=list[SignalOut])
async def signals(
    limit: int = Query(60, ge=1, le=500), _: str = Depends(current_user)
) -> list[SignalOut]:
    async with session_scope() as session:
        rows = await repo.recent_signals(session, limit)
    return [SignalOut.model_validate(r, from_attributes=True) for r in rows]


@app.get("/api/equity", response_model=list[EquityPoint])
async def equity(
    hours: int = Query(72, ge=1, le=24 * 90), _: str = Depends(current_user)
) -> list[EquityPoint]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with session_scope() as session:
        rows = await repo.equity_curve(session, since, limit=3000)
    return [
        EquityPoint(
            t=r.taken_at,
            equity=r.equity,
            drawdown_pct=r.drawdown_pct,
            unrealized_pnl=r.unrealized_pnl,
        )
        for r in rows
    ]


@app.get("/api/events", response_model=list[EventOut])
async def events(
    limit: int = Query(150, ge=1, le=1000),
    level: str | None = None,
    _: str = Depends(current_user),
) -> list[EventOut]:
    async with session_scope() as session:
        rows = await repo.recent_events(session, limit, level)
    return [EventOut.model_validate(r, from_attributes=True) for r in rows]


@app.get("/api/stats")
async def stats(days: int = Query(30, ge=1, le=365), _: str = Depends(current_user)) -> dict:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    async with session_scope() as session:
        window = await metrics_mod.trade_stats(session, since)
        overall = await metrics_mod.trade_stats(session)
        closed = await repo.closed_positions(session)

    by_symbol: dict[str, dict] = {}
    for position in closed:
        entry = by_symbol.setdefault(
            position.symbol, {"trades": 0, "wins": 0, "pnl": 0.0}
        )
        entry["trades"] += 1
        entry["wins"] += 1 if position.realized_pnl > 0 else 0
        entry["pnl"] += position.realized_pnl

    by_reason: dict[str, dict] = {}
    for position in closed:
        key = position.exit_reason or "unknown"
        entry = by_reason.setdefault(key, {"count": 0, "pnl": 0.0})
        entry["count"] += 1
        entry["pnl"] += position.realized_pnl

    return {
        "window_days": days,
        "window": window,
        "overall": overall,
        "by_symbol": by_symbol,
        "by_exit_reason": by_reason,
    }


# --------------------------------------------------------------------------
# capital
# --------------------------------------------------------------------------


@app.get("/api/capital", response_model=list[CapitalFlowOut])
async def capital_history(_: str = Depends(current_user)) -> list[CapitalFlowOut]:
    async with session_scope() as session:
        rows = await repo.capital_flows(session, 200)
    return [CapitalFlowOut.model_validate(r, from_attributes=True) for r in rows]


@app.post("/api/capital", response_model=CapitalFlowOut)
async def add_capital(
    payload: CapitalFlowIn, user: str = Depends(current_user)
) -> CapitalFlowOut:
    """Record money added to (or taken out of) the trading account.

    This does not move funds - transfer them on the exchange itself. Recording the
    flow here keeps the profit percentages and the drawdown honest.
    """
    marks = await bus.get_marks()
    async with session_scope() as session:
        latest = await repo.latest_equity(session)
        before = latest.equity if latest else await repo.capital_base(session)
        after = before + (payload.amount if payload.kind == "deposit" else -payload.amount)

        flow = await repo.add_capital_flow(
            session,
            kind=payload.kind,
            amount=payload.amount,
            asset=settings.quote_asset,
            source="manual",
            note=payload.note,
            equity_before=before,
            equity_after=after,
        )
        base = await repo.capital_base(session)
        await repo.log_event(
            session,
            f"{payload.kind.capitalize()} of {payload.amount:.2f} {settings.quote_asset} "
            f"recorded by {user}. Capital base is now {base:.2f}.",
            source="capital",
            data={"kind": payload.kind, "amount": payload.amount},
        )
        result = CapitalFlowOut.model_validate(flow, from_attributes=True)

    await bus.publish(bus.CH_EVENTS, {"type": "capital", "kind": payload.kind,
                                      "amount": payload.amount})
    return result


# --------------------------------------------------------------------------
# control
# --------------------------------------------------------------------------


@app.get("/api/settings")
async def get_settings_endpoint(_: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        effective = await _effective_settings(session)
    return {
        "effective": effective,
        "static": {
            "mode": settings.trading_mode,
            "exchange": exchange_registry.describe(),
            "quote_asset": settings.quote_asset,
            "fx_configured": fx_mod.configured(),
            "strategy_core": settings.strategy_core,
            "timeframe": settings.decision_timeframe,
            "htf_timeframe": settings.htf_timeframe if not settings.core_is_portfolio4h else None,
            "universe": settings.data_symbols,
            # Fixed by the study; shown read-only so the user sees what runs.
            "core_families": [
                {
                    "name": f.name,
                    "research_name": f.research_name,
                    "risk_pct": f.risk_pct,
                    "max_position_pct": f.max_position_pct,
                    "atr_stop": f.atr_stop,
                    "cooldown_min": f.cooldown_min,
                }
                for f in core4h.FAMILIES.values()
            ] if settings.core_is_portfolio4h else [],
            "taker_fee_pct": settings.taker_fee_pct,
            "round_trip_fee_pct": settings.round_trip_fee_pct,
            "llm_model": settings.llm_model,
            "candle_history": settings.candle_history,
        },
    }


@app.patch("/api/settings")
async def patch_settings(
    payload: SettingsPatch, user: str = Depends(current_user)
) -> dict:
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="nothing to update")

    async with session_scope() as session:
        for key, value in updates.items():
            await repo.set_setting(session, key, value)
            # keep the in-process settings object in step for this worker
            if hasattr(settings, key):
                object.__setattr__(settings, key, value)
        await repo.log_event(
            session,
            f"Settings updated by {user}: "
            + ", ".join(f"{k}={v}" for k, v in updates.items()),
            source="panel",
            data=updates,
        )
        effective = await _effective_settings(session)

    await bus.publish(bus.CH_EVENTS, {"type": "settings", "changed": list(updates)})
    return {"updated": updates, "effective": effective}


@app.post("/api/control/arm")
async def arm(payload: ArmRequest, user: str = Depends(current_user)) -> dict:
    """Arm or disarm live trading. Arming a live account requires confirmation."""
    if payload.armed and settings.trading_mode == "live":
        if (payload.confirm or "").strip().upper() != "ARM LIVE":
            raise HTTPException(
                status_code=400,
                detail='to arm live trading send confirm: "ARM LIVE"',
            )

    async with session_scope() as session:
        await repo.set_setting(session, "trading_armed", payload.armed)
        if payload.armed:
            await repo.set_setting(session, "kill_switch", False)
        await repo.log_event(
            session,
            f"Trading {'ARMED' if payload.armed else 'DISARMED'} by {user} "
            f"({settings.trading_mode} mode)",
            level="warn",
            source="panel",
        )
    await bus.publish(bus.CH_EVENTS, {"type": "armed", "armed": payload.armed})
    return {"armed": payload.armed, "mode": settings.trading_mode}


@app.post("/api/control/kill")
async def kill_switch(user: str = Depends(current_user)) -> dict:
    """Stop everything: no new entries, and flatten what is open."""
    async with session_scope() as session:
        await repo.set_setting(session, "kill_switch", True)
        await repo.set_setting(session, "trading_armed", False)
        await repo.log_event(
            session,
            f"KILL SWITCH engaged by {user}. All positions will be closed.",
            level="error",
            source="panel",
        )
    await bus.publish(bus.CH_COMMANDS, {"action": "flatten_all"})
    await bus.publish(bus.CH_EVENTS, {"type": "kill_switch"})
    return {"kill_switch": True}


@app.post("/api/control/resume")
async def resume(user: str = Depends(current_user)) -> dict:
    """Clear the kill switch and any daily-loss pause."""
    async with session_scope() as session:
        await repo.set_setting(session, "kill_switch", False)
        await repo.set_setting(session, "paused_until", None)
        await repo.log_event(
            session, f"Trading resumed by {user}", level="warn", source="panel"
        )
    return {"kill_switch": False, "paused_until": None}


@app.post("/api/control/command")
async def command(payload: CommandRequest, user: str = Depends(current_user)) -> dict:
    if payload.action == "close_position" and not payload.position_id:
        raise HTTPException(status_code=400, detail="position_id is required")
    body = payload.model_dump(exclude_none=True)
    await bus.publish(bus.CH_COMMANDS, body)
    async with session_scope() as session:
        await repo.log_event(
            session, f"Command '{payload.action}' issued by {user}",
            source="panel", data=body,
        )
    return {"sent": body}


# --------------------------------------------------------------------------
# exchange sync
# --------------------------------------------------------------------------


@app.get("/api/exchange/sync")
async def exchange_sync(_: str = Depends(current_user)) -> dict:
    async with session_scope() as session:
        state = await repo.get_sync_state(session)
        base = await repo.capital_base(session)
        ip = await ipwatch.current(session)
    return {
        "connected": state.connected,
        "stream_connected": state.stream_connected,
        "last_rest_sync": state.last_rest_sync,
        "last_stream_event": state.last_stream_event,
        "server_time_offset_ms": state.server_time_offset_ms,
        "balances": state.balances or {},
        "open_orders": state.exchange_open_orders,
        "futures_positions": state.exchange_positions or {},
        "drift": state.drift or {},
        "last_error": state.last_error,
        # A balance change the ledger cannot explain yet - a deposit or
        # withdrawal awaiting the two-sweep confirmation. The panel shows it
        # as pending; peaks and risk gates ignore readings taken meanwhile.
        "balance_unexplained": state.balance_unexplained,
        "balance_unexplained_since": state.balance_unexplained_since,
        "capital_base": base,
        "mode": settings.trading_mode,
        # The address the exchange sees; the key's allow-list must contain it.
        "public_ip": ip["public_ip"],
        "public_ip_seen_at": ip["public_ip_seen_at"],
    }


@app.get("/api/orders")
async def orders(
    limit: int = Query(100, ge=1, le=500), _: str = Depends(current_user)
) -> list[dict]:
    async with session_scope() as session:
        rows = await repo.recent_orders(session, limit)
    return [
        {
            "id": o.id,
            "created_at": o.created_at,
            "symbol": o.symbol,
            "side": o.side,
            "type": o.type,
            "intent": o.intent,
            "qty": o.qty,
            "filled_qty": o.filled_qty,
            "avg_price": o.avg_price,
            "fee": o.fee,
            "status": o.status,
            "mode": o.mode,
            "exchange_order_id": o.exchange_order_id,
            "error": o.error,
        }
        for o in rows
    ]


@app.get("/api/llm/reviews")
async def llm_reviews(
    limit: int = Query(20, ge=1, le=100), _: str = Depends(current_user)
) -> list[dict]:
    async with session_scope() as session:
        from sqlalchemy import select

        from app.db.models import LLMReview

        rows = (
            await session.execute(
                select(LLMReview).order_by(LLMReview.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "scope": r.scope,
            "symbol": r.symbol,
            "stance": r.stance,
            "risk_multiplier": r.risk_multiplier,
            "confidence": r.confidence,
            "reasoning": r.reasoning,
            "payload": r.payload,
            "tokens": r.input_tokens + r.output_tokens,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# backtest
# --------------------------------------------------------------------------


@app.post("/api/backtest", response_model=BacktestResponse)
async def run_backtest(
    payload: BacktestRequest, _: str = Depends(current_user)
) -> BacktestResponse:
    if settings.core_is_portfolio4h:
        return await _run_portfolio_backtest(payload)

    from app.backtest.engine import backtest_symbol

    timeframe = payload.timeframe or settings.timeframe
    async with session_scope() as session:
        candles = await repo.load_candles(
            session, payload.symbol.upper(), timeframe, payload.candles
        )
        # The higher-timeframe gate, as the live engine applies it.
        htf_candles = None
        if timeframe != settings.htf_timeframe:
            htf_candles = await repo.load_candles(
                session, payload.symbol.upper(), settings.htf_timeframe, payload.candles
            )
    if len(candles) < 300:
        raise HTTPException(
            status_code=400,
            detail=f"only {len(candles)} candles stored for {payload.symbol} {timeframe}",
        )

    result = await asyncio.to_thread(
        backtest_symbol,
        payload.symbol.upper(),
        timeframe,
        candles,
        payload.starting_capital,
        htf_candles,
        settings.htf_timeframe,
    )
    return BacktestResponse(**result)


async def _run_portfolio_backtest(payload: BacktestRequest) -> BacktestResponse:
    """The 4h core is a portfolio: the whole universe through the research
    simulator, whatever symbol the request names."""
    from app.backtest.portfolio4h import backtest_portfolio
    from app.strategies.core4h import MIN_BARS

    async with session_scope() as session:
        candles = {
            symbol: await repo.load_candles(session, symbol, CORE4H_TIMEFRAME, payload.candles)
            for symbol in settings.data_symbols
        }
    usable = {s: df for s, df in candles.items() if len(df) >= max(300, MIN_BARS)}
    if MARKET_GATE_SYMBOL not in usable:
        raise HTTPException(
            status_code=400,
            detail=f"only {len(candles.get(MARKET_GATE_SYMBOL, []))} 4h candles stored for "
            f"{MARKET_GATE_SYMBOL}; the market gate needs at least {max(300, MIN_BARS)}",
        )
    try:
        result = await asyncio.to_thread(backtest_portfolio, usable, payload.starting_capital)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BacktestResponse(**result)


# --------------------------------------------------------------------------
# realtime
# --------------------------------------------------------------------------


WS_TOKEN_RECHECK_SECONDS = 60


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        await decode_token(token)
    except HTTPException:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    log.info("panel websocket connected")
    last_check = time.monotonic()

    async def pump() -> None:
        async for channel, payload in bus.subscribe(
            bus.CH_EVENTS, bus.CH_TICKS, bus.CH_CANDLES
        ):
            await websocket.send_text(
                json.dumps({"channel": channel, "data": payload}, default=str)
            )

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            # keep the connection alive and drain anything the client sends
            await asyncio.wait_for(websocket.receive_text(), timeout=60)
            # the panel pings regularly; use that to drop sockets whose token
            # was revoked (logout / logout-all) or has expired meanwhile
            if time.monotonic() - last_check >= WS_TOKEN_RECHECK_SECONDS:
                last_check = time.monotonic()
                try:
                    await decode_token(token)
                except HTTPException:
                    await websocket.close(code=4401)
                    break
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001
        log.info("panel websocket closed: %s", exc)
    finally:
        pump_task.cancel()
        log.info("panel websocket disconnected")
