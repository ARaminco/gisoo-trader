"""SQLAlchemy models - every table the system persists."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
    JSON, LargeBinary, String, Text, UniqueConstraint, false, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Candle(Base):
    """OHLCV, one row per symbol/timeframe/open_time."""

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle"),
        Index("ix_candle_lookup", "symbol", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    open_time: Mapped[int] = mapped_column(BigInteger, nullable=False)  # ms epoch
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    quote_volume: Mapped[float] = mapped_column(Float, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    taker_buy_base: Mapped[float] = mapped_column(Float, default=0.0)


class Signal(Base):
    """A decision produced by the ensemble, whether or not it was traded."""

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signal_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short / flat
    score: Mapped[float] = mapped_column(Float, nullable=False)  # 0..1 ensemble score
    ml_probability: Mapped[Optional[float]] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(24), default="unknown")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    atr: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_stop: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_target: Mapped[float] = mapped_column(Float, default=0.0)
    expected_move_pct: Mapped[float] = mapped_column(Float, default=0.0)
    contributions: Mapped[dict] = mapped_column(JSON, default=dict)  # per-strategy votes
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(160))
    llm_verdict: Mapped[Optional[str]] = mapped_column(String(24))
    llm_note: Mapped[Optional[str]] = mapped_column(Text)


class Position(Base):
    """An open or closed position. One row per round trip."""

    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_position_status", "status", "symbol"),
        Index("ix_position_closed", "status", "mode", "closed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    market: Mapped[str] = mapped_column(String(8), default="spot")  # spot / futures
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / closed / error
    mode: Mapped[str] = mapped_column(String(8), default="paper")

    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    qty: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[Optional[float]] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)

    stop_price: Mapped[float] = mapped_column(Float, default=0.0)
    target_price: Mapped[float] = mapped_column(Float, default=0.0)
    initial_stop: Mapped[float] = mapped_column(Float, default=0.0)
    trail_active: Mapped[bool] = mapped_column(Boolean, default=False)
    high_water: Mapped[float] = mapped_column(Float, default=0.0)

    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    r_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(48))

    # ---- execution state, one column each -------------------------------
    # The engine and the reconciler run in different processes and both write
    # these. They are columns, not keys in `meta`, so an UPDATE of one never
    # discards the other process's write of another (a whole-JSON assignment
    # would).
    #
    # pending_exit_*: a rule or stop exit the exchange has not executed yet.
    #   Rule exits are one-bar pulses, so without this a refused order would
    #   leave the position open until the rule happens to fire again.
    # stop_missing / stop_retry_at: the exchange-side protective stop is gone
    #   (rejected, cancelled, lost) and must be re-placed.
    # exit_check_bar: the oldest 4h bar whose exit rule could not be read
    #   because the symbol had no candle; re-checked when the candle lands.
    pending_exit_reason: Mapped[Optional[str]] = mapped_column(String(48))
    pending_exit_price: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    pending_exit_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    stop_missing: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    stop_retry_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    exit_check_bar: Mapped[Optional[int]] = mapped_column(BigInteger)

    signal_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("signals.id"))
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    orders: Mapped[list["Order"]] = relationship(back_populates="position")


class Order(Base):
    """Every order the system sends, and what the exchange answered."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_order_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    position_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("positions.id"))
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    market: Mapped[str] = mapped_column(String(8), default="spot")
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY / SELL
    type: Mapped[str] = mapped_column(String(24), default="MARKET")
    intent: Mapped[str] = mapped_column(String(16), default="entry")  # entry / exit / stop
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="NEW")
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(64))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text)

    position: Mapped[Optional["Position"]] = relationship(back_populates="orders")


class EquitySnapshot(Base):
    """Periodic account value - the source of the equity curve and drawdown."""

    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_time", "taken_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    taken_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    free_balance: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    peak_equity: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Taken while the exchange balance held money the capital ledger could not
    # explain yet (a deposit or withdrawal not classified as a flow). The
    # equity is real, its meaning is not known: such a row never raises a
    # peak and `repo.peak_equity` leaves it out, because restating it with
    # the flow recorded later would count the money twice. The server
    # default lets `create_all` and the additive-column step in db.session
    # add the column to an existing table (see db.session.ADDITIVE_COLUMNS).
    suspect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class CapitalFlow(Base):
    """Deposits and withdrawals of trading capital.

    Without this, adding money to the account would register as profit and
    would reset the drawdown calculation. Every performance number is measured
    against `capital_base`, which is the running sum of these rows.
    """

    __tablename__ = "capital_flows"
    __table_args__ = (Index("ix_capital_time", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # deposit / withdrawal
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # always positive
    asset: Mapped[str] = mapped_column(String(12), default="USDT")
    source: Mapped[str] = mapped_column(String(24), default="manual")  # manual / exchange_sync
    note: Mapped[Optional[str]] = mapped_column(Text)
    equity_before: Mapped[float] = mapped_column(Float, default=0.0)
    equity_after: Mapped[float] = mapped_column(Float, default=0.0)
    external_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)


class ExchangeSync(Base):
    """State of the reconciliation loop against the live Binance account."""

    __tablename__ = "exchange_sync"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    stream_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_rest_sync: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_stream_event: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    server_time_offset_ms: Mapped[int] = mapped_column(Integer, default=0)
    balances: Mapped[dict] = mapped_column(JSON, default=dict)
    exchange_open_orders: Mapped[int] = mapped_column(Integer, default=0)
    exchange_positions: Mapped[dict] = mapped_column(JSON, default=dict)
    drift: Mapped[dict] = mapped_column(JSON, default=dict)  # differences found and fixed
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    # Quote balance the trade history cannot explain, as the last REST sweep
    # saw it (signed: positive = money appeared). Non-zero means a deposit or
    # withdrawal is in flight and not yet in `capital_flows`; every equity
    # reading taken meanwhile is suspect. `_since` is the last sweep at
    # which the balance WAS explained - the flow happened after it - and
    # becomes the recorded flow's `occurred_at`.
    balance_unexplained: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    balance_unexplained_since: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class Setting(Base):
    """Runtime-editable settings. Overrides the environment when present."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EventLog(Base):
    """Human-readable audit trail shown in the panel."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_event_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(12), default="info")  # info/warn/error/trade
    source: Mapped[str] = mapped_column(String(32), default="engine")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class PushSubscription(Base):
    """A browser that asked for Web Push notifications (one row per device).

    `endpoint` is the push service URL the browser handed out; `p256dh` and
    `auth` are the keys the payload is encrypted with. A 404/410 from the
    push service means the browser dropped the subscription and the row goes.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_success_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)


class Passkey(Base):
    """A WebAuthn credential the user can log in with: a hardware security
    key or a phone's platform passkey.

    `credential_id` / `public_key` are what the authenticator handed over at
    enrolment (base64url text and the raw COSE key). `sign_count` is the
    authenticator's usage counter, required to grow on every login - a cloned
    key falls behind. `rp_id` is the host the credential is bound to: a key
    enrolled on localhost is never offered on the public host and vice versa.
    """

    __tablename__ = "passkeys"
    __table_args__ = (Index("ix_passkey_rp", "rp_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    credential_id: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    rp_id: Mapped[str] = mapped_column(String(253), nullable=False)
    transports: Mapped[list] = mapped_column(JSON, default=list)  # usb / nfc / ble / ...
    name: Mapped[str] = mapped_column(String(64), default="")
    aaguid: Mapped[Optional[str]] = mapped_column(String(36))  # authenticator model id
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    backup_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)


class MLModel(Base):
    """Metadata for each trained model version."""

    __tablename__ = "ml_models"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    symbol: Mapped[str] = mapped_column(String(24), default="ALL")
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    features: Mapped[list] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CommitteeMeeting(Base):
    """One sitting of the investment committee (app/committee).

    `briefing` is the numeric picture the collector built (account, positions,
    market, performance vs the policy, health checks, stance history);
    `minutes` is the analyst's answer in the `CommitteeMinutes` schema, or the
    templated fallback when no model answered (`provider = "template"`).
    `applied_decisions` records what the runner actually applied (stance /
    risk multiplier through `llm_reviews`); `proposal_status` is keyed by the
    proposal's index in `minutes["proposals"]` and holds the user's
    approve / decline verdict and what the approval did. `delivery` is the
    per-channel result of the user notification, read by the next meeting's
    "notifications delivered" health check.
    """

    __tablename__ = "committee_minutes"
    __table_args__ = (Index("ix_committee_time", "held_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    held_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled / manual
    briefing: Mapped[dict] = mapped_column(JSON, default=dict)
    minutes: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(24), default="template")
    model: Mapped[str] = mapped_column(String(64), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    applied_decisions: Mapped[list] = mapped_column(JSON, default=list)
    proposal_status: Mapped[dict] = mapped_column(JSON, default=dict)
    health_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery: Mapped[dict] = mapped_column(JSON, default=dict)


class LLMReview(Base):
    """Each market-regime review returned by Claude."""

    __tablename__ = "llm_reviews"
    __table_args__ = (Index("ix_llm_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    scope: Mapped[str] = mapped_column(String(24), default="market")  # market / trade
    symbol: Mapped[Optional[str]] = mapped_column(String(24))
    stance: Mapped[str] = mapped_column(String(16), default="neutral")
    risk_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
