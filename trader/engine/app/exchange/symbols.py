"""Exchange filters: tick size, step size, min notional.

Getting these wrong is the single most common cause of rejected orders, so the
rounding helpers here are used by every code path that sizes an order.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from app.exchange.base import ExchangeClient, Market

log = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    tick_size: float = 0.01
    step_size: float = 0.0001
    min_qty: float = 0.0
    max_qty: float = 1e18
    min_notional: float = 5.0
    price_precision: int = 8
    qty_precision: int = 8
    market: Market = "spot"

    def round_price(self, price: float) -> float:
        return _to_step(price, self.tick_size, self.price_precision, ROUND_DOWN)

    def round_qty(self, qty: float) -> float:
        return _to_step(qty, self.step_size, self.qty_precision, ROUND_DOWN)

    def ceil_qty(self, qty: float) -> float:
        """Smallest on-grid quantity not below `qty` - for sizing up to a minimum."""
        return _to_step(qty, self.step_size, self.qty_precision, ROUND_UP)

    def is_tradable(self, qty: float, price: float) -> tuple[bool, str]:
        if qty <= 0:
            return False, "quantity rounds to zero"
        if qty < self.min_qty:
            return False, f"quantity {qty} below min {self.min_qty}"
        if qty > self.max_qty:
            return False, f"quantity {qty} above max {self.max_qty}"
        notional = qty * price
        if notional < self.min_notional:
            return False, f"notional {notional:.2f} below min {self.min_notional}"
        return True, ""


def _to_step(value: float, step: float, precision: int, rounding: str) -> float:
    """Snap `value` to the exchange grid without binary-float artefacts.

    `math.floor(0.7 / 0.1)` is 6, not 7: float division lands one ulp short on
    roughly a third of on-grid quantities, and the lost step became dust on
    every exit. Dividing the decimal strings is exact. The 10-decimal pre-round
    removes arithmetic noise (2.3 - 1.0 == 1.2999999999999998) while keeping
    every real sub-step amount - exchange grids never go past 8 decimals.
    """
    if not math.isfinite(value):
        raise ValueError(f"cannot round non-finite value {value!r}")
    if step <= 0:
        return round(value, precision)
    d_step = Decimal(str(step))
    steps = (Decimal(str(round(value, 10))) / d_step).to_integral_value(rounding=rounding)
    return round(float(steps * d_step), precision)


def _decimals(step: float) -> int:
    if step <= 0:
        return 8
    text = f"{step:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


class SymbolRegistry:
    """Caches exchangeInfo. Refreshed at most once an hour."""

    def __init__(self, client: ExchangeClient, ttl_seconds: int = 3600):
        self.client = client
        self.ttl = ttl_seconds
        self._cache: dict[Market, dict[str, SymbolInfo]] = {}
        self._loaded_at: dict[Market, float] = {}

    async def refresh(self, market: Market = "spot") -> None:
        info = await self.client.exchange_info(market)
        table: dict[str, SymbolInfo] = {}
        for entry in info.get("symbols", []):
            si = SymbolInfo(
                symbol=entry["symbol"],
                base_asset=entry.get("baseAsset", ""),
                quote_asset=entry.get("quoteAsset", ""),
                status=entry.get("status", "UNKNOWN"),
                market=market,
            )
            lot_size_seen = False
            for f in entry.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "PRICE_FILTER":
                    si.tick_size = float(f["tickSize"])
                    si.price_precision = _decimals(si.tick_size)
                elif ftype in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                    step = float(f["stepSize"])
                    # LOT_SIZE is the grid every order (market ones included)
                    # must sit on. MARKET_LOT_SIZE on Binance spot carries
                    # stepSize 0 / minQty 0 and only caps market-order size,
                    # so it is a fallback for symbols with no LOT_SIZE at all.
                    # The old "override when step_size is still the default
                    # 0.0001" test wiped ETHUSDT's real 0.0001 step with the
                    # MARKET_LOT_SIZE zeros and every ETH order failed LOT_SIZE.
                    if ftype == "LOT_SIZE":
                        lot_size_seen = True
                    elif lot_size_seen or step <= 0:
                        continue
                    si.step_size = step
                    si.qty_precision = _decimals(step)
                    si.min_qty = float(f["minQty"])
                    si.max_qty = float(f["maxQty"])
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    value = f.get("minNotional") or f.get("notional")
                    if value is not None:
                        si.min_notional = float(value)
            table[si.symbol] = si
        self._cache[market] = table
        self._loaded_at[market] = time.time()
        log.info("loaded %s %s symbols", len(table), market)

    async def get(self, symbol: str, market: Market = "spot") -> SymbolInfo:
        stale = time.time() - self._loaded_at.get(market, 0) > self.ttl
        if market not in self._cache or stale:
            await self.refresh(market)
        info = self._cache[market].get(symbol.upper())
        if info is None:
            raise KeyError(f"{symbol} is not listed on {self.client.name} {market}")
        return info

    async def validate_universe(
        self, symbols: list[str], market: Market = "spot"
    ) -> tuple[list[str], list[str]]:
        """Split a configured universe into tradable and rejected symbols."""
        ok: list[str] = []
        bad: list[str] = []
        for symbol in symbols:
            try:
                info = await self.get(symbol, market)
                if info.status == "TRADING":
                    ok.append(symbol)
                else:
                    bad.append(f"{symbol} ({info.status})")
            except KeyError:
                bad.append(f"{symbol} (not listed)")
        return ok, bad
