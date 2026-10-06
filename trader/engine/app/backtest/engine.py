"""Vectorised-enough backtester.

It replays candles through the *same* feature builder, the same ensemble and
the same exit rules the live engine uses, so a result here means something.
What it deliberately models:
  * taker fees on both sides, both subtracted from each trade's PnL
  * slippage
  * one position at a time per symbol
  * intrabar stop/target hits, using the candle's high and low
  * the same 48-hour time stop as the live risk manager, whatever the bar size
  * the trailing stop ratcheting on the candle close, as the live engine does
  * the higher-timeframe gate, when HTF candles are supplied - fed only with
    HTF bars that had closed by the time the decision bar closed

What it cannot model: order book depth, and the ML/LLM layers (the model is
trained on this same history, so including it would leak).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.config import settings
from app.features.build import build_features
from app.strategies.ensemble import Ensemble
from app.util.timeframes import tf_seconds

log = logging.getLogger(__name__)

SLIPPAGE_PCT = 0.02
WARMUP = 210


@dataclass
class BTPosition:
    side: str
    entry_price: float
    qty: float
    stop: float
    target: float
    initial_stop: float
    entry_index: int
    high_water: float
    fees: float = 0.0
    trail_active: bool = False


@dataclass
class BTTrade:
    symbol: str
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    fees: float
    reason: str
    bars_held: int


def backtest_symbol(
    symbol: str,
    timeframe: str,
    candles: pd.DataFrame,
    starting_capital: float = 100.0,
    htf_candles: pd.DataFrame | None = None,
    htf_timeframe: str | None = None,
) -> dict:
    features = build_features(candles)
    ensemble = Ensemble()

    # Mirrors RiskManager.should_exit: 48 hours, not a fixed bar count.
    time_stop_bars = int(48 * 3600 / tf_seconds(timeframe))
    open_times = features["open_time"].to_numpy()
    bar_ms = tf_seconds(timeframe) * 1000

    # The live engine passes the HTF frame once it holds 60 candles; the
    # backtest only shows bars that had closed when the decision bar closed.
    htf_feat: pd.DataFrame | None = None
    htf_close = np.empty(0)
    if htf_candles is not None and htf_timeframe and len(htf_candles) >= 60:
        htf_feat = build_features(htf_candles)
        htf_close = htf_feat["open_time"].to_numpy() + tf_seconds(htf_timeframe) * 1000

    equity = starting_capital
    peak = starting_capital
    max_drawdown = 0.0
    equity_curve: list[float] = []
    trades: list[BTTrade] = []
    position: BTPosition | None = None

    fee_rate = settings.taker_fee_pct / 100
    slip = SLIPPAGE_PCT / 100

    highs = features["high"].to_numpy()
    lows = features["low"].to_numpy()
    closes = features["close"].to_numpy()
    atrs = features["atr"].to_numpy()

    for i in range(WARMUP, len(features)):
        price = closes[i]

        # ---------------- manage an open position ----------------
        if position is not None:
            exit_price = None
            reason = ""

            if position.side == "long":
                # a candle that touches both is assumed to hit the stop first
                if lows[i] <= position.stop:
                    exit_price, reason = position.stop, "stop_loss"
                elif highs[i] >= position.target:
                    exit_price, reason = position.target, "take_profit"
            else:
                if highs[i] >= position.stop:
                    exit_price, reason = position.stop, "stop_loss"
                elif lows[i] <= position.target:
                    exit_price, reason = position.target, "take_profit"

            # time stop
            if exit_price is None and i - position.entry_index > time_stop_bars:
                move = abs(price - position.entry_price) / position.entry_price * 100
                if move < settings.round_trip_fee_pct * 2:
                    exit_price, reason = price, "time_stop"

            # trailing stop
            if exit_price is None and settings.trailing_enabled and np.isfinite(atrs[i]):
                risk = abs(position.entry_price - position.initial_stop)
                if risk > 0:
                    gain = (
                        price - position.entry_price
                        if position.side == "long"
                        else position.entry_price - price
                    )
                    if gain / risk >= settings.trailing_activate_r:
                        if position.side == "long":
                            position.high_water = max(position.high_water, price)
                            new_stop = max(
                                position.high_water - atrs[i] * settings.trailing_atr_mult,
                                position.entry_price * 1.0005,
                            )
                            if new_stop > position.stop:
                                position.stop = new_stop
                                position.trail_active = True
                        else:
                            position.high_water = min(position.high_water, price)
                            new_stop = min(
                                position.high_water + atrs[i] * settings.trailing_atr_mult,
                                position.entry_price * 0.9995,
                            )
                            if new_stop < position.stop:
                                position.stop = new_stop
                                position.trail_active = True

            if exit_price is not None:
                fill = exit_price * (1 - slip if position.side == "long" else 1 + slip)
                exit_fee = fill * position.qty * fee_rate
                gross = (
                    (fill - position.entry_price) * position.qty
                    if position.side == "long"
                    else (position.entry_price - fill) * position.qty
                )
                total_fees = position.fees + exit_fee
                pnl = gross - total_fees  # trade PnL net of both fees
                equity += gross - exit_fee  # the entry fee left equity at entry

                basis = position.entry_price * position.qty
                risk_amount = (
                    abs(position.entry_price - position.initial_stop) * position.qty
                )
                trades.append(
                    BTTrade(
                        symbol=symbol,
                        side=position.side,
                        entry_index=position.entry_index,
                        exit_index=i,
                        entry_price=position.entry_price,
                        exit_price=fill,
                        qty=position.qty,
                        pnl=pnl,
                        pnl_pct=pnl / basis * 100 if basis else 0.0,
                        r_multiple=pnl / risk_amount if risk_amount else 0.0,
                        fees=total_fees,
                        reason=reason,
                        bars_held=i - position.entry_index,
                    )
                )
                position = None

        # ---------------- look for an entry ----------------
        if position is None and equity > starting_capital * 0.15:
            window = features.iloc[: i + 1]
            htf_window = None
            if htf_feat is not None:
                n = int(np.searchsorted(htf_close, open_times[i] + bar_ms, side="right"))
                htf_window = htf_feat.iloc[:n] if n >= 60 else None
            decision = ensemble.evaluate(symbol, timeframe, window, htf_window, None)

            # same session rule as the live risk manager
            bar_time = pd.Timestamp(int(features["open_time"].iloc[i]), unit="ms", tz="UTC")
            session_blocked = (
                (settings.skip_weekend_entries and bar_time.weekday() >= 5)
                or bar_time.hour in settings.skip_entry_hours_utc
            )
            if decision.tradable and decision.side == "long" and not session_blocked:
                # Shorts need futures; the backtest stays long-only to match
                # the default configuration.
                stop_distance = abs(decision.price - decision.stop_price)
                if stop_distance > 0:
                    risk_amount = equity * (settings.risk_per_trade_pct / 100)
                    qty = risk_amount / stop_distance
                    max_notional = equity * (settings.max_position_pct / 100)
                    if qty * decision.price > max_notional:
                        qty = max_notional / decision.price

                    notional = qty * decision.price
                    if notional >= 5.0 and qty > 0:
                        fill = decision.price * (1 + slip)
                        entry_fee = fill * qty * fee_rate
                        equity -= entry_fee
                        position = BTPosition(
                            side="long",
                            entry_price=fill,
                            qty=qty,
                            stop=decision.stop_price,
                            target=decision.target_price,
                            initial_stop=decision.stop_price,
                            entry_index=i,
                            high_water=fill,
                            fees=entry_fee,
                        )

        # ---------------- bookkeeping ----------------
        mark_equity = equity
        if position is not None:
            direction = 1 if position.side == "long" else -1
            mark_equity += (price - position.entry_price) * position.qty * direction
        equity_curve.append(round(mark_equity, 4))
        peak = max(peak, mark_equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - mark_equity) / peak * 100)

    # ---------------- summary ----------------
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    net = sum(t.pnl for t in trades)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": int(len(features)),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "net_pnl": round(net, 4),
        "net_pnl_pct": round(net / starting_capital * 100, 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0,
        "max_drawdown_pct": round(max_drawdown, 3),
        "avg_r": round(sum(t.r_multiple for t in trades) / len(trades), 3) if trades else 0.0,
        "expectancy": round(net / len(trades), 4) if trades else 0.0,
        "fees_paid": round(sum(t.fees for t in trades), 4),
        # thin the curve so the payload stays small
        "equity_curve": equity_curve[:: max(1, len(equity_curve) // 500)],
        "trade_log": [
            {
                "side": t.side,
                "entry_price": round(t.entry_price, 6),
                "exit_price": round(t.exit_price, 6),
                "qty": round(t.qty, 8),
                "pnl": round(t.pnl, 4),
                "pnl_pct": round(t.pnl_pct, 3),
                "r": round(t.r_multiple, 3),
                "reason": t.reason,
                "bars": t.bars_held,
            }
            for t in trades[-200:]
        ],
    }
