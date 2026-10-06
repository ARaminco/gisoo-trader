"""Which exchanges exist and how to build the selected one.

`make_client()` is the only place the engine decides which connector trades;
everything downstream talks to the `ExchangeClient` contract in base.py.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass

from app.config import settings
from app.exchange.base import ExchangeClient


@dataclass(frozen=True)
class ExchangeSpec:
    name: str
    title: str
    title_fa: str
    module: str
    cls: str
    website: str
    api_docs: str
    #: quote assets the connector maps (canonical names)
    quotes: tuple[str, ...]
    #: "reference" = the engine was built and run on it;
    #: "beta" = written against the published API docs, not yet run with real money
    status: str


EXCHANGES: dict[str, ExchangeSpec] = {
    spec.name: spec
    for spec in (
        ExchangeSpec(
            "binance", "Binance", "بایننس", "app.exchange.binance_client", "BinanceClient",
            "https://www.binance.com", "https://developers.binance.com/docs/binance-spot-api-docs",
            ("USDT", "USDC", "FDUSD", "BTC"), "reference",
        ),
        ExchangeSpec(
            "nobitex", "Nobitex", "نوبیتکس", "app.exchange.iran.nobitex", "NobitexClient",
            "https://nobitex.ir", "https://apidocs.nobitex.ir",
            ("USDT", "IRT"), "beta",
        ),
        ExchangeSpec(
            "wallex", "Wallex", "والکس", "app.exchange.iran.wallex", "WallexClient",
            "https://wallex.ir", "https://developers.wallex.ir/docs/",
            ("USDT", "IRT"), "beta",
        ),
        ExchangeSpec(
            "bitpin", "Bitpin", "بیت‌پین", "app.exchange.iran.bitpin", "BitpinClient",
            "https://bitpin.ir", "https://docs.bitpin.ir/v1/",
            ("USDT", "IRT"), "beta",
        ),
        ExchangeSpec(
            "ramzinex", "Ramzinex", "رمزینکس", "app.exchange.iran.ramzinex", "RamzinexClient",
            "https://ramzinex.ir", "https://docs.ramzinex.ir",
            ("USDT", "IRT"), "beta",
        ),
        ExchangeSpec(
            "okex", "OK-EX", "اوکی اکسچنج", "app.exchange.iran.okex", "OkexClient",
            "https://ok-ex.io", "https://docs.ok-ex.io/docs/api/getting-started",
            ("USDT", "IRT"), "beta",
        ),
    )
}


def spec(name: str | None = None) -> ExchangeSpec:
    key = (name or settings.exchange).lower()
    try:
        return EXCHANGES[key]
    except KeyError:
        raise ValueError(
            f"unknown EXCHANGE {key!r}; choose one of {', '.join(EXCHANGES)}"
        ) from None


def client_class(name: str | None = None) -> type[ExchangeClient]:
    s = spec(name)
    return getattr(importlib.import_module(s.module), s.cls)


def make_client(name: str | None = None, **kwargs) -> ExchangeClient:
    """A connector for EXCHANGE (or `name`) with the configured credentials."""
    cls = client_class(name)
    if settings.trading_mode == "testnet" and not cls.supports_testnet:
        # Never let "testnet" place real orders on an exchange without one.
        raise RuntimeError(f"TRADING_MODE=testnet is not available on {cls.title}; use paper")
    return cls(**kwargs)


def describe(name: str | None = None) -> dict:
    """What the panel shows about the selected exchange."""
    s = spec(name)
    cls = client_class(s.name)
    return {
        "name": s.name,
        "title": s.title,
        "title_fa": s.title_fa,
        "status": s.status,
        "website": s.website,
        "api_docs": s.api_docs,
        "quotes": list(s.quotes),
        "stop_orders": cls.supports_stop_orders,
        "kline_stream": cls.supports_kline_stream,
        "user_stream": cls.supports_user_stream,
    }
