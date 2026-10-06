"""Request and response models for the panel API."""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TotpChallengeResponse(BaseModel):
    """Step one of a 2FA login succeeded; the code is still needed."""

    requires_totp: Literal[True] = True
    challenge: str
    expires_in: int


class TotpLoginRequest(BaseModel):
    challenge: str
    code: str = Field(max_length=64)


class TotpCodeRequest(BaseModel):
    code: str = Field(max_length=64)


class MeResponse(BaseModel):
    username: str
    totp_enabled: bool
    token_expires_at: dt.datetime
    passkeys_enrolled: int = 0
    passkey_required: bool = False


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_png_base64: str


class TotpEnableResponse(BaseModel):
    enabled: Literal[True] = True
    backup_codes: list[str]


class TotpDisableResponse(BaseModel):
    enabled: Literal[False] = False


# Random id of a pending WebAuthn ceremony (see security.new_state_id).
PASSKEY_STATE_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"


class PasskeyOptionsResponse(BaseModel):
    """`options` is the PublicKeyCredential{Creation,Request}Options JSON the
    browser gets (binary fields base64url); `state` comes back with the answer."""

    state: str
    options: dict[str, Any]


class PasskeyRegisterVerifyRequest(BaseModel):
    state: str = Field(pattern=PASSKEY_STATE_PATTERN)
    # `navigator.credentials.create()` result with its ArrayBuffers as base64url
    credential: dict[str, Any]
    name: str = Field(default="", max_length=64)


class PasskeyLoginOptionsRequest(BaseModel):
    username: str = Field(max_length=128)


class PasskeyLoginVerifyRequest(BaseModel):
    state: str = Field(pattern=PASSKEY_STATE_PATTERN)
    credential: dict[str, Any]


class PasskeyOut(BaseModel):
    id: int
    name: str
    rp_id: str
    transports: list[str]
    aaguid: str | None
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    backup_eligible: bool
    backed_up: bool


class PasskeyListResponse(BaseModel):
    passkeys: list[PasskeyOut]
    passkey_required: bool


class PasskeyRequiredRequest(BaseModel):
    enabled: bool


class PasskeyAvailableResponse(BaseModel):
    available: bool


class OverviewResponse(BaseModel):
    mode: str
    strategy_core: str = "portfolio4h"
    armed: bool
    kill_switch: bool
    paused_until: str | None = None

    equity: float
    free_balance: float
    capital_base: float
    unrealized_pnl: float
    total_pnl: float
    total_pnl_pct: float
    today_pnl: float
    today_pnl_pct: float
    peak_equity: float
    drawdown_pct: float
    # Closed equity = equity minus open profit; the 4h core's kill switch
    # measures its drawdown on this (see risk.manager).
    closed_equity: float = 0.0
    closed_peak_equity: float = 0.0
    closed_drawdown_pct: float = 0.0
    # A deposit or withdrawal seen on the exchange but not yet recorded as a
    # capital flow (signed). While non-zero the figures above exclude it.
    balance_unexplained: float = 0.0
    balance_unexplained_since: dt.datetime | None = None

    open_positions: int
    max_positions: int
    trades_total: int
    trades_today: int
    win_rate: float
    profit_factor: float
    avg_r: float

    market_stance: str
    risk_multiplier: float
    exchange_connected: bool
    stream_connected: bool
    last_sync: str | None = None
    workers: dict[str, Any] = Field(default_factory=dict)


class PositionOut(BaseModel):
    id: int
    symbol: str
    side: str
    market: str
    status: str
    qty: float
    entry_price: float
    exit_price: float | None = None
    mark_price: float | None = None
    stop_price: float
    target_price: float
    notional: float
    unrealized_pnl: float = 0.0
    unrealized_pct: float = 0.0
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    r_multiple: float = 0.0
    fees_paid: float = 0.0
    trail_active: bool = False
    # Execution state the user must be able to see: an exit the exchange has
    # not executed yet, and a protective stop that is being re-placed.
    pending_exit_reason: str | None = None
    stop_missing: bool = False
    # Peak open gain, from the bar-close high-water mark (long: highest high,
    # short: lowest low), and how much of it has been given back.
    high_water: float = 0.0
    peak_pct: float = 0.0
    giveback_pct: float = 0.0
    exit_reason: str | None = None
    opened_at: dt.datetime
    closed_at: dt.datetime | None = None
    meta: dict = Field(default_factory=dict)


class SignalOut(BaseModel):
    id: int
    created_at: dt.datetime
    symbol: str
    side: str
    score: float
    ml_probability: float | None = None
    regime: str
    price: float
    expected_move_pct: float
    executed: bool
    rejection_reason: str | None = None
    llm_verdict: str | None = None
    llm_note: str | None = None
    contributions: dict = Field(default_factory=dict)


class EquityPoint(BaseModel):
    t: dt.datetime
    equity: float
    drawdown_pct: float
    unrealized_pnl: float


class EventOut(BaseModel):
    id: int
    created_at: dt.datetime
    level: str
    source: str
    message: str
    data: dict = Field(default_factory=dict)


class CapitalFlowIn(BaseModel):
    kind: Literal["deposit", "withdrawal"]
    amount: float = Field(gt=0)
    note: str | None = None


class CapitalFlowOut(BaseModel):
    id: int
    occurred_at: dt.datetime
    kind: str
    amount: float
    asset: str
    source: str
    note: str | None = None


class SettingsPatch(BaseModel):
    """Everything the user can change at runtime without a restart.

    `strategy_core` is deliberately absent: switching cores changes what the
    market-data worker streams, so it is an .env change plus a restart. Under
    the portfolio4h core the ensemble-only knobs (signal score, ATR bracket,
    trailing, ATR ratio, ML, per-trade risk, position cap, cooldown) are
    accepted but not read - the families carry their own validated sizing.
    """

    risk_per_trade_pct: float | None = Field(default=None, ge=0.1, le=10)
    max_concurrent_positions: int | None = Field(default=None, ge=1, le=10)
    max_daily_loss_pct: float | None = Field(default=None, ge=1, le=50)
    max_drawdown_pct: float | None = Field(default=None, ge=2, le=90)
    min_signal_score: float | None = Field(default=None, ge=0.3, le=0.95)
    min_expected_move_pct: float | None = Field(default=None, ge=0.1, le=10)
    max_position_pct: float | None = Field(default=None, ge=5, le=100)
    atr_stop_mult: float | None = Field(default=None, ge=0.5, le=6)
    atr_target_mult: float | None = Field(default=None, ge=0.5, le=12)
    trailing_enabled: bool | None = None
    cooldown_minutes_after_loss: int | None = Field(default=None, ge=0, le=1440)
    ml_enabled: bool | None = None
    ml_min_probability: float | None = Field(default=None, ge=0.4, le=0.9)
    llm_enabled: bool | None = None
    llm_can_veto: bool | None = None
    llm_review_minutes: int | None = Field(default=None, ge=5, le=720)
    futures_enabled: bool | None = None
    futures_leverage: int | None = Field(default=None, ge=1, le=10)
    min_atr_ratio: float | None = Field(default=None, ge=0.0, le=5.0)
    skip_weekend_entries: bool | None = None
    core4h_risk_scale: float | None = Field(default=None, ge=0.25, le=1.5)
    symbols: list[str] | None = None


class ArmRequest(BaseModel):
    armed: bool
    confirm: str | None = None


class CommandRequest(BaseModel):
    action: Literal["close_position", "flatten_all", "evaluate_now", "reload_models"]
    position_id: int | None = None


class PushKeys(BaseModel):
    p256dh: str = Field(max_length=255)
    auth: str = Field(max_length=64)


class PushSubscribeIn(BaseModel):
    """What `PushSubscription.toJSON()` gives the browser, plus the device name."""

    endpoint: str = Field(max_length=2048)
    keys: PushKeys
    user_agent: str | None = Field(default=None, max_length=255)


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(max_length=2048)


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = Field(default="23:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class NotifyPrefs(BaseModel):
    categories: dict[str, bool]
    quiet_hours: QuietHours


class NotifyPrefsPatch(BaseModel):
    categories: dict[str, bool] | None = None
    quiet_hours: QuietHours | None = None


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    candles: int = Field(default=1500, ge=300, le=20000)
    starting_capital: float = Field(default=100.0, gt=0)


class BacktestResponse(BaseModel):
    symbol: str
    timeframe: str
    candles: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    net_pnl_pct: float
    profit_factor: float
    max_drawdown_pct: float
    avg_r: float
    expectancy: float
    fees_paid: float
    equity_curve: list[float]
    trade_log: list[dict]
