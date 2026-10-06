"""LLM advisory layer.

Deliberately narrow scope. The LLM never opens a trade - it cannot see the order
book, it is slower than the market, and paying for a token round trip on every
candle would cost more than the account makes. What it is good at:

  * reading the overall market picture across symbols at once
  * spotting when conditions are unusual enough that the quant rules should
    stand down (a "risk_off" stance)
  * sanity-checking a single candidate trade against that picture

Its only powers are: veto a trade, and scale position size between 0.25x and
1.5x. Both are bounded, auditable, and can be switched off in the panel.

The model behind it is a setting (app/llm/providers.py: Anthropic, OpenAI or
DeepSeek), optionally with a second provider that is tried once when the first
one fails. Every failure, including both providers failing, is "no opinion":
the caller trades on the quant rules alone.

Cost control: a stable system prompt is cached, the model is called at most
every LLM_REVIEW_MINUTES, and a hard daily call budget is enforced.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.db import repo
from app.db.session import session_scope
from app.llm.providers import (
    AnthropicProvider, ChatCompletionsProvider, build_provider, describe_error,
)

log = logging.getLogger(__name__)

Stance = Literal["risk_on", "neutral", "risk_off"]
Verdict = Literal["approve", "reduce", "veto"]
Provider = AnthropicProvider | ChatCompletionsProvider

# (primary, fallback) seconds per call, and the total the whole chain may take.
# The trade review is cut off by the trader at 30 s (LLM_REVIEW_TIMEOUT), so
# the primary has to give up early enough to leave the fallback a chance.
TRADE_TIMEOUTS = (20.0, 15.0)
TRADE_DEADLINE = 28.0
MARKET_TIMEOUTS = (45.0, 30.0)
MARKET_DEADLINE = 80.0
# A fallback that would have less than this is not started at all.
MIN_USEFUL_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# structured output schemas
# --------------------------------------------------------------------------


class SymbolView(BaseModel):
    symbol: str
    bias: Literal["bullish", "bearish", "neutral"]
    note: str = Field(max_length=280)


class MarketReview(BaseModel):
    """What the model returns for a whole-market review."""

    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    risk_multiplier: float = Field(ge=0.25, le=1.5)
    reasoning: str = Field(max_length=1200)
    symbols: list[SymbolView] = Field(default_factory=list)
    watch_for: list[str] = Field(default_factory=list, max_length=5)


class TradeReview(BaseModel):
    """What the model returns when asked about one specific candidate trade."""

    verdict: Verdict
    size_multiplier: float = Field(ge=0.25, le=1.5)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=800)
    concerns: list[str] = Field(default_factory=list, max_length=4)


# --------------------------------------------------------------------------
# prompts - kept byte-stable so the cache prefix survives between calls
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the risk advisor for a small automated crypto trading account.

Account facts that never change:
- The account is tiny (order of 100-1000 USDT). Position sizes are small and \
every round trip pays roughly 0.15-0.2% in fees.
- A quantitative ensemble (EMA trend, RSI reversion, squeeze breakout, VWAP \
pullback, momentum burst, market structure) plus a gradient-boosted classifier \
already produced the signals. They are not up for debate.
- Your job is risk oversight, not signal generation. You never propose trades \
and you never pick symbols to buy.

What you actually decide:
1. A market stance: risk_on, neutral, or risk_off.
2. A risk multiplier between 0.25 and 1.5 that scales every position size.
3. For a single candidate trade: approve, reduce, or veto.

How to think about it:
- Default to neutral and a multiplier of 1.0. Only move away from that when the \
data in front of you justifies it.
- risk_off is for genuinely dangerous conditions: volatility expanding across \
most symbols at once, correlated breakdowns, or price action inconsistent with \
the stated regime. It is not for "the chart looks uncertain" - that is normal.
- veto is for a trade that contradicts the broader picture, sits directly under \
resistance or above support, or arrives after an exhausted move.
- A quiet market is not a reason to reduce risk. Boredom is not a signal.
- You see indicator values, not raw charts. Say what the numbers support, and \
say plainly when they support nothing.

Be concise. Reasoning is read by one person in a dashboard, not by a committee."""


def _fmt_symbol_block(snapshots: dict[str, dict]) -> str:
    lines = []
    for symbol, snap in snapshots.items():
        parts = [f"{k}={v}" for k, v in snap.items() if k != "regime"]
        lines.append(f"- {symbol} [{snap.get('regime', '?')}]: " + " ".join(parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# advisor
# --------------------------------------------------------------------------


class LLMAdvisor:
    """Prompt building, provider chain, budget and failure handling."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._calls_today = 0
        self._day = dt.date.today()
        self._last_error: str | None = None
        self._errors: dict[str, str | None] = {}

    # -- lifecycle --

    @property
    def enabled(self) -> bool:
        return bool(settings.llm_enabled and settings.active_llm_provider)

    def _provider(self, name: str | None, configured_model: str) -> Provider | None:
        """The provider called `name`, built on first use; None when it cannot be."""
        if not name:
            return None
        provider = self._providers.get(name)
        if provider is None:
            try:
                provider = build_provider(name, settings.llm_model_for(name, configured_model))
            except Exception as exc:  # noqa: BLE001 - missing key, bad install
                log.error("could not create the %s client: %s", name, describe_error(exc))
                self._note_error(name, describe_error(exc))
                return None
            self._providers[name] = provider
        return provider

    def _note_error(self, provider: str, message: str) -> None:
        self._errors[provider] = self._last_error = message[:300]

    async def _budget_available(self) -> bool:
        """Daily budget, counted from the reviews table so every process shares it."""
        today = dt.date.today()
        if today != self._day:
            self._day = today
            self._calls_today = 0
        try:
            async with session_scope() as session:
                recorded = await repo.llm_calls_today(session)
        except Exception as exc:  # noqa: BLE001 - fall back to this process's count
            log.warning("could not read the LLM call count: %s", exc)
            recorded = 0
        return max(recorded, self._calls_today) < settings.llm_daily_call_budget

    # -- calls --

    async def _ask(
        self,
        what: str,
        schema: type[BaseModel],
        user_content: str,
        max_tokens: int,
        timeouts: tuple[float, float],
        deadline: float,
        system: str | None = None,
    ) -> tuple[BaseModel | None, dict]:
        """Primary provider, then the fallback once; (None, {}) when neither answers.

        Each attempt gets its own timeout, clipped to what is left of the
        deadline. Errors are remembered per provider for the panel but never
        raised: the advisor is optional and the caller trades on without it.
        `system` replaces the advisor's own prompt (the investment committee
        brings its own, byte-stable one); the chain, budget and error
        bookkeeping stay shared.
        """
        if not self.enabled or not await self._budget_available():
            return None, {}

        system_prompt = system or SYSTEM_PROMPT
        started = time.monotonic()
        chain = (
            (self._provider(settings.active_llm_provider, settings.llm_model), timeouts[0]),
            (self._provider(settings.active_llm_fallback_provider, settings.llm_fallback_model), timeouts[1]),
        )
        for provider, timeout in chain:
            if provider is None:
                continue
            timeout = min(timeout, deadline - (time.monotonic() - started))
            if timeout < MIN_USEFUL_TIMEOUT:
                log.warning("%s: not enough time left for %s", what, provider.name)
                break
            self._calls_today += 1
            try:
                parsed, usage = await asyncio.wait_for(
                    provider.complete_json(system_prompt, user_content, schema, max_tokens),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                log.warning("%s: %s gave no answer within %.0fs", what, provider.name, timeout)
                self._note_error(provider.name, f"{what}: no answer within {timeout:.0f}s")
                continue
            except Exception as exc:  # noqa: BLE001 - never let the advisor stop trading
                log.error("%s failed on %s: %s", what, provider.name, describe_error(exc))
                self._note_error(provider.name, f"{what}: {describe_error(exc)}")
                continue
            if parsed is None:
                self._note_error(provider.name, f"{what}: no usable answer")
                continue
            self._errors[provider.name] = None
            return parsed, {**usage, "provider": provider.name, "model": provider.model}
        return None, {}

    async def review_market(
        self,
        snapshots: dict[str, dict],
        equity: float,
        open_positions: list[dict],
        recent_performance: dict,
    ) -> tuple[MarketReview | None, dict]:
        """Whole-market risk review. Returns (review, usage)."""
        positions_text = (
            "\n".join(
                f"- {p['symbol']} {p['side']} entry={p['entry_price']} "
                f"unrealised={p.get('unrealized_pct', 0):.2f}%"
                for p in open_positions
            )
            or "- none"
        )

        user_content = f"""Current market snapshot (one line per symbol, indicator values):
{_fmt_symbol_block(snapshots)}

Account:
- equity: {equity:.2f} USDT
- open positions:
{positions_text}
- last 7 days: {recent_performance.get('trades', 0)} trades, \
win rate {recent_performance.get('win_rate', 0):.0%}, \
net {recent_performance.get('net_pnl', 0):+.2f} USDT
- current drawdown: {recent_performance.get('drawdown_pct', 0):.1f}%

Give the market stance, the risk multiplier, and a short read on each symbol."""

        review, usage = await self._ask(
            "market review", MarketReview, user_content, 4000, MARKET_TIMEOUTS, MARKET_DEADLINE
        )
        return review, usage  # type: ignore[return-value]

    async def review_trade(
        self,
        symbol: str,
        side: str,
        snapshot: dict,
        ensemble_score: float,
        contributions: dict,
        market_stance: str,
        expected_move_pct: float,
        stop_pct: float,
    ) -> tuple[TradeReview | None, dict]:
        """Second opinion on one candidate trade."""
        votes = "\n".join(
            f"- {name}: {data.get('score', 0):+.2f} ({data.get('reason', '')})"
            for name, data in contributions.items()
        )
        indicators = " ".join(f"{k}={v}" for k, v in snapshot.items() if k != "regime")

        user_content = f"""Candidate trade

Symbol: {symbol}
Direction: {side}
Ensemble score: {ensemble_score:.2f}
Regime: {snapshot.get('regime', 'unknown')}
Current market stance: {market_stance}
Take profit target: {expected_move_pct:.2f}% away
Stop loss: {stop_pct:.2f}% away

Indicators:
{indicators}

Strategy votes:
{votes}

Approve, reduce, or veto."""

        review, usage = await self._ask(
            f"trade review {symbol}", TradeReview, user_content, 2000, TRADE_TIMEOUTS, TRADE_DEADLINE
        )
        return review, usage  # type: ignore[return-value]

    # -- diagnostics for the panel --

    def status(self) -> dict:
        primary = settings.active_llm_provider
        fallback = settings.active_llm_fallback_provider
        models = {
            name: provider.model or "auto" for name, provider in self._providers.items()
        }
        if primary and primary not in models:
            models[primary] = settings.llm_model_for(primary, settings.llm_model) or "auto"
        if fallback and fallback not in models:
            models[fallback] = settings.llm_model_for(fallback, settings.llm_fallback_model) or "auto"
        return {
            "enabled": self.enabled,
            "provider": primary,
            "fallback_provider": fallback,
            "model": models.get(primary) if primary else None,
            "models": models,
            "calls_today": self._calls_today,
            "daily_budget": settings.llm_daily_call_budget,
            "last_error": self._last_error,
            "errors": dict(self._errors),
        }


advisor = LLMAdvisor()
