"""The exchange contract every connector implements.

The engine was written against Binance spot, and its executor, reconciler and
risk manager read Binance-shaped answers (`executedQty`, `fills`, `orderId`,
`PRICE_FILTER` ...). Instead of teaching every caller a new vocabulary, each
connector translates its exchange into that one shape, documented below. A
connector that cannot do something says so through its capability flags and
the engine takes the safe path (an engine-managed stop, REST polling instead
of websockets, no Simple Earn).

Normalised shapes
-----------------
exchange_info()   {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC",
                    "quoteAsset": "USDT", "status": "TRADING", "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.000001",
                     "minQty": "0.000001", "maxQty": "1e9"},
                    {"filterType": "NOTIONAL", "minNotional": "5"}]}]}
klines()          [[open_ms, "o", "h", "l", "c", "volume", close_ms,
                    "quote_volume", trades, "taker_buy_base"], ...] oldest
                    first; the last row may still be forming.
ticker_price(s)   {"symbol": s, "price": "123.4"} (a list when s is None)
order_book(s)     {"bids": [["price", "qty"], ...], "asks": [...]}
spot_account()    {"balances": [{"asset": "USDT", "free": "1", "locked": "0"}]}
an order          {"symbol", "orderId", "clientOrderId", "side": "BUY"|"SELL",
                   "type": "MARKET"|"LIMIT"|"STOP_LOSS_LIMIT", "status":
                   NEW|PARTIALLY_FILLED|FILLED|CANCELED|REJECTED|EXPIRED,
                   "price", "stopPrice", "origQty", "executedQty",
                   "cummulativeQuoteQty", "time", "updateTime",
                   "fills": [{"price", "qty", "commission", "commissionAsset"}]}

Symbols are always canonical inside the engine: BASE + QUOTE in upper case
(`BTCUSDT`, `ETHIRT`). `IRT` is the Toman; a connector whose exchange speaks
Rial converts at its boundary so the engine never sees Rial.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Literal

log = logging.getLogger(__name__)

Market = Literal["spot", "futures"]

# Order states the engine understands (Binance's names).
OPEN_STATES = ("NEW", "PARTIALLY_FILLED")
FINAL_STATES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH")

# Client order ids the engine sends start with these, so the reconciler can
# tell its own orders from the ones the user placed by hand.
ORDER_PREFIX = "gtr"
STOP_PREFIX = "gtrs"
EXIT_PREFIX = "gtrx"


def notify_user(category: str, level: str, title: str, body: str) -> None:
    """User notification, fire-and-forget. Imported lazily so connectors stay
    usable from scripts that never load the notifier's dependencies."""
    try:
        from app.notify import service

        service.emit(category, level, title, body)
    except Exception as exc:  # noqa: BLE001 - never let a notification break a request
        log.debug("notification skipped: %s", exc)


class ExchangeError(RuntimeError):
    """Any failed exchange call.

    `status` is the HTTP status (0 = no answer at all), `code` the exchange's
    own error code when it sends one, `kind` a portable classification the
    engine can act on without knowing the exchange: "unknown_order",
    "auth", "insufficient_balance", "invalid_symbol", "unsupported",
    "rate_limit" or None.
    """

    def __init__(
        self,
        status: int,
        code: int | str | None,
        message: str,
        *,
        exchange: str = "exchange",
        kind: str | None = None,
    ):
        super().__init__(f"{exchange} {status}/{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.exchange = exchange
        self.kind = kind

    @property
    def is_retryable(self) -> bool:
        # 418/429 rate limit, 5xx server side
        return self.status in (418, 429) or self.status >= 500

    @property
    def is_unknown_order(self) -> bool:
        """The exchange has no such order: it filled, was cancelled or never existed."""
        return self.kind == "unknown_order"

    @property
    def is_unsupported(self) -> bool:
        return self.kind == "unsupported"


class ExchangeClient(ABC):
    """A spot exchange, seen through the normalised shapes above."""

    #: registry key, e.g. "binance", "nobitex"
    name: str = "exchange"
    #: shown to the user
    title: str = "Exchange"
    title_fa: str = "صرافی"

    # ---- capabilities ----
    #: resting stop orders on the exchange; without them the engine's tick
    #: watchdog enforces every stop itself and must keep running
    supports_stop_orders: bool = False
    #: a websocket kline stream; without it market data is polled over REST
    supports_kline_stream: bool = False
    #: a private websocket for fills; without it the REST sweep reconciles
    supports_user_stream: bool = False
    supports_earn: bool = False
    supports_futures: bool = False
    supports_testnet: bool = False
    #: Binance's BNB fee discount
    supports_fee_token: bool = False
    #: the exchange can say what an API key is allowed to do
    supports_key_permissions: bool = False

    api_key: str = ""
    testnet: bool = False
    time_offset_ms: int = 0

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)

    # ---- lifecycle ----
    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    async def __aenter__(self) -> "ExchangeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def sync_time(self, market: Market = "spot") -> int:
        """Server clock offset in ms. Exchanges without signed timestamps return 0."""
        return self.time_offset_ms

    def ws_url(self, market: Market = "spot") -> str:
        raise ExchangeError(0, None, "no websocket stream", exchange=self.name, kind="unsupported")

    # ---- public market data ----
    @abstractmethod
    async def exchange_info(self, market: Market = "spot") -> dict: ...

    @abstractmethod
    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
        market: Market = "spot",
    ) -> list[list]: ...

    @abstractmethod
    async def ticker_price(self, symbol: str | None = None, market: Market = "spot") -> Any: ...

    async def order_book(self, symbol: str, limit: int = 20, market: Market = "spot") -> dict:
        raise self._unsupported("order book")

    # ---- account ----
    @abstractmethod
    async def spot_account(self) -> dict: ...

    async def spot_balances(self) -> dict[str, float]:
        account = await self.spot_account()
        return {
            b["asset"]: float(b["free"]) + float(b["locked"])
            for b in account.get("balances", [])
            if float(b["free"]) + float(b["locked"]) > 0
        }

    async def free_balance(self, asset: str) -> float:
        account = await self.spot_account()
        for b in account.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    async def api_restrictions(self) -> dict:
        raise self._unsupported("key permissions")

    # ---- orders ----
    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        market: Market = "spot",
        quantity: float | None = None,
        quote_quantity: float | None = None,
        price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str | None = None,
        reduce_only: bool | None = None,
        client_order_id: str | None = None,
    ) -> dict: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str | int, market: Market = "spot") -> dict: ...

    @abstractmethod
    async def open_orders(self, symbol: str | None = None, market: Market = "spot") -> list: ...

    @abstractmethod
    async def query_order(
        self,
        symbol: str,
        order_id: str | int | None = None,
        market: Market = "spot",
        client_order_id: str | None = None,
    ) -> dict: ...

    async def my_trades(self, symbol: str, order_id: str | int, market: Market = "spot") -> list[dict]:
        """The fills behind one order. Connectors whose order answer already
        carries the fee return the order's own `fills`."""
        order = await self.query_order(symbol, order_id, market)
        return list(order.get("fills") or [])

    # ---- Binance-only features, absent elsewhere ----
    async def simple_earn_flexible_position(self, asset: str = "USDT") -> list[dict]:
        return []

    async def simple_earn_flexible_all(self) -> dict[str, dict[str, Any]]:
        return {}

    async def simple_earn_redeem(self, product_id: str, amount: float, *, fast: bool = True) -> dict:
        raise self._unsupported("Simple Earn")

    async def set_bnb_fee_discount(self, enabled: bool = True) -> dict:
        raise self._unsupported("fee token discount")

    async def futures_account(self) -> dict:
        raise self._unsupported("futures")

    async def futures_positions(self) -> list[dict]:
        return []

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        raise self._unsupported("futures")

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        raise self._unsupported("futures")

    # ---- helpers ----
    def _unsupported(self, what: str) -> ExchangeError:
        return ExchangeError(0, None, f"{what} is not available on {self.title}",
                             exchange=self.name, kind="unsupported")
