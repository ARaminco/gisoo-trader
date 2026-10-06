"""The rule-based strategies that make up the ensemble.

Each one targets a different market condition. The ensemble weights them by
regime so that, for example, mean-reversion does not fight a strong trend.
"""
from __future__ import annotations

import pandas as pd

from app.strategies.base import Strategy, Vote


class EmaTrendStrategy(Strategy):
    """Trend following: EMA stack alignment plus MACD confirmation."""

    name = "ema_trend"
    weight = 1.2
    regimes = ("trend_up", "trend_down", "transition")

    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        ema_spread = self.last(df, "ema_spread")
        slow_dist = self.last(df, "ema_slow_dist")
        macd_hist = self.last(df, "macd_hist_norm")
        macd_prev = self.prev(df, "macd_hist_norm")
        adx = self.last(df, "adx")
        st_dir = self.last(df, "st_dir")
        above200 = self.last(df, "above_ema200")

        score = 0.0
        # EMA9 over EMA21 is the primary trigger
        score += 0.35 * max(-1.0, min(1.0, ema_spread / 0.35))
        # position relative to EMA50 keeps us on the right side of the swing
        score += 0.2 * max(-1.0, min(1.0, slow_dist / 1.2))
        # MACD histogram expanding in the same direction
        if macd_hist * (macd_hist - macd_prev) > 0:
            score += 0.25 * (1 if macd_hist > 0 else -1)
        # supertrend as a regime confirmation
        score += 0.1 * st_dir
        # long-term bias
        score += 0.1 * (1 if above200 else -1)

        # A trend strategy is worthless without trend strength.
        if adx < 20:
            score *= 0.4

        direction = "long" if score > 0 else "short"
        return Vote(
            self.name,
            score,
            f"ema_spread={ema_spread:.3f} adx={adx:.1f} macd={macd_hist:.4f} -> {direction}",
            {"adx": adx, "ema_spread": ema_spread},
        ).clamped()


class RsiReversionStrategy(Strategy):
    """Mean reversion: fade stretched RSI inside a range, with wick confirmation."""

    name = "rsi_reversion"
    weight = 1.0
    regimes = ("range", "transition")

    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        rsi = self.last(df, "rsi", 50.0)
        rsi_slope = self.last(df, "rsi_slope")
        bb_pos = self.last(df, "bb_pos", 0.5)
        lower_wick = self.last(df, "lower_wick")
        upper_wick = self.last(df, "upper_wick")
        adx = self.last(df, "adx")

        score = 0.0
        if rsi <= 32:
            score += 0.45 * min(1.0, (32 - rsi) / 12)
            if rsi_slope > 0:
                score += 0.2          # turning back up
            if lower_wick > 0.4:
                score += 0.15         # rejection from below
        elif rsi >= 68:
            score -= 0.45 * min(1.0, (rsi - 68) / 12)
            if rsi_slope < 0:
                score -= 0.2
            if upper_wick > 0.4:
                score -= 0.15

        # Band position reinforces the same idea
        if bb_pos <= 0.05:
            score += 0.2
        elif bb_pos >= 0.95:
            score -= 0.2

        # Never fade a strong trend.
        if adx > 30:
            score *= 0.25

        return Vote(
            self.name,
            score,
            f"rsi={rsi:.1f} slope={rsi_slope:.2f} bb_pos={bb_pos:.2f}",
            {"rsi": rsi, "bb_pos": bb_pos},
        ).clamped()


class SqueezeBreakoutStrategy(Strategy):
    """Volatility expansion: Bollinger squeeze resolving with volume."""

    name = "squeeze_breakout"
    weight = 1.1
    regimes = ("range", "transition", "trend_up", "trend_down")

    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        was_squeezed = any(
            self.prev(df, "squeeze", offset=i) > 0 for i in range(2, 7) if len(df) > i
        )
        squeezed_now = self.last(df, "squeeze") > 0
        bb_width_pct = self.last(df, "bb_width_pct", 0.5)
        dc_pos = self.last(df, "dc_pos", 0.5)
        vol_z = self.last(df, "vol_z")
        direction = self.last(df, "direction")
        body_ratio = self.last(df, "body_ratio")

        if squeezed_now or not was_squeezed:
            return Vote(self.name, 0.0, "no squeeze release")

        score = 0.0
        # break of the Donchian channel is the trigger
        if dc_pos >= 0.95:
            score += 0.5
        elif dc_pos <= 0.05:
            score -= 0.5
        else:
            return Vote(self.name, 0.0, "squeeze released without a break")

        # a real breakout comes with volume and a decisive candle
        if vol_z > 1.0:
            score += 0.25 * direction
        if body_ratio > 0.55:
            score += 0.2 * direction
        # expanding bands confirm the release
        if bb_width_pct > 0.6:
            score += 0.1 * direction

        return Vote(
            self.name,
            score,
            f"dc_pos={dc_pos:.2f} vol_z={vol_z:.2f} body={body_ratio:.2f}",
            {"vol_z": vol_z, "dc_pos": dc_pos},
        ).clamped()


class VwapPullbackStrategy(Strategy):
    """Buy the pullback to VWAP inside an established trend."""

    name = "vwap_pullback"
    weight = 1.0
    regimes = ("trend_up", "trend_down")

    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        vwap_dist = self.last(df, "vwap_dist")
        prev_dist = self.prev(df, "vwap_dist")
        di_spread = self.last(df, "di_spread")
        adx = self.last(df, "adx")
        rsi = self.last(df, "rsi", 50.0)

        if adx < 20:
            return Vote(self.name, 0.0, "no trend to pull back into")

        score = 0.0
        trend_up = di_spread > 0
        # price dipped below VWAP in an uptrend and is turning back
        if trend_up and -1.2 < vwap_dist < 0.05 and vwap_dist > prev_dist:
            score += 0.55
            if 40 <= rsi <= 60:
                score += 0.2
        elif not trend_up and -0.05 < vwap_dist < 1.2 and vwap_dist < prev_dist:
            score -= 0.55
            if 40 <= rsi <= 60:
                score -= 0.2

        score += 0.15 * max(-1.0, min(1.0, di_spread / 25))

        return Vote(
            self.name,
            score,
            f"vwap_dist={vwap_dist:.3f} di_spread={di_spread:.1f} adx={adx:.1f}",
            {"vwap_dist": vwap_dist},
        ).clamped()


class MomentumBurstStrategy(Strategy):
    """Short-horizon continuation: a volume-backed impulse candle."""

    name = "momentum_burst"
    weight = 0.9
    regimes = ("trend_up", "trend_down", "volatile", "transition")

    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        ret_3 = self.last(df, "ret_3")
        ret_6 = self.last(df, "ret_6")
        vol_z = self.last(df, "vol_z")
        atr_pct = self.last(df, "atr_pct", 0.5)
        mfi = self.last(df, "mfi", 50.0)
        body_ratio = self.last(df, "body_ratio")

        if atr_pct <= 0:
            return Vote(self.name, 0.0, "no volatility reading")

        # normalise the impulse by current volatility
        impulse = ret_3 / max(atr_pct, 0.05)
        score = 0.4 * max(-1.0, min(1.0, impulse / 2.0))

        if vol_z > 1.2 and body_ratio > 0.5:
            score += 0.25 * (1 if ret_3 > 0 else -1)
        # money flow agreeing with price
        if mfi > 60 and ret_6 > 0:
            score += 0.2
        elif mfi < 40 and ret_6 < 0:
            score -= 0.2

        # An exhausted move is not a continuation.
        if abs(ret_6) > 4 * atr_pct:
            score *= 0.3

        return Vote(
            self.name,
            score,
            f"ret_3={ret_3:.2f} vol_z={vol_z:.2f} mfi={mfi:.0f}",
            {"ret_3": ret_3, "vol_z": vol_z},
        ).clamped()


class StructureStrategy(Strategy):
    """Respect recent swing highs/lows: buy near support, sell near resistance."""

    name = "structure"
    weight = 0.8

    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        close = self.last(df, "close")
        swing_high = self.last(df, "swing_high")
        swing_low = self.last(df, "swing_low")
        atr = self.last(df, "atr")

        if not close or not atr or not swing_high or not swing_low:
            return Vote(self.name, 0.0, "structure not established")

        to_support = (close - swing_low) / atr
        to_resistance = (swing_high - close) / atr

        score = 0.0
        if 0 <= to_support < 1.0:
            score += 0.5 * (1 - to_support)
        if 0 <= to_resistance < 1.0:
            score -= 0.5 * (1 - to_resistance)

        # breaking structure flips the sign
        if close > swing_high:
            score += 0.3
        elif close < swing_low:
            score -= 0.3

        return Vote(
            self.name,
            score,
            f"support={to_support:.2f}atr resistance={to_resistance:.2f}atr",
            {"to_support": to_support, "to_resistance": to_resistance},
        ).clamped()


ALL_STRATEGIES: list[Strategy] = [
    EmaTrendStrategy(),
    RsiReversionStrategy(),
    SqueezeBreakoutStrategy(),
    VwapPullbackStrategy(),
    MomentumBurstStrategy(),
    StructureStrategy(),
]
