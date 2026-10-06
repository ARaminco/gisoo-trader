"""Portfolio simulator with three optional hooks for the alt candidates.

`simulate_ext` is `app.research.portfolio.simulate` copied line for line, plus:

  * per-bar `stop_dist` / `target_dist` columns (absolute price distance)
    that replace `atr * atr_stop` / `atr * atr_target` on the entry bar when
    > 0 - needed for "stop below the swing low" and "stop at the compression
    low";
  * an `exit` column: 1 closes an open position at that bar close with
    reason "signal_exit" - needed for "exit when RSI(2) > 65" and "hold until
    it drops out of the top 5";
  * `hard_time_stop_hours`: close at the bar close after N hours regardless
    of PnL (the built-in time stop only closes trades that went nowhere).

The profit-protection study (docs/research-profit-protection.md) added four
more, all off by default: a close-based ATR trail (`trail_close_atr`), a
give-back stop (`giveback_peak_pct`), a crash guard on BTC / symbol crash bars
(`crash_btc_pct`, `crash_symbol_atr`, exit or entry-block mode) and a percent
take-profit (`target_pct`). A stop raised by one of them is reported as
`protect_stop`, a crash exit as `crash_guard`.

The scaling study (docs/research-scaling.md) added staged exits and staged
entries, also off by default: `scale_out` sells fractions of the position at
percent targets (each leg is a trade row with `partial=True`, fees on the sold
part), `remainder_giveback_pct` raises the stop of what is left once every leg
is sold, and `scale_in` opens a first tranche at the signal and adds the rest
on confirmation (turtle-style +k ATR above the previous fill, or a pullback to
EMA20 / previous fill - 0.5 ATR); an add recomputes the average entry and
raises the stop to max(stop, fill - atr_stop x ATR) as the classic Turtle rules
do. The optional `ema20` frame column feeds the pullback rule (0 = absent).
Rows are `AltTrade` (Trade plus partial / leg / adds / under_min), so the
published columns are unchanged.

With the hook columns at 0 and every hook parameter at its default the loop is
the original; `python -m app.research.alt.run --parity` proves it on real
frames and `app.research.alt.protection --variants base` (and
`app.research.alt.scaling --parity`) reproduce the two live families'
published per-window numbers exactly. Fees, slippage, sizing, gap fills,
trailing, daily-loss pause and the kill switch are untouched so the numbers
stay comparable with the baseline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from app.research.portfolio import SimParams, SimResult, Trade, _Position, _utc_day, compute_metrics
from app.util.timeframes import tf_seconds

HOOK_COLUMNS = ("stop_dist", "target_dist", "exit")
# optional per-bar inputs of the scaling hooks; 0 when a frame does not carry them
OPT_COLUMNS = ("ema20",)


@dataclass
class AltTrade(Trade):
    """Trade row plus the scaling bookkeeping (all defaults = a plain full close)."""

    partial: bool = False     # a scale-out leg; the round trip continues
    leg: int = 0              # 1..n for scale-out legs, 0 for the final close
    adds: int = 0             # scale-in adds filled before this row
    under_min: bool = False   # sold notional below params.min_notional (exchange would reject it)
    adds_under_min: int = 0   # adds whose notional was below params.min_notional


@dataclass
class _AltPosition(_Position):
    """_Position plus the scaling state; a plain position leaves the defaults."""

    initial_qty: float = 0.0    # qty the scale-out fractions refer to (after any add)
    legs_done: int = 0
    adds_done: int = 0
    last_fill: float = 0.0
    last_fill_atr: float = 0.0
    tranche_risk: float = 0.0   # USDT risk budget of one scale-in tranche
    adds_under_min: int = 0


@dataclass
class AltParams(SimParams):
    """SimParams plus the hooks. Defaults keep the original behaviour."""

    hard_time_stop_hours: float | None = None
    exit_on_signal: bool = True
    frame_stops: bool = True
    # ---- profit-protection hooks (docs/research-profit-protection.md); None = off ----
    # ATR trail from the highest close since entry: stop = max(stop, peak_close - k * ATR),
    # armed once the peak close is >= activate_r R above the entry (0 = from the first bar).
    trail_close_atr: float | None = None
    trail_close_activate_r: float = 1.0
    # Give-back rule: once the peak close is >= G % above the entry,
    # stop = max(stop, entry * (1 + keep * peak_gain)).
    giveback_peak_pct: float | None = None
    giveback_keep: float = 0.5
    # Crash guard: a bar where BTC closes more than X % under its open, or the symbol closes
    # more than m ATR under its open. Mode "exit" closes every open position at that close
    # (and takes no entry on that bar); mode "block" only refuses new entries for block_hours
    # (market-wide after a BTC crash bar, per symbol after a symbol crash bar).
    crash_btc_pct: float | None = None
    crash_symbol_atr: float | None = None
    crash_mode: str = "exit"
    crash_block_hours: float = 24.0
    crash_market_symbol: str = "BTCUSDT"
    # Profit lock: take profit at entry * (1 + T / 100), replacing the ATR target.
    target_pct: float | None = None
    # ---- scaling hooks (docs/research-scaling.md); None = off, long only ----
    # Scale-out legs ((target_pct, fraction_of_initial_qty), ...): leg i sells fraction_i of the
    # position at entry * (1 + T_i / 100), filled like a take-profit (the open on a gap, else the
    # level; a bar that also touches the stop is a stop). What is left rides the family's exit.
    scale_out: tuple[tuple[float, float], ...] | None = None
    # Give-back on the remainder once every leg is sold: with peak_gain the highest close since
    # entry in % of the average entry, stop = max(stop, entry * (1 + (peak_gain - G) / 100)).
    remainder_giveback_pct: float | None = None
    # Scale-in: "atr" adds at the close when close >= previous fill + step x ATR(previous fill);
    # "pullback" adds at the close when the bar's low touched EMA20 and the close is back above it,
    # or the close is <= previous fill - pullback_atr x ATR, while the close is above the stop.
    # One add per bar, none once a scale-out leg has been sold. tranches = total number of fills.
    scale_in: str | None = None
    scale_in_tranches: int = 3
    scale_in_step_atr: float = 0.5
    scale_in_pullback_atr: float = 0.5
    # "split": every tranche risks risk_pct / tranches (total planned risk = the base);
    # "extra": the first tranche risks the full risk_pct and every add risks it again (capped by
    # max_position_pct and the free cash, like an entry).
    scale_in_risk: str = "split"


def simulate_ext(
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    params: SimParams,
    starting_capital: float = 100.0,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> SimResult:
    tf_ms = tf_seconds(timeframe) * 1000
    bar_hours = tf_ms / 3_600_000
    fee = params.fee_pct / 100
    slip = params.slippage_pct / 100
    fee_round_trip_pct = params.fee_pct * 2
    hard_hours = getattr(params, "hard_time_stop_hours", None)
    exit_on_signal = getattr(params, "exit_on_signal", True)
    frame_stops = getattr(params, "frame_stops", True)
    # profit-protection hooks; every branch below is skipped when they are off
    trail_k = getattr(params, "trail_close_atr", None)
    trail_r = getattr(params, "trail_close_activate_r", 1.0)
    gb_pct = getattr(params, "giveback_peak_pct", None)
    gb_keep = getattr(params, "giveback_keep", 0.5)
    crash_btc = getattr(params, "crash_btc_pct", None)
    crash_atr = getattr(params, "crash_symbol_atr", None)
    crash_mode = getattr(params, "crash_mode", "exit")
    crash_block_ms = getattr(params, "crash_block_hours", 24.0) * 3_600_000
    crash_market = getattr(params, "crash_market_symbol", "BTCUSDT")
    crash_on = crash_btc is not None or crash_atr is not None
    target_pct = getattr(params, "target_pct", None)
    protect_on = trail_k is not None or gb_pct is not None
    # scaling hooks (docs/research-scaling.md); off unless set
    scale_out = tuple(getattr(params, "scale_out", None) or ())
    rem_gb = getattr(params, "remainder_giveback_pct", None)
    scale_in = getattr(params, "scale_in", None)
    tranches = int(getattr(params, "scale_in_tranches", 3))
    step_atr = float(getattr(params, "scale_in_step_atr", 0.5))
    pullback_atr = float(getattr(params, "scale_in_pullback_atr", 0.5))
    split_risk = getattr(params, "scale_in_risk", "split") == "split"
    scaling_on = bool(scale_out) or scale_in is not None
    if scale_out and sum(f for _, f in scale_out) >= 1.0:
        raise ValueError("scale_out fractions must leave a remainder (sum < 1)")

    def symbol_crash(o: float, c: float, atr: float) -> bool:
        return crash_atr is not None and atr > 0 and c < o - crash_atr * atr

    # ---- index the data for O(1) per-bar lookups ----
    lookup: dict[str, dict[int, tuple]] = {}
    all_times: set[int] = set()
    cols = ["open", "high", "low", "close", "side", "score", "atr", "atr_ratio", "regime", "adx", "htf_bias",
            *HOOK_COLUMNS, *OPT_COLUMNS]
    for symbol, df in frames.items():
        sub = df
        if start_ms is not None:
            sub = sub[sub["open_time"] >= start_ms]
        if end_ms is not None:
            sub = sub[sub["open_time"] < end_ms]
        if sub.empty:
            continue
        missing = {c: 0.0 for c in ("htf_bias", *HOOK_COLUMNS, *OPT_COLUMNS) if c not in sub.columns}
        if missing:
            sub = sub.assign(**missing)
        table: dict[int, tuple] = {}
        for values in sub[["open_time", *cols]].itertuples(index=False, name=None):
            table[int(values[0])] = values[1:]
        lookup[symbol] = table
        all_times.update(table.keys())

    times = sorted(all_times)
    if not times:
        return SimResult(asdict(params), starting_capital, starting_capital, [], [], {})

    cash = starting_capital
    positions: dict[str, _AltPosition] = {}
    trades: list[AltTrade] = []
    curve: list[tuple[int, float]] = []
    invested: list[float] = []            # market value of the open positions per bar (money-weighted view)
    last_close: dict[str, float] = {}
    last_loss_time: dict[str, int] = {}
    peak = starting_capital
    day = _utc_day(times[0])
    day_start_equity = starting_capital
    paused_until_day: int | None = None
    killed_at: int | None = None
    daily_pauses = 0
    bar_index = 0
    peak_close: dict[str, float] = {}     # highest close since entry, per open position
    protected: set[str] = set()           # positions whose stop a protection hook has raised
    market_block_until = 0                # crash guard, mode "block": no entries before this time
    symbol_block_until: dict[str, int] = {}

    def mark_equity() -> float:
        total = cash
        for pos in positions.values():
            price = last_close.get(pos.symbol, pos.entry)
            if pos.side == "long":
                total += pos.qty * price
            else:
                total += pos.qty * pos.entry + (pos.entry - price) * pos.qty
        return total

    def close_position(pos: _AltPosition, fill: float, t: int, reason: str, gap: bool) -> None:
        nonlocal cash
        exit_fee = fill * pos.qty * fee
        if pos.side == "long":
            gross = (fill - pos.entry) * pos.qty
            cash += pos.qty * fill - exit_fee
        else:
            gross = (pos.entry - fill) * pos.qty
            cash += pos.qty * pos.entry + gross - exit_fee
        pnl = gross - exit_fee - pos.fees
        basis = pos.entry * pos.qty
        risk = abs(pos.entry - pos.initial_stop) * pos.qty
        trades.append(
            AltTrade(
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
                adds=pos.adds_done,
                under_min=scaling_on and fill * pos.qty < params.min_notional,
                adds_under_min=pos.adds_under_min,
            )
        )
        if pnl < 0:
            last_loss_time[pos.symbol] = t
        del positions[pos.symbol]
        peak_close.pop(pos.symbol, None)
        protected.discard(pos.symbol)

    def close_leg(pos: _AltPosition, qty_leg: float, fill: float, t: int, gap: bool) -> None:
        """Scale-out: sell `qty_leg` of a long position, keep the rest open (its own trade row)."""
        nonlocal cash
        exit_fee = fill * qty_leg * fee
        gross = (fill - pos.entry) * qty_leg
        cash += qty_leg * fill - exit_fee
        fee_share = pos.fees * qty_leg / pos.qty     # entry (and add) fees pro rata to the sold part
        pnl = gross - exit_fee - fee_share
        basis = pos.entry * qty_leg
        risk = abs(pos.entry - pos.initial_stop) * qty_leg
        pos.legs_done += 1
        trades.append(
            AltTrade(
                symbol=pos.symbol,
                side=pos.side,
                entry_time=pos.entry_time,
                exit_time=t,
                entry_price=pos.entry,
                exit_price=fill,
                qty=qty_leg,
                notional=basis,
                pnl=pnl,
                pnl_pct=pnl / basis * 100 if basis else 0.0,
                r_multiple=pnl / risk if risk else 0.0,
                fees=fee_share + exit_fee,
                reason="scale_out",
                bars_held=bar_index - pos.entry_bar,
                score=pos.score,
                regime=pos.regime,
                gap_fill=gap,
                partial=True,
                leg=pos.legs_done,
                adds=pos.adds_done,
                under_min=fill * qty_leg < params.min_notional,
            )
        )
        pos.fees -= fee_share
        pos.qty -= qty_leg

    def add_tranche(pos: _AltPosition, price: float, atr: float, equity: float) -> None:
        """Scale-in: buy one more tranche at the close, average the entry, raise the stop."""
        nonlocal cash
        pos.adds_done += 1
        fill = price * (1 + slip)
        new_stop = max(pos.stop, fill - atr * params.atr_stop)
        distance = fill - new_stop
        if distance <= 0:
            return
        qty = pos.tranche_risk / distance
        room = min(equity * params.max_position_pct / 100 - pos.qty * price, cash * 0.98)
        if qty * fill > room:
            qty = room / fill
        if qty <= 0:
            return
        if fill * qty < params.min_notional:
            pos.adds_under_min += 1
        add_fee = fill * qty * fee
        cash -= fill * qty + add_fee
        pos.entry = (pos.entry * pos.qty + fill * qty) / (pos.qty + qty)
        pos.qty += qty
        pos.initial_qty = pos.qty
        pos.fees += add_fee
        pos.stop = new_stop
        pos.last_fill, pos.last_fill_atr = fill, atr

    for t in times:
        bar_index += 1

        today = _utc_day(t)
        if today != day:
            day = today
            day_start_equity = mark_equity()

        # ---- crash guard: is this a BTC crash bar? ----
        btc_crash = False
        if crash_btc is not None:
            mbar = lookup.get(crash_market, {}).get(t)
            btc_crash = mbar is not None and mbar[3] < mbar[0] * (1 - crash_btc / 100)
            if btc_crash and crash_mode == "block":
                market_block_until = max(market_block_until, int(t + crash_block_ms))

        # ---- manage open positions on this bar ----
        for symbol in list(positions.keys()):
            bar = lookup.get(symbol, {}).get(t)
            if bar is None:
                continue
            o, h, l, c, _side, _score, atr, _ratio, _regime, _adx, _htf, _sd, _td, exit_flag, ema20 = bar
            last_close[symbol] = c
            pos = positions[symbol]
            long = pos.side == "long"

            exit_fill: float | None = None
            reason = ""
            gap = False

            if long and o <= pos.stop:
                exit_fill, reason, gap = o, "stop_loss", True
            elif not long and o >= pos.stop:
                exit_fill, reason, gap = o, "stop_loss", True
            elif long and l <= pos.stop:
                exit_fill, reason = pos.stop, "stop_loss"
            elif not long and h >= pos.stop:
                exit_fill, reason = pos.stop, "stop_loss"
            elif long and scale_out and pos.legs_done < len(scale_out):
                # ---- hook: scale-out legs (a bar that touched the stop above is a stop) ----
                while pos.legs_done < len(scale_out):
                    t_pct, frac = scale_out[pos.legs_done]
                    level = pos.entry * (1 + t_pct / 100)
                    if o >= level:
                        leg_fill, leg_gap = o, True
                    elif h >= level:
                        leg_fill, leg_gap = level, False
                    else:
                        break
                    qty_leg = min(frac * pos.initial_qty, pos.qty)
                    close_leg(pos, qty_leg, leg_fill * (1 - slip), t, leg_gap)
            # the target checks are the original elif chain: a bar that hit the stop stays a stop
            if exit_fill is None:
                if long and o >= pos.target:
                    exit_fill, reason, gap = o, "take_profit", True
                elif not long and o <= pos.target:
                    exit_fill, reason, gap = o, "take_profit", True
                elif long and h >= pos.target:
                    exit_fill, reason = pos.target, "take_profit"
                elif not long and l <= pos.target:
                    exit_fill, reason = pos.target, "take_profit"

            held_hours = (bar_index - pos.entry_bar) * bar_hours
            if exit_fill is None and held_hours > params.time_stop_hours:
                move = abs(c - pos.entry) / pos.entry * 100
                if move < fee_round_trip_pct * 2:
                    exit_fill, reason = c, "time_stop"

            # ---- hooks: hard time stop, signal exit ----
            if exit_fill is None and hard_hours is not None and held_hours >= hard_hours:
                exit_fill, reason = c, "time_stop_hard"
            if exit_fill is None and exit_on_signal and exit_flag > 0:
                exit_fill, reason = c, "signal_exit"

            # ---- hook: crash guard ----
            if exit_fill is None and crash_on:
                own_crash = symbol_crash(o, c, atr)
                if crash_mode == "exit" and (btc_crash or own_crash):
                    exit_fill, reason = c, "crash_guard"
                elif crash_mode == "block" and own_crash:
                    symbol_block_until[symbol] = max(symbol_block_until.get(symbol, 0), int(t + crash_block_ms))

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

            # ---- hooks: close-based ATR trail and give-back rule (long only, spot) ----
            # Like the built-in trail the new stop applies from the next bar on.
            if exit_fill is None and long and protect_on:
                pc = max(peak_close.get(symbol, pos.entry), c)
                peak_close[symbol] = pc
                new_stop = pos.stop
                if trail_k is not None and atr > 0:
                    risk = abs(pos.entry - pos.initial_stop)
                    armed = trail_r <= 0 or (risk > 0 and (pc - pos.entry) / risk >= trail_r)
                    if armed:
                        new_stop = max(new_stop, pc - trail_k * atr)
                if gb_pct is not None:
                    peak_gain = (pc - pos.entry) / pos.entry
                    if peak_gain * 100 >= gb_pct:
                        new_stop = max(new_stop, pos.entry * (1 + gb_keep * peak_gain))
                if new_stop > pos.stop:
                    pos.stop = new_stop
                    protected.add(symbol)

            # ---- hook: give-back on the scale-out remainder (peak close since entry) ----
            if exit_fill is None and long and scale_out:
                pc = max(peak_close.get(symbol, pos.entry), c)
                peak_close[symbol] = pc
                if rem_gb is not None and pos.legs_done >= len(scale_out):
                    peak_gain = (pc - pos.entry) / pos.entry * 100
                    if peak_gain > rem_gb:
                        new_stop = pos.entry * (1 + (peak_gain - rem_gb) / 100)
                        if new_stop > pos.stop:
                            pos.stop = new_stop
                            protected.add(symbol)

            # ---- hook: scale-in adds at the close (never on an exit bar, never after a leg) ----
            if (exit_fill is None and long and scale_in is not None and atr > 0
                    and pos.adds_done < tranches - 1 and pos.legs_done == 0 and c > pos.stop):
                if scale_in == "atr":
                    trigger = c >= pos.last_fill + step_atr * pos.last_fill_atr
                else:
                    trigger = (ema20 > 0 and l <= ema20 <= c) or c <= pos.last_fill - pullback_atr * pos.last_fill_atr
                if trigger:
                    add_tranche(pos, c, atr, mark_equity())

            if exit_fill is not None:
                if reason == "stop_loss" and symbol in protected:
                    reason = "protect_stop"
                fill = exit_fill * (1 - slip) if long else exit_fill * (1 + slip)
                close_position(pos, fill, t, reason, gap)

        for symbol, table in lookup.items():
            bar = table.get(t)
            if bar is not None:
                last_close[symbol] = bar[3]

        equity = mark_equity()
        peak = max(peak, equity)
        curve.append((t, equity))
        invested.append(equity - cash)

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
        # crash guard: no entries on a BTC crash bar (exit mode) or inside the block window
        if btc_crash or (crash_mode == "block" and t < market_block_until):
            continue

        # ---- entries: best candidates first ----
        candidates: list[tuple[float, str, tuple]] = []
        for symbol, table in lookup.items():
            if symbol in positions:
                continue
            bar = table.get(t)
            if bar is None:
                continue
            o, h, l, c, side, score, atr, ratio, regime, adx, htf_bias, _sd, _td, _ex, _ema = bar
            if side == "flat" or score < params.min_score or atr <= 0:
                continue
            if side == "short" and not params.allow_short:
                continue
            if params.min_htf_bias is not None:
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
            if crash_on:
                if symbol_crash(o, c, atr):
                    if crash_mode == "block":
                        symbol_block_until[symbol] = max(symbol_block_until.get(symbol, 0), int(t + crash_block_ms))
                    continue
                if crash_mode == "block" and symbol_block_until.get(symbol, 0) > t:
                    continue
            candidates.append((score, symbol, bar))

        candidates.sort(key=lambda item: item[0], reverse=True)
        free = cash
        for score, symbol, bar in candidates:
            if len(positions) >= params.max_positions:
                break
            o, h, l, c, side, _score, atr, ratio, regime, adx, _htf, stop_d, target_d, _ex, _ema = bar
            price = c
            stop_distance = atr * params.atr_stop
            target_distance = atr * params.atr_target
            if frame_stops:
                if stop_d > 0:
                    stop_distance = stop_d
                if target_d > 0:
                    target_distance = target_d
            if target_pct is not None:
                target_distance = price * target_pct / 100
            expected_move_pct = target_distance / price * 100
            min_move = max(params.min_move_pct, fee_round_trip_pct * params.fee_hurdle_mult)
            if expected_move_pct < min_move:
                continue
            if target_distance / stop_distance < params.min_rr:
                continue

            risk_amount = equity * (params.risk_pct / 100) * params.size_multiplier
            tranche_risk = risk_amount
            if scale_in is not None and split_risk:
                # the first tranche risks 1/N of the budget; the adds bring the total to the base
                risk_amount = tranche_risk = risk_amount / tranches
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
            cash -= fill * qty + entry_fee
            free = cash
            positions[symbol] = _AltPosition(
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
                initial_qty=qty,
                last_fill=fill,
                last_fill_atr=atr,
                tranche_risk=tranche_risk,
            )

    final_t = times[-1]
    for symbol in list(positions.keys()):
        pos = positions[symbol]
        price = last_close.get(pos.symbol, pos.entry)
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
    result.invested_curve = invested   # aligned with equity_curve; read by alt.scaling
    return result
