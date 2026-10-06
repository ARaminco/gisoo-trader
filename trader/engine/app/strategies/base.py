"""Strategy contract.

Each strategy looks at the last row of a feature frame and returns a vote in
[-1, 1]: positive is long, negative is short, zero is no opinion. The ensemble
combines the votes; no single strategy can open a trade on its own.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Vote:
    strategy: str
    score: float  # -1..1
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def clamped(self) -> "Vote":
        self.score = max(-1.0, min(1.0, float(self.score)))
        return self


class Strategy(ABC):
    name: str = "unnamed"
    # Weight in the ensemble, and the regimes where this strategy is trusted.
    weight: float = 1.0
    regimes: tuple[str, ...] = ("trend_up", "trend_down", "range", "transition", "volatile")

    @abstractmethod
    def evaluate(self, df: pd.DataFrame, regime: str) -> Vote:
        """Return this strategy's vote for the most recent completed candle."""

    def applies_to(self, regime: str) -> bool:
        return regime in self.regimes

    # -- helpers available to every strategy --

    @staticmethod
    def last(df: pd.DataFrame, column: str, default: float = 0.0) -> float:
        if column not in df.columns or df.empty:
            return default
        value = df[column].iloc[-1]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return default if pd.isna(value) else value

    @staticmethod
    def prev(df: pd.DataFrame, column: str, offset: int = 2, default: float = 0.0) -> float:
        if column not in df.columns or len(df) < offset:
            return default
        value = df[column].iloc[-offset]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return default if pd.isna(value) else value
