"""Portfolio simulator.

Replays every symbol of a window through ONE shared equity pool with the same
rules the live risk manager enforces: at most N concurrent positions, risk per
trade from the ATR stop distance, position cap, exchange minimum, fee hurdle,
cooldown after a loss, daily-loss pause, max-drawdown kill switch, trailing and
time stops.

Fills are pessimistic: a bar whose open gaps through the stop fills at the
open, not at the stop; a bar that touches both stop and target counts the stop.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from app.util.timeframes import tf_seconds


@dataclass
class SimParams:
    min_score: float = 0.62
    min_move_pct: float = 0.8
    atr_stop: float = 1.8
    atr_target: float = 3.0
    risk_pct: float = 1.5
    max_positions: int = 3
    max_position_pct: float = 35.0
    min_notional: float = 5.0
    fee_pct: float = 0.075          # per side
    slippage_pct: float = 0.02
    trailing: bool = True
    trail_activate_r: float = 1.0
    trail_atr: float = 1.4
    daily_loss_pct: float = 5.0
    max_dd_pct: float = 20.0
    cooldown_min: int = 45
    time_stop_hours: float = 48.0
    min_rr: float = 1.3
    fee_hurdle_mult: float = 2.5
    allow_short: bool = False
    # experimental gates the analysts can switch on
    vol_breaker: float | None = None       # skip entries when atr_ratio > this
    blocked_regimes: tuple[str, ...] = ()  # skip entries in these regimes
    min_adx: float | None = None           # skip entries below this ADX
    # Only take a long when the 4h bias is at least this (-1 bearish .. +1
    # bullish). 0 means "never buy against a bearish higher timeframe".
    min_htf_bias: float | None = None
    # Forensics gates (docs/research-loss-forensics.md): entries in quiet
    # volatility lose; weekends and the 00-04 UTC session lose; some symbols
    # never work.
    min_atr_ratio: float | None = None
    min_atr_pct: float | None = None
    skip_weekend: bool = False
    skip_hours: tuple[int, ...] = ()
    blocked_symbols: tuple[str, ...] = ()
    size_multiplier: float = 1.0

    def key(self) -> str:
        return (
            f"s{self.min_score}_m{self.min_move_pct}_st{self.atr_stop}_tg{self.atr_target}"
            f"_r{self.risk_pct}_n{self.max_positions}"
            + (f"_vb{self.vol_breaker}" if self.vol_breaker else "")
            + (f"_adx{self.min_adx}" if self.min_adx else "")
            + (f"_blk{'-'.join(self.blocked_regimes)}" if self.blocked_regimes else "")
            + (f"_htf{self.min_htf_bias}" if self.min_htf_bias is not None else "")
            + ("_short" if self.allow_short else "")
            + (f"_dd{self.max_dd_pct}" if self.max_dd_pct != 20.0 else "")
            + (f"_ar{self.min_atr_ratio}" if self.min_atr_ratio is not None else "")
            + (f"_ap{self.min_atr_pct}" if self.min_atr_pct is not None else "")
            + ("_nowknd" if self.skip_weekend else "")
            + (f"_noh{'-'.join(map(str, self.skip_hours))}" if self.skip_hours else "")
            + (f"_nosym{len(self.blocked_symbols)}" if self.blocked_symbols else "")
        )


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    notional: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    fees: float
    reason: str
    bars_held: int
    score: float
    regime: str
    gap_fill: bool


@dataclass
class _Position:
    symbol: str
    side: str
    qty: float
    entry: float
    stop: float
    target: float
    initial_stop: float
    high_water: float
    entry_time: int
    entry_bar: int
    fees: float
    score: float
    regime: str
    trail_active: bool = False


@dataclass
class SimResult:
    params: dict
    starting_capital: float
    final_equity: float
    trades: list[Trade]
    equity_curve: list[tuple[int, float]]
    metrics: dict = field(default_factory=dict)
    kill_triggered_at: int | None = None
    daily_pauses: int = 0


def _utc_day(ms: int) -> int:
    return ms // 86_400_000


def simulate(
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    params: SimParams,
    starting_capital: float = 100.0,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> SimResult:
    """`frames` maps symbol -> decisions frame (from research.decisions)."""
    tf_ms = tf_seconds(timeframe) * 1000
    bar_hours = tf_ms / 3_600_000
    fee = params.fee_pct / 100
    slip = params.slippage_pct / 100
    fee_round_trip_pct = params.fee_pct * 2

    # ---- index the data for O(1) per-bar lookups ----
    lookup: dict[str, dict[int, tuple]] = {}
    all_times: set[int] = set()
    for symbol, df in frames.items():
        sub = df
        if start_ms is not None:
            sub = sub[sub["open_time"] >= start_ms]
        if end_ms is not None:
            sub = sub[sub["open_time"] < end_ms]
        if sub.empty:
            continue
        table: dict[int, tuple] = {}
        cols = ["open", "high", "low", "close", "side", "score", "atr", "atr_ratio", "regime", "adx", "htf_bias", "atr_pct"]
        if "htf_bias" not in sub.columns:
            sub = sub.assign(htf_bias=0.0)
        if "atr_pct" not in sub.columns:
            sub = sub.assign(atr_pct=0.0)
        for values in sub[["open_time", *cols]].itertuples(index=False, name=None):
            table[int(values[0])] = values[1:]
        lookup[symbol] = table
        all_times.update(table.keys())

    times = sorted(all_times)
    if not times:
        return SimResult(asdict(params), starting_capital, starting_capital, [], [], {})

    cash = starting_capital
    positions: dict[str, _Position] = {}
    trades: list[Trade] = []
    curve: list[tuple[int, float]] = []
    last_close: dict[str, float] = {}
    last_loss_time: dict[str, int] = {}
    peak = starting_capital
    day = _utc_day(times[0])
    day_start_equity = starting_capital
    paused_until_day: int | None = None
    killed_at: int | None = None
    daily_pauses = 0
    bar_index = 0

    def mark_equity() -> float:
        total = cash
        for pos in positions.values():
            price = last_close.get(pos.symbol, pos.entry)
            if pos.side == "long":
                total += pos.qty * price
            else:
                total += pos.qty * pos.entry + (pos.entry - price) * pos.qty
        return total

    def close_position(pos: _Position, fill: float, t: int, reason: str, gap: bool) -> None:
        nonlocal cash
        exit_fee = fill * pos.qty * fee
        if pos.side == "long":
            gross = (fill - pos.entry) * pos.qty
            cash += pos.qty * fill - exit_fee
        else:
            gross = (pos.entry - fill) * pos.qty
            cash += pos.qty * pos.entry + gross - exit_fee
        # Both fees count against the trade - cash already paid the entry fee,
        # so per-trade PnL must match what the equity curve shows.
        pnl = gross - exit_fee - pos.fees
        basis = pos.entry * pos.qty
        risk = abs(pos.entry - pos.initial_stop) * pos.qty
        trades.append(
            Trade(
                symbol=pos.symbol,
                side=pos.side,
                entry_time=pos.entry_time,
                exit_time=t,
                entry_price=pos.entry,
                exit_price=fill,
                qty=pos.qty,
                notional=basis,
                pnl=pnl,
                pnl_pct=pnl / basis * 100 if basis else 0.0,
                r_multiple=pnl / risk if risk else 0.0,
                fees=pos.fees + exit_fee,
                reason=reason,
                bars_held=bar_index - pos.entry_bar,
                score=pos.score,
                regime=pos.regime,
                gap_fill=gap,
            )
        )
        if pnl < 0:
            last_loss_time[pos.symbol] = t
        del positions[pos.symbol]

    for t in times:
        bar_index += 1

        # ---- new UTC day: reset the daily loss baseline ----
        today = _utc_day(t)
        if today != day:
            day = today
            day_start_equity = mark_equity()

        # ---- manage open positions on this bar ----
        for symbol in list(positions.keys()):
            bar = lookup.get(symbol, {}).get(t)
            if bar is None:
                continue
            o, h, l, c, _side, _score, atr, _ratio, _regime, _adx, _htf, _ap = bar
            last_close[symbol] = c
            pos = positions[symbol]
            long = pos.side == "long"

            exit_fill: float | None = None
            reason = ""
            gap = False

            # gap through the stop at the open -> filled at the open
            if long and o <= pos.stop:
                exit_fill, reason, gap = o, "stop_loss", True
            elif not long and o >= pos.stop:
                exit_fill, reason, gap = o, "stop_loss", True
            elif long and l <= pos.stop:
                exit_fill, reason = pos.stop, "stop_loss"
            elif not long and h >= pos.stop:
                exit_fill, reason = pos.stop, "stop_loss"
            elif long and o >= pos.target:
                exit_fill, reason, gap = o, "take_profit", True
            elif not long and o <= pos.target:
                exit_fill, reason, gap = o, "take_profit", True
            elif long and h >= pos.target:
                exit_fill, reason = pos.target, "take_profit"
            elif not long and l <= pos.target:
                exit_fill, reason = pos.target, "take_profit"

            if exit_fill is None and (bar_index - pos.entry_bar) * bar_hours > params.time_stop_hours:
                move = abs(c - pos.entry) / pos.entry * 100
                if move < fee_round_trip_pct * 2:
                    exit_fill, reason = c, "time_stop"

            if exit_fill is None and params.trailing and atr > 0:
                risk = abs(pos.entry - pos.initial_stop)
                if risk > 0:
                    gain = (c - pos.entry) if long else (pos.entry - c)
                    if gain / risk >= params.trail_activate_r:
                        if long:
                            pos.high_water = max(pos.high_water, h)
                            new_stop = max(pos.high_water - atr * params.trail_atr, pos.entry * 1.0005)
                            if new_stop > pos.stop:
                                pos.stop, pos.trail_active = new_stop, True
                        else:
                            pos.high_water = min(pos.high_water, l)
                            new_stop = min(pos.high_water + atr * params.trail_atr, pos.entry * 0.9995)
                            if new_stop < pos.stop:
                                pos.stop, pos.trail_active = new_stop, True

            if exit_fill is not None:
                fill = exit_fill * (1 - slip) if long else exit_fill * (1 + slip)
                close_position(pos, fill, t, reason, gap)

        # update marks for symbols without positions too
        for symbol, table in lookup.items():
            bar = table.get(t)
            if bar is not None:
                last_close[symbol] = bar[3]

        equity = mark_equity()
        peak = max(peak, equity)
        curve.append((t, equity))

        # ---- account-level gates ----
        if killed_at is None and peak > 0 and (peak - equity) / peak * 100 >= params.max_dd_pct:
            killed_at = t
        if paused_until_day is not None and today >= paused_until_day:
            paused_until_day = None
        if paused_until_day is None and day_start_equity > 0:
            if (equity - day_start_equity) / day_start_equity * 100 <= -params.daily_loss_pct:
                paused_until_day = today + 1
                daily_pauses += 1

        if killed_at is not None or paused_until_day is not None:
            continue
        if len(positions) >= params.max_positions:
            continue

        # ---- entries: best candidates first ----
        if params.skip_weekend or params.skip_hours:
            stamp = dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc)
            if params.skip_weekend and stamp.weekday() >= 5:
                continue
            if params.skip_hours and stamp.hour in params.skip_hours:
                continue

        candidates: list[tuple[float, str, tuple]] = []
        for symbol, table in lookup.items():
            if symbol in positions or symbol in params.blocked_symbols:
                continue
            bar = table.get(t)
            if bar is None:
                continue
            o, h, l, c, side, score, atr, ratio, regime, adx, htf_bias, atr_pct = bar
            if side == "flat" or score < params.min_score or atr <= 0:
                continue
            if params.min_atr_ratio is not None and ratio < params.min_atr_ratio:
                continue
            if params.min_atr_pct is not None and atr_pct < params.min_atr_pct:
                continue
            if side == "short" and not params.allow_short:
                continue
            if params.min_htf_bias is not None:
                # longs need the higher timeframe on side; shorts the mirror
                if side == "long" and htf_bias < params.min_htf_bias:
                    continue
                if side == "short" and -htf_bias < params.min_htf_bias:
                    continue
            if params.vol_breaker is not None and ratio > params.vol_breaker:
                continue
            if regime in params.blocked_regimes:
                continue
            if params.min_adx is not None and adx < params.min_adx:
                continue
            last_loss = last_loss_time.get(symbol)
            if last_loss is not None and (t - last_loss) < params.cooldown_min * 60_000:
                continue
            candidates.append((score, symbol, bar))

        candidates.sort(key=lambda item: item[0], reverse=True)
        free = cash
        for score, symbol, bar in candidates:
            if len(positions) >= params.max_positions:
                break
            o, h, l, c, side, _score, atr, ratio, regime, adx, _htf, _ap = bar
            price = c
            stop_distance = atr * params.atr_stop
            target_distance = atr * params.atr_target
            expected_move_pct = target_distance / price * 100
            min_move = max(params.min_move_pct, fee_round_trip_pct * params.fee_hurdle_mult)
            if expected_move_pct < min_move:
                continue
            if target_distance / stop_distance < params.min_rr:
                continue

            risk_amount = equity * (params.risk_pct / 100) * params.size_multiplier
            qty = risk_amount / stop_distance
            max_notional = min(equity * params.max_position_pct / 100, free * 0.98)
            if qty * price > max_notional:
                qty = max_notional / price
            notional = qty * price
            if notional < params.min_notional:
                min_qty = params.min_notional * 1.02 / price
                if min_qty * stop_distance <= risk_amount * 1.6 and min_qty * price <= max_notional:
                    qty = min_qty
                    notional = qty * price
                else:
                    continue
            if qty <= 0:
                continue

            fee_cost = notional * fee_round_trip_pct / 100
            if target_distance * qty <= fee_cost * params.fee_hurdle_mult:
                continue

            long = side == "long"
            fill = price * (1 + slip) if long else price * (1 - slip)
            entry_fee = fill * qty * fee
            if long:
                cash -= fill * qty + entry_fee
            else:
                cash -= fill * qty + entry_fee  # margin locked at 1x
            free = cash
            positions[symbol] = _Position(
                symbol=symbol,
                side=side,
                qty=qty,
                entry=fill,
                stop=fill - stop_distance if long else fill + stop_distance,
                target=fill + target_distance if long else fill - target_distance,
                initial_stop=fill - stop_distance if long else fill + stop_distance,
                high_water=fill,
                entry_time=t,
                entry_bar=bar_index,
                fees=entry_fee,
                score=score,
                regime=regime,
            )

    # ---- close whatever is still open at the last price ----
    final_t = times[-1]
    for symbol in list(positions.keys()):
        pos = positions[symbol]
        price = last_close.get(symbol, pos.entry)
        fill = price * (1 - slip) if pos.side == "long" else price * (1 + slip)
        close_position(pos, fill, final_t, "end_of_window", False)

    result = SimResult(
        params=asdict(params),
        starting_capital=starting_capital,
        final_equity=cash,
        trades=trades,
        equity_curve=curve,
        kill_triggered_at=killed_at,
        daily_pauses=daily_pauses,
    )
    result.metrics = compute_metrics(result, timeframe)
    return result


def compute_metrics(result: SimResult, timeframe: str) -> dict:
    trades = result.trades
    start = result.starting_capital
    curve = pd.Series(
        [e for _, e in result.equity_curve],
        index=pd.to_datetime([t for t, _ in result.equity_curve], unit="ms", utc=True),
    )
    if curve.empty:
        return {"trades": 0}

    running_peak = curve.cummax()
    drawdown = (running_peak - curve) / running_peak * 100
    daily = curve.resample("1D").last().dropna()
    daily_ret = daily.pct_change().dropna()
    sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(365)) if len(daily_ret) > 2 and daily_ret.std() > 0 else 0.0
    downside = daily_ret[daily_ret < 0]
    sortino = float(daily_ret.mean() / downside.std() * math.sqrt(365)) if len(downside) > 2 and downside.std() > 0 else 0.0

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    net = result.final_equity - start

    streak = worst_streak = 0
    for t in trades:
        streak = streak + 1 if t.pnl <= 0 else 0
        worst_streak = max(worst_streak, streak)

    total_bars = len(result.equity_curve)
    bars_in_market = sum(t.bars_held for t in trades)
    days = max(1, (curve.index[-1] - curve.index[0]).days)

    reasons: dict[str, dict] = {}
    for t in trades:
        entry = reasons.setdefault(t.reason, {"count": 0, "pnl": 0.0})
        entry["count"] += 1
        entry["pnl"] += t.pnl

    by_regime: dict[str, dict] = {}
    for t in trades:
        entry = by_regime.setdefault(t.regime, {"count": 0, "pnl": 0.0, "wins": 0})
        entry["count"] += 1
        entry["pnl"] += t.pnl
        entry["wins"] += 1 if t.pnl > 0 else 0

    by_symbol: dict[str, dict] = {}
    for t in trades:
        entry = by_symbol.setdefault(t.symbol, {"count": 0, "pnl": 0.0, "wins": 0})
        entry["count"] += 1
        entry["pnl"] += t.pnl
        entry["wins"] += 1 if t.pnl > 0 else 0

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "net_pnl": net,
        "net_pct": net / start * 100,
        "monthly_pct": (net / start * 100) / days * 30,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0),
        "max_dd_pct": float(drawdown.max()),
        "avg_r": float(np.mean([t.r_multiple for t in trades])) if trades else 0.0,
        "expectancy": net / len(trades) if trades else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "fees": sum(t.fees for t in trades),
        "best_trade": max((t.pnl for t in trades), default=0.0),
        "worst_trade": min((t.pnl for t in trades), default=0.0),
        "worst_streak": worst_streak,
        "exposure_pct": bars_in_market / (total_bars * 1.0) * 100 if total_bars else 0.0,
        "avg_bars_held": bars_in_market / len(trades) if trades else 0.0,
        "gap_fills": sum(1 for t in trades if t.gap_fill),
        "kill_triggered": result.kill_triggered_at is not None,
        "daily_pauses": result.daily_pauses,
        "days": days,
        "exit_reasons": reasons,
        "by_regime": by_regime,
        "by_symbol": by_symbol,
    }


def buy_and_hold(frames: dict[str, pd.DataFrame], start_ms: int, end_ms: int) -> dict:
    """Equal-weight hold of the same universe, and BTC alone, over the window."""
    returns: dict[str, float] = {}
    for symbol, df in frames.items():
        sub = df[(df["open_time"] >= start_ms) & (df["open_time"] < end_ms)]
        if len(sub) < 2:
            continue
        returns[symbol] = (sub["close"].iloc[-1] / sub["close"].iloc[0] - 1) * 100
    if not returns:
        return {"equal_weight_pct": 0.0, "btc_pct": 0.0}
    return {
        "equal_weight_pct": float(np.mean(list(returns.values()))),
        "btc_pct": float(returns.get("BTCUSDT", 0.0)),
        "per_symbol": returns,
    }


def trades_frame(result: SimResult) -> pd.DataFrame:
    if not result.trades:
        return pd.DataFrame()
    df = pd.DataFrame([asdict(t) for t in result.trades])
    df["entry_at"] = pd.to_datetime(df["entry_time"], unit="ms", utc=True)
    df["exit_at"] = pd.to_datetime(df["exit_time"], unit="ms", utc=True)
    return df
