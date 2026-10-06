"""The ensemble: turns strategy votes plus the ML probability into one decision.

Design rules that matter more than the maths:
  * A trade needs agreement, not a single loud opinion.
  * The take-profit must clear the round-trip fee by a wide margin, or the
    trade is not taken at all - with 100 USDT that is what kills accounts.
  * Stops come from ATR, never from a fixed percentage.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import pandas as pd

from app.config import settings
from app.features.build import classify_regime
from app.strategies.base import Strategy, Vote
from app.strategies.rules import ALL_STRATEGIES

log = logging.getLogger(__name__)

# How much each regime trusts each strategy family.
REGIME_WEIGHTS: dict[str, dict[str, float]] = {
    "trend_up":   {"ema_trend": 1.3, "vwap_pullback": 1.2, "momentum_burst": 1.0,
                   "squeeze_breakout": 0.9, "structure": 0.7, "rsi_reversion": 0.3},
    "trend_down": {"ema_trend": 1.3, "vwap_pullback": 1.2, "momentum_burst": 1.0,
                   "squeeze_breakout": 0.9, "structure": 0.7, "rsi_reversion": 0.3},
    "range":      {"rsi_reversion": 1.4, "structure": 1.2, "squeeze_breakout": 1.0,
                   "ema_trend": 0.4, "vwap_pullback": 0.3, "momentum_burst": 0.4},
    "transition": {"squeeze_breakout": 1.2, "ema_trend": 0.9, "rsi_reversion": 0.8,
                   "structure": 0.9, "vwap_pullback": 0.7, "momentum_burst": 0.8},
    "volatile":   {"momentum_burst": 1.1, "ema_trend": 0.8, "structure": 0.8,
                   "squeeze_breakout": 0.7, "vwap_pullback": 0.5, "rsi_reversion": 0.4},
}

# A vote this small is an abstention, not an opinion.
ABSTAIN_THRESHOLD = 0.08
# Never trade on a single strategy's say-so.
MIN_VOTERS = 2


@dataclass
class Decision:
    symbol: str
    timeframe: str
    side: str                    # long / short / flat
    score: float                 # 0..1 confidence in `side`
    raw_score: float             # -1..1 signed ensemble output
    regime: str
    price: float
    atr: float
    stop_price: float = 0.0
    target_price: float = 0.0
    expected_move_pct: float = 0.0
    ml_probability: float | None = None
    contributions: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    tradable: bool = False
    rejection: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Ensemble:
    def __init__(self, strategies: list[Strategy] | None = None):
        self.strategies = strategies or ALL_STRATEGIES

    # ------------------------------------------------------------------
    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        htf: pd.DataFrame | None = None,
        ml_probability: float | None = None,
    ) -> Decision:
        row = df.iloc[-1]
        regime = classify_regime(row)
        price = float(row["close"])
        atr = float(row.get("atr", 0.0) or 0.0)

        votes: list[tuple[Vote, float]] = []
        contributions: dict[str, dict] = {}

        for strategy in self.strategies:
            if not strategy.applies_to(regime):
                continue
            try:
                vote = strategy.evaluate(df, regime)
            except Exception as exc:  # noqa: BLE001 - one bad strategy must not stop the loop
                log.warning("strategy %s failed on %s: %s", strategy.name, symbol, exc)
                continue
            regime_weight = REGIME_WEIGHTS.get(regime, {}).get(strategy.name, 1.0)
            weight = strategy.weight * regime_weight
            votes.append((vote, weight))
            contributions[strategy.name] = {
                "score": round(vote.score, 4),
                "weight": round(weight, 3),
                "reason": vote.reason,
            }

        # Only strategies that actually formed an opinion get a say. Averaging
        # over abstaining strategies too would dilute genuine confluence toward
        # zero and make the threshold meaningless.
        active = [(v, w) for v, w in votes if abs(v.score) >= ABSTAIN_THRESHOLD]
        if len(active) < MIN_VOTERS:
            decision_regime = regime
            return Decision(
                symbol=symbol,
                timeframe=timeframe,
                side="flat",
                score=0.0,
                raw_score=0.0,
                regime=decision_regime,
                price=float(row["close"]),
                atr=float(row.get("atr", 0.0) or 0.0),
                contributions=contributions,
                ml_probability=ml_probability,
                rejection=f"only {len(active)} strategies had an opinion",
            )

        weighted_sum = sum(v.score * w for v, w in active)
        weight_total = sum(w for _, w in active)
        raw = weighted_sum / weight_total if weight_total else 0.0

        # Confluence: how much of the active weight points the same way as the
        # net result. Six strategies pulling together is a very different setup
        # from three long and three short that happen to net out.
        direction = 1 if raw >= 0 else -1
        agreeing = sum(w for v, w in active if (1 if v.score >= 0 else -1) == direction)
        agreement = agreeing / weight_total if weight_total else 0.0
        raw *= 0.55 + 0.45 * agreement

        decision = Decision(
            symbol=symbol,
            timeframe=timeframe,
            side="flat",
            score=0.0,
            raw_score=round(raw, 4),
            regime=regime,
            price=price,
            atr=atr,
            contributions=contributions,
            ml_probability=ml_probability,
        )

        # ---- higher timeframe gate ----
        htf_bias = self._htf_bias(htf)
        decision.contributions["htf_bias"] = {"score": htf_bias, "weight": 1.0,
                                              "reason": "higher timeframe direction"}

        side = "long" if raw > 0 else "short"
        confidence = abs(raw)

        # Trading against the higher timeframe needs a much stronger signal.
        if htf_bias != 0 and ((side == "long") != (htf_bias > 0)):
            confidence *= 0.55
            decision.reasons.append("counter to higher timeframe")
        else:
            confidence = min(1.0, confidence * 1.1)

        # ---- ML gate ----
        if ml_probability is not None and settings.ml_enabled:
            # ml_probability is P(up). Convert to agreement with `side`.
            agreement = ml_probability if side == "long" else 1 - ml_probability
            decision.reasons.append(f"ml agreement {agreement:.2f}")
            if agreement < settings.ml_min_probability:
                decision.side = "flat"
                decision.score = round(confidence, 4)
                decision.rejection = f"ml disagrees ({agreement:.2f})"
                return decision
            confidence = min(1.0, confidence * (0.7 + 0.6 * agreement))

        decision.side = side
        decision.score = round(confidence, 4)

        # ---- risk geometry ----
        if atr <= 0:
            decision.rejection = "no ATR reading"
            return decision

        stop_distance = atr * settings.atr_stop_mult
        target_distance = atr * settings.atr_target_mult
        if side == "long":
            decision.stop_price = price - stop_distance
            decision.target_price = price + target_distance
        else:
            decision.stop_price = price + stop_distance
            decision.target_price = price - target_distance

        decision.expected_move_pct = round(target_distance / price * 100, 4)

        # ---- volatility gate ----
        # The forensics on 12 regime windows found the same signal loses in
        # quiet markets and only carries an edge while volatility expands.
        atr_ratio = float(row.get("atr_ratio", 1.0) or 1.0)
        if settings.min_atr_ratio and atr_ratio < settings.min_atr_ratio:
            decision.rejection = (
                f"volatility too quiet (atr ratio {atr_ratio:.2f} < {settings.min_atr_ratio})"
            )
            return decision

        # ---- the two hard gates ----
        if confidence < settings.min_signal_score:
            decision.rejection = f"score {confidence:.2f} < {settings.min_signal_score}"
            return decision

        fee_hurdle = settings.round_trip_fee_pct * 2.5
        min_move = max(settings.min_expected_move_pct, fee_hurdle)
        if decision.expected_move_pct < min_move:
            decision.rejection = (
                f"target {decision.expected_move_pct:.2f}% below fee hurdle {min_move:.2f}%"
            )
            return decision

        # Reward-to-risk must be worth the trade after fees.
        rr = target_distance / stop_distance if stop_distance else 0
        if rr < 1.3:
            decision.rejection = f"reward/risk {rr:.2f} too low"
            return decision

        decision.tradable = True
        return decision

    # ------------------------------------------------------------------
    @staticmethod
    def _htf_bias(htf: pd.DataFrame | None) -> float:
        """+1 bullish, -1 bearish, 0 undecided, on the higher timeframe."""
        if htf is None or htf.empty or len(htf) < 3:
            return 0.0
        row = htf.iloc[-1]
        try:
            above200 = float(row.get("above_ema200", 0) or 0)
            ema_spread = float(row.get("ema_spread", 0) or 0)
            adx = float(row.get("adx", 0) or 0)
            di_spread = float(row.get("di_spread", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

        if adx < 18:
            return 0.0
        score = 0.0
        score += 0.4 if above200 else -0.4
        score += 0.3 * (1 if ema_spread > 0 else -1)
        score += 0.3 * (1 if di_spread > 0 else -1)
        if score > 0.35:
            return 1.0
        if score < -0.35:
            return -1.0
        return 0.0
