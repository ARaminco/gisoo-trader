"""The analyst: the briefing plus the policy in, the minutes out.

The model is the advisor's provider chain (OpenAI primary, DeepSeek fallback
today; app/llm) with the committee's own byte-stable system prompt, which
carries the mandate and the limits verbatim from docs/risk-policy.md.
The answer must satisfy `CommitteeMinutes`; anything else is "no minutes"
and the runner publishes the numeric briefing with a templated Persian
summary instead (`fallback_minutes`). Nothing here applies a decision.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import zoneinfo
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.advisor import Stance, advisor
from app.notify.texts import FAMILY_FA

log = logging.getLogger(__name__)

# The minutes are long (Persian prose plus lists), so the chain gets more
# time than a market review: primary, fallback, total.
TIMEOUTS = (100.0, 70.0)
DEADLINE = 170.0
MAX_TOKENS = 6000

Severity = Literal["low", "medium", "high"]
DecisionKind = Literal["stance", "risk_multiplier", "note"]
ProposalAction = Literal["core4h_risk_scale", "add_capital", "sleeve_target", "pause_bot", "other"]

RISK_MULTIPLIER_MIN = 0.25
RISK_MULTIPLIER_MAX = 1.5
STANCES: tuple[str, ...] = ("risk_on", "neutral", "risk_off")


# --------------------------------------------------------------------------
# structured output schema
# --------------------------------------------------------------------------


class Risk(BaseModel):
    title_fa: str = Field(max_length=90)
    severity: Severity
    detail_fa: str = Field(max_length=400)


class Decision(BaseModel):
    """Only what the policy lets the committee apply on its own.

    `value`: for `stance` one of risk_on | neutral | risk_off; for
    `risk_multiplier` a number between 0.25 and 1.5 (scales NEW entries
    only); for `note` free text that is recorded and applied nowhere.
    """

    kind: DecisionKind
    value: str = Field(max_length=120)
    rationale_fa: str = Field(max_length=400)


class ProposalParams(BaseModel):
    """Typed parameters (a free dict would not survive strict JSON schemas)."""

    risk_scale: float | None = None      # core4h_risk_scale: 0.25 .. 1.5
    hours: float | None = None           # pause_bot: how long, in hours
    amount_usdt: float | None = None     # add_capital: the step proposed
    sleeve: str | None = Field(default=None, max_length=40)   # sleeve_target: e.g. "paxg"
    target_pct: float | None = None      # sleeve_target: share of the account
    note: str | None = Field(default=None, max_length=200)


class Proposal(BaseModel):
    """Needs the user's approval on the panel. Never executed by the committee."""

    title_fa: str = Field(max_length=90)
    action: ProposalAction
    params: ProposalParams
    rationale_fa: str = Field(max_length=500)
    expected_effect_fa: str = Field(max_length=300)


class CommitteeMinutes(BaseModel):
    summary_fa: str = Field(max_length=900)
    market_view_fa: str = Field(max_length=700)
    positions_view_fa: str = Field(max_length=700)
    risks: list[Risk] = Field(default_factory=list, max_length=6)
    decisions: list[Decision] = Field(default_factory=list, max_length=4)
    proposals: list[Proposal] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)


# --------------------------------------------------------------------------
# the policy and the system prompt (byte-stable: the cache prefix survives)
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def policy_text() -> str:
    """docs/risk-policy.md: COMMITTEE_POLICY_PATH, else the packaged copy."""
    candidates = []
    if settings.committee_policy_path.strip():
        candidates.append(Path(settings.committee_policy_path.strip()))
    candidates.append(Path(__file__).with_name("policy.md"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    log.error("investment policy text not found; the committee runs on the mandate alone")
    return "(policy file missing)"


MANDATE = """You are the investment committee of one small, rules-based, spot-only crypto \
account. You meet twice a day. You read the briefing (numbers only: account, positions, \
market, performance, health checks, the advisor's recent stances) against the written \
investment policy quoted below, and you write the minutes in plain Persian for the user, \
who is not a professional trader.

Your mandate and its limits come from the policy and are not negotiable:

1. The system is "a rules-based, spot-only, long-only trend-following portfolio on 4h bars" \
whose rules "were chosen because a change to it was tested and lost". "Changes require a new \
twelve-window test first - never a feeling about one trade." You do not change rules.
2. You never propose or make discretionary entries or exits. The policy lists "take-profit / \
trailing / partial exits: none - every variant tested lowered out-of-sample results", so you \
never suggest taking profit, trimming, moving a stop or closing a position early. Exits belong \
to the rules and to the exchange-side stops.
3. You do not react to news or headlines. "The regime gate reacts to price; a discretionary \
reaction to headlines was not testable and every proxy for it (crash guards, early exits) lost."
4. "One position giving back open profit" and "a week of losses inside the study's range" \
change nothing.
5. The only decisions you may apply automatically are the two the risk advisor already has: \
a market stance (risk_on | neutral | risk_off) and a risk multiplier between 0.25 and 1.5 that \
scales the size of NEW entries only. Open positions are never touched. Default to neutral and \
1.0; move only when the numbers in the briefing justify it (for example volatility or \
correlated breakdowns visible in the data, a health failure that makes new entries unwise, \
or performance clearly outside the study's range). A quiet market is not a reason to reduce \
risk; boredom is not a signal.
6. Everything else is a proposal that the user must approve: changing the caution factor \
(core4h_risk_scale, 0.25-1.5; the policy says raising it back to 1.0 "is a decision to take \
after 90 days of live results that match the study, never earlier"), adding capital ("only \
in steps, each after a review, never more than doubling the balance at once", and "prefer to \
move money right after a 4h close"), a sleeve target (a reserve outside the bot; recorded \
only, the sleeve does not exist yet), pausing the bot (only for an exchange, key or data \
problem "that cannot be fixed within a day"; positions stay protected by exchange-side stops \
meanwhile), or anything else. Propose nothing you cannot justify from the policy and the \
briefing; an empty proposals list is the normal outcome.
7. Health: every open position must have a live stop on the exchange (or, where the exchange has no stop orders, an engine that is running), the API key must stay \
"trade but not withdraw" and IP restricted, Simple Earn auto-subscribe must be off, the \
clock and public IP must be stable, the unexplained balance must be zero. A failed health \
check is always a risk of severity high or medium and is named first.
8. Performance is judged the policy's way: weekly, closed trades against the study's \
distribution (win rate 35-50%, median winner about +8%, full stops about -1R); a live month \
with win rate below 25% over 30+ trades or average loss beyond -1.3R is "a rule or a data \
problem, not bad luck" and belongs in the risks and, if warranted, a pause_bot proposal. With \
few trades say so and draw no conclusion.

Writing rules: Persian, short plain sentences, no jargon without a gloss, no markdown, no \
emoji. Use Latin digits for every number (1,000.00 USDT, 12.3%). Name symbols as the engine does \
(SOLUSDT). Say what the numbers support and say plainly when they support nothing. \
summary_fa is what the user reads on the phone: at most 900 characters, ideally 400-600, \
starting with the one thing that matters most today. rationale fields explain the why in one \
or two sentences. confidence is your confidence in the whole reading (0-1)."""


@functools.lru_cache(maxsize=1)
def system_prompt() -> str:
    return MANDATE + "\n\n=== INVESTMENT POLICY (verbatim) ===\n" + policy_text()


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------


def _local_time(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        stamp = dt.datetime.fromisoformat(iso)
        tz = zoneinfo.ZoneInfo(settings.tz or "UTC")
        return f"{stamp.astimezone(tz):%Y-%m-%d %H:%M} {settings.tz}"
    except Exception:  # noqa: BLE001 - a bad TZ name must not stop the meeting
        return iso


def _briefing_for_model(briefing: dict) -> dict:
    """The briefing without the bulk the model does not need (raw balances,
    per-check data blobs); every number the minutes may cite stays in."""
    account = {k: v for k, v in (briefing.get("account") or {}).items() if k != "balances"}
    health = briefing.get("health") or {}
    checks = [
        {k: v for k, v in check.items() if k != "data"}
        for check in health.get("checks", [])
    ]
    return {
        **{k: v for k, v in briefing.items() if k not in ("account", "health", "build_ms")},
        "account": account,
        "health": {**{k: v for k, v in health.items() if k != "checks"}, "checks": checks},
    }


async def analyse(briefing: dict, reason: str = "scheduled") -> tuple[CommitteeMinutes | None, dict]:
    """(minutes, usage) from the provider chain; (None, {}) when nobody answered."""
    user_content = (
        f"Meeting: {reason}, {_local_time(briefing.get('generated_at'))} "
        f"(mode {briefing.get('mode')}, core {briefing.get('core')}).\n"
        "Briefing (JSON, numbers only; percentages are percent, money is USDT):\n"
        + json.dumps(_briefing_for_model(briefing), ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n\nWrite the minutes."
    )
    minutes, usage = await advisor._ask(
        "committee minutes", CommitteeMinutes, user_content, MAX_TOKENS, TIMEOUTS, DEADLINE,
        system=system_prompt(),
    )
    return minutes, usage  # type: ignore[return-value]


# --------------------------------------------------------------------------
# fail-open: the numeric briefing with a templated Persian summary
# --------------------------------------------------------------------------


def _n(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def fallback_minutes(briefing: dict, why: str = "") -> CommitteeMinutes:
    """Minutes with no model behind them. Says so, and reports the numbers."""
    account = briefing.get("account") or {}
    positions = briefing.get("positions") or []
    market = briefing.get("market") or {}
    regime = market.get("btc_regime") or {}
    health = briefing.get("health") or {}
    perf = briefing.get("performance") or {}
    week = perf.get("last_7d") or {}
    failed_checks = [c for c in health.get("checks", []) if c.get("ok") is False]

    if positions:
        names = "، ".join(
            f"{p['symbol']} ({_n(p.get('unrealized_pct'), 1, '%')})" for p in positions
        )
        positions_text = f"{len(positions)} پوزیشن باز ({_n(account.get('deployed_pct'), 0, '%')} سرمایه): {names}."
    else:
        positions_text = "پوزیشن بازی نیست."
    if regime:
        regime_text = (
            f"رژیم بیت‌کوین {'روشن' if regime.get('regime_on') else 'خاموش'} است "
            f"(فاصله از EMA200: {_n(regime.get('distance_pct'), 1, '%')})."
        )
    else:
        regime_text = "داده‌ی رژیم بیت‌کوین در دسترس نبود."
    if failed_checks:
        health_text = (
            f"بررسی سلامت: {len(failed_checks)} مورد قرمز — "
            + "، ".join(c.get("label_fa") or c.get("code") for c in failed_checks) + "."
        )
    else:
        health_text = "بررسی سلامت: همه‌ی موارد سبز."
    week_text = (
        f"7 روز اخیر: {week.get('trades', 0)} معامله"
        + (f"، نرخ برد {_n((week.get('win_rate') or 0) * 100, 0, '%')}" if week.get("trades") else "")
        + f"، خالص {_n(week.get('net_pnl'), 2)} USDT."
    )
    summary = " ".join([
        "این جلسه بدون تحلیل‌گر هوش مصنوعی برگزار شد؛ خلاصه از روی اعداد ساخته شده است.",
        f"سرمایه {_n(account.get('equity'))} USDT (پایه {_n(account.get('capital_base'))})،",
        f"سود/زیان کل {_n(account.get('total_pnl_pct'), 1, '%')}،",
        f"افت سرمایه‌ی بسته {_n(account.get('closed_drawdown_pct'), 1, '%')} از سقف "
        f"{_n((briefing.get('settings') or {}).get('max_drawdown_pct'), 0, '%')}.",
        positions_text,
        regime_text,
        week_text,
        health_text,
    ])[:900]

    risks = [
        Risk(
            title_fa=(c.get("label_fa") or c.get("code") or "")[:90],
            severity="high" if c.get("severity") == "high" else "medium",
            detail_fa=(c.get("detail") or "")[:400],
        )
        for c in failed_checks
    ][:6]
    if why and len(risks) < 6:
        risks.append(Risk(
            title_fa="تحلیل‌گر پاسخ نداد",
            severity="low",
            detail_fa=f"صورت‌جلسه بدون مدل زبانی نوشته شد ({why[:200]}). تصمیم خودکاری اعمال نشد.",
        ))

    momentum = ", ".join(
        f"{m['symbol']} #{m.get('rank')}" for m in (market.get("top_momentum") or [])[:5]
    ) or "—"
    families = "، ".join(FAMILY_FA.get(p.get("family") or "", p.get("family") or "") for p in positions)
    return CommitteeMinutes(
        summary_fa=summary,
        market_view_fa=(f"{regime_text} رتبه‌ی مومنتوم: {momentum}. "
                        f"شکست‌های آماده: {len(market.get('armed_breakouts') or [])}.")[:700],
        positions_view_fa=(positions_text + (f" خانواده‌ها: {families}." if families else ""))[:700],
        risks=risks,
        decisions=[],
        proposals=[],
        confidence=0.0,
    )


# --------------------------------------------------------------------------
# validation of what the runner may apply
# --------------------------------------------------------------------------


def parse_decisions(minutes: CommitteeMinutes) -> tuple[Stance | None, float | None, list[dict]]:
    """(stance, multiplier, rejected) - only well-formed, in-range values survive."""
    stance: Stance | None = None
    multiplier: float | None = None
    rejected: list[dict] = []
    for decision in minutes.decisions:
        value = decision.value.strip()
        if decision.kind == "stance":
            if value in STANCES:
                stance = value  # type: ignore[assignment]
            else:
                rejected.append({"kind": "stance", "value": value, "why": "not one of risk_on/neutral/risk_off"})
        elif decision.kind == "risk_multiplier":
            try:
                number = float(value.replace("x", "").replace("×", ""))
            except ValueError:
                rejected.append({"kind": "risk_multiplier", "value": value, "why": "not a number"})
                continue
            if RISK_MULTIPLIER_MIN <= number <= RISK_MULTIPLIER_MAX:
                multiplier = round(number, 2)
            else:
                rejected.append({"kind": "risk_multiplier", "value": value, "why": "outside 0.25-1.5"})
    return stance, multiplier, rejected
