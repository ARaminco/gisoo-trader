"""Binance Simple Earn seen as what it is: trading capital held off the spot wallet.

Binance's auto-subscribe sweeps idle balances out of the spot wallet into
flexible Simple Earn and leaves an `LD<asset>` receipt token behind. The spot
account then reads as (almost) empty while the money is still the user's and
still redeemable on demand.

An engine that reads only the spot wallet cannot tell a sweep from the user
taking money out:

  * the sweeps become "withdrawals" in `capital_flows` and shrink the capital
    base to a fraction of the account;
  * the closed-equity drawdown divides a since-inception loss in dollars by
    that base, so a small trading loss reads as a large drawdown and trips
    the kill switch; and
  * free balance reads as 0, so nothing can be sized even before the gate.

This module is the single place that knows about Earn. Two rules matter more
than the rest:

  1. A failed read is *unknown*, never zero. `EarnBook.read` keeps the last
     good holdings and reports `ok=False`; a reading that is not `ok` must not
     be turned into an equity peak or a capital flow. Treating an unreadable
     Earn endpoint as an empty one would re-create the exact bug above.
  2. `LD<asset>` balances in the spot wallet mirror these positions. Whoever
     values the account adds Earn *or* the receipt tokens - never both.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.config import settings
from app.exchange.base import ExchangeClient

log = logging.getLogger(__name__)

# Receipt tokens Binance leaves in the spot wallet for a Simple Earn position.
LD_PREFIX = "LD"

# How long a holdings reading stays fresh. The engine values the account once a
# minute; Earn moves only on a sweep or a redemption, so a short cache keeps the
# extra weight off the rate limit without hiding a redemption from the next
# sizing decision.
CACHE_SECONDS = 30.0

# Redeeming less than this is not worth a request: Binance rejects dust
# redemptions and the amount cannot matter to a position size.
MIN_REDEEM_QUOTE = 0.5


def is_receipt_token(asset: str) -> bool:
    """True for `LDUSDT`, `LDADA`, ... - the spot mirror of an Earn position.

    These are not tradable and have no `<asset>USDT` market, so an account
    valuation that walks the spot wallet must skip them explicitly rather than
    rely on their ticker lookup failing.
    """
    return asset.startswith(LD_PREFIX) and len(asset) > len(LD_PREFIX)


@dataclass
class EarnSnapshot:
    """One reading of the flexible Earn book.

    `ok` is False when the last fetch failed. `holdings` then still carries the
    previous good reading (or is empty if there has never been one) and
    `known` says which of the two it is.
    """

    holdings: dict[str, float] = field(default_factory=dict)
    products: dict[str, str] = field(default_factory=dict)
    ok: bool = True
    known: bool = True
    taken_at: float = 0.0

    @property
    def quote(self) -> float:
        """Redeemable quote asset sitting in Earn."""
        return self.holdings.get(settings.quote_asset, 0.0)

    def amount(self, asset: str) -> float:
        return self.holdings.get(asset, 0.0)

    @property
    def trustworthy(self) -> bool:
        """Safe to build a capital-flow verdict or an equity peak on."""
        return self.ok and self.known


class EarnBook:
    """Cached view of the account's flexible Simple Earn positions."""

    def __init__(self, client: ExchangeClient) -> None:
        self.client = client
        self._holdings: dict[str, float] = {}
        self._products: dict[str, str] = {}
        self._known = False
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def read(self, *, force: bool = False) -> EarnSnapshot:
        """Current holdings, from cache unless it is stale or `force` is set.

        Never raises: a Binance failure returns the previous reading with
        `ok=False` so callers can stand their gates down instead of valuing the
        account as if the money had vanished.
        """
        if settings.trading_mode == "paper" or not self.client.supports_earn:
            return EarnSnapshot(ok=True, known=True, taken_at=time.monotonic())

        async with self._lock:
            fresh = (time.monotonic() - self._fetched_at) < CACHE_SECONDS
            if fresh and not force and self._known:
                return self._snapshot(ok=True)

            try:
                book = await self.client.simple_earn_flexible_all()
            except Exception as exc:  # noqa: BLE001 - an Earn outage must never stop trading
                log.warning(
                    "Simple Earn positions unreadable (%s); using the last known "
                    "holdings and standing capital-flow detection down", exc,
                )
                return self._snapshot(ok=False)

            self._holdings = {a: float(v["amount"]) for a, v in book.items()}
            self._products = {a: str(v["product_id"]) for a, v in book.items()}
            self._known = True
            self._fetched_at = time.monotonic()
            return self._snapshot(ok=True)

    def _snapshot(self, *, ok: bool) -> EarnSnapshot:
        return EarnSnapshot(
            holdings=dict(self._holdings),
            products=dict(self._products),
            ok=ok,
            known=self._known,
            taken_at=self._fetched_at,
        )

    def invalidate(self) -> None:
        """Force the next read to hit the exchange (after a redemption)."""
        self._fetched_at = 0.0

    # ------------------------------------------------------------------
    async def redeem_quote(self, needed: float) -> float:
        """Redeem up to `needed` quote asset from Earn into spot; returns what was redeemed.

        Called when a sized entry needs more free quote than the spot wallet
        holds because auto-subscribe swept it. Redemption is inside the user's
        own account, so it is not an external action and needs no approval; it
        is the reverse of the sweep that put the money there.
        """
        return await self._redeem(settings.quote_asset, needed, MIN_REDEEM_QUOTE)

    async def redeem_asset(self, asset: str, needed: float, min_amount: float = 0.0) -> float:
        """Redeem `needed` of a base asset so a full position can be sold.

        Auto-subscribe nibbles the fractional part of a freshly bought coin into
        Earn, which leaves the spot wallet holding slightly less than the
        position's quantity. Exits clamp to the free balance, so without this
        the remainder is stranded in Earn instead of being sold.
        """
        return await self._redeem(asset, needed, min_amount)

    async def _redeem(self, asset: str, needed: float, floor: float) -> float:
        if needed <= 0:
            return 0.0
        snapshot = await self.read()
        available = snapshot.amount(asset)
        amount = min(needed, available)
        if amount <= 0 or amount < floor:
            return 0.0
        product_id = snapshot.products.get(asset) or f"{asset}001"
        try:
            await self.client.simple_earn_redeem(product_id, amount)
        except Exception as exc:  # noqa: BLE001 - the caller falls back to spot only
            log.warning("Simple Earn redemption of %.8f %s failed: %s", amount, asset, exc)
            return 0.0
        self.invalidate()
        log.info("redeemed %.8f %s from Simple Earn into spot", amount, asset)
        return amount
