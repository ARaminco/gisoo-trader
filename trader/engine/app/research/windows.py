"""Historical test windows and the dated events inside them.

Each window is a distinct market regime. The system must be judged on all of
them together - a strategy that only works in one regime is a coin flip on
which regime comes next.

Dates are UTC. Events are the major, verifiable market-moving moments; the
event analysis measures what the system did in the days around each one.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    date: str  # YYYY-MM-DD (UTC)
    title: str
    kind: str  # crash / rally / news / macro / hack / regulatory
    note: str = ""


@dataclass(frozen=True)
class Window:
    id: str
    label: str
    start: str  # inclusive, YYYY-MM-DD
    end: str    # exclusive
    regime: str  # bull / bear / crash / chop / mixed
    description: str
    events: tuple[Event, ...] = field(default_factory=tuple)

    @property
    def start_ms(self) -> int:
        return int(dt.datetime.fromisoformat(self.start).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(dt.datetime.fromisoformat(self.end).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    @property
    def days(self) -> int:
        return int((self.end_ms - self.start_ms) / 86_400_000)


# Extra history fetched before each window so indicators (EMA200 on 4h needs
# ~35 days) are fully warmed up at the window start.
WARMUP_DAYS = 45

WINDOWS: list[Window] = [
    Window(
        "w01_bull_2021", "Bull run Q1 2021", "2021-01-01", "2021-03-15", "bull",
        "Parabolic uptrend with violent 20-25% shakeouts every couple of weeks.",
        (
            Event("2021-01-04", "BTC flash crash -20% intraday ($34k -> $28k)", "crash"),
            Event("2021-01-08", "BTC new ATH $41.9k", "rally"),
            Event("2021-01-11", "Two-day -25% dump", "crash"),
            Event("2021-02-08", "Tesla discloses $1.5B BTC purchase, +20%", "news"),
            Event("2021-02-21", "BTC ATH $58k", "rally"),
            Event("2021-02-22", "-25% two-day correction, yields spike", "crash"),
            Event("2021-03-13", "BTC ATH $61k", "rally"),
        ),
    ),
    Window(
        "w02_crash_may_2021", "May 2021 crash", "2021-05-01", "2021-07-22", "crash",
        "Top, then -50% in weeks. China mining ban, Musk tweets, record liquidations.",
        (
            Event("2021-05-08", "DOGE peaks around SNL appearance", "rally"),
            Event("2021-05-12", "Tesla suspends BTC payments, -15%", "news"),
            Event("2021-05-19", "China ban headlines: BTC $43k -> $30k intraday, record liquidations", "crash"),
            Event("2021-05-21", "China State Council crackdown on mining", "regulatory"),
            Event("2021-06-08", "El Salvador makes BTC legal tender", "news"),
            Event("2021-06-21", "Sichuan mining shutdown, BTC under $32k", "crash"),
            Event("2021-07-20", "Cycle low $29.3k", "crash"),
        ),
    ),
    Window(
        "w03_bull_q4_2021", "Bull run Q4 2021", "2021-09-15", "2021-12-06", "bull",
        "Grind to the $69k ATH, then a -20% flash crash on Dec 4.",
        (
            Event("2021-09-20", "Evergrande contagion fear, -10%", "macro"),
            Event("2021-09-24", "China declares all crypto transactions illegal", "regulatory"),
            Event("2021-10-19", "First US BTC futures ETF (BITO) launches", "news"),
            Event("2021-10-20", "BTC ATH $67k", "rally"),
            Event("2021-11-10", "BTC ATH $69k, CPI print", "rally"),
            Event("2021-12-04", "Flash crash -20% ($57k -> $42k) on thin weekend liquidity", "crash"),
        ),
    ),
    Window(
        "w04_luna_2022", "LUNA / Celsius collapse 2022", "2022-04-15", "2022-06-30", "bear",
        "Bear market with two cascading failures: UST depeg in May, Celsius/3AC in June.",
        (
            Event("2022-05-04", "Fed +50bp; relief rally reverses next day", "macro"),
            Event("2022-05-09", "UST depeg begins", "crash"),
            Event("2022-05-11", "LUNA collapse, BTC $34k -> $26k", "crash"),
            Event("2022-06-12", "Celsius freezes withdrawals", "crash"),
            Event("2022-06-13", "BTC -15% in a day, ETH -20%", "crash"),
            Event("2022-06-15", "Fed +75bp", "macro"),
            Event("2022-06-18", "BTC under $18k, 3AC insolvency", "crash"),
        ),
    ),
    Window(
        "w05_ftx_2022", "FTX collapse and dead December", "2022-10-15", "2022-12-31", "crash",
        "Low-volatility bear, one violent gap down, then the quietest month in years.",
        (
            Event("2022-11-02", "CoinDesk publishes Alameda balance sheet", "news"),
            Event("2022-11-06", "Binance announces FTT liquidation", "news"),
            Event("2022-11-08", "FTX halts withdrawals, BTC $21k -> $16k", "crash"),
            Event("2022-11-11", "FTX files for bankruptcy", "crash"),
            Event("2022-11-21", "BTC cycle low $15.5k", "crash"),
            Event("2022-12-13", "SBF arrested; CPI", "news"),
        ),
    ),
    Window(
        "w06_chop_2023", "Summer 2023 chop", "2023-05-15", "2023-08-31", "chop",
        "Months of range-bound, low-volume trading - the hardest regime for breakout logic.",
        (
            Event("2023-06-05", "SEC sues Binance", "regulatory"),
            Event("2023-06-06", "SEC sues Coinbase, alt-coins -20%", "regulatory"),
            Event("2023-06-15", "BlackRock files spot ETF, rally starts", "news"),
            Event("2023-07-13", "Ripple partial win vs SEC, XRP +70%", "regulatory"),
            Event("2023-08-17", "Flash crash -10% ($29k -> $25k)", "crash"),
        ),
    ),
    Window(
        "w07_etf_bull_2024", "ETF approval bull 2024", "2023-12-01", "2024-03-20", "bull",
        "Run-up into the spot ETF, sell-the-news dip, then straight to a new ATH.",
        (
            Event("2024-01-03", "Matrixport rejection note, flash -10%", "news"),
            Event("2024-01-09", "Fake SEC approval tweet whipsaw", "news"),
            Event("2024-01-10", "SEC approves spot BTC ETFs", "regulatory"),
            Event("2024-01-11", "ETFs trade; sell-the-news begins ($49k -> $38.5k by Jan 23)", "crash"),
            Event("2024-02-28", "BTC crosses $60k, ETF inflows peak", "rally"),
            Event("2024-03-05", "New ATH $69k then -10% intraday", "rally"),
            Event("2024-03-14", "ATH $73.7k", "rally"),
        ),
    ),
    Window(
        "w08_aug_2024_crash", "August 2024 carry-trade crash", "2024-07-15", "2024-09-15", "mixed",
        "Range, then a global risk-off gap down on Aug 5, then range again.",
        (
            Event("2024-07-29", "Nashville conference, BTC $70k", "rally"),
            Event("2024-08-02", "Weak US jobs, risk-off starts", "macro"),
            Event("2024-08-05", "Yen carry unwind: BTC $49k, ETH -20% in hours", "crash"),
            Event("2024-08-23", "Powell at Jackson Hole signals cuts", "macro"),
            Event("2024-09-06", "Jobs report, BTC $52k", "macro"),
        ),
    ),
    Window(
        "w09_election_2024", "US election bull 2024", "2024-10-20", "2025-01-20", "bull",
        "Clean trend from $67k to $108k with a hawkish-Fed shakeout in December.",
        (
            Event("2024-11-05", "US election day", "macro"),
            Event("2024-11-06", "BTC ATH $75k on result", "rally"),
            Event("2024-11-12", "BTC $90k", "rally"),
            Event("2024-12-05", "BTC crosses $100k", "rally"),
            Event("2024-12-17", "ATH $108k", "rally"),
            Event("2024-12-18", "Hawkish Fed, -6%", "macro"),
            Event("2025-01-20", "Inauguration, ATH $109k", "macro"),
        ),
    ),
    Window(
        "w10_tariff_2025", "Tariff shock 2025", "2025-02-01", "2025-04-25", "bear",
        "Macro-driven bear: tariff headlines, largest exchange hack ever, Liberation Day crash.",
        (
            Event("2025-02-02", "Tariffs on CA/MX/CN announced, ETH -25% next day", "macro"),
            Event("2025-02-21", "Bybit $1.5B hack", "hack"),
            Event("2025-02-28", "BTC $78k, altcoins capitulate", "crash"),
            Event("2025-03-02", "Strategic reserve post, +10% then full reversal", "news"),
            Event("2025-04-02", "Liberation Day tariffs", "macro"),
            Event("2025-04-07", "Global crash, BTC $74.5k", "crash"),
            Event("2025-04-09", "90-day tariff pause, +8%", "macro"),
            Event("2025-04-22", "BTC $93k", "rally"),
        ),
    ),
    Window(
        "w11_oct_2025_liquidation", "October 2025 liquidation cascade", "2025-09-15", "2025-11-15", "crash",
        "ATH then the largest liquidation event in crypto history on Oct 10.",
        (
            Event("2025-10-06", "BTC ATH near $126k", "rally"),
            Event("2025-10-10", "100% China tariff threat: ~$19B liquidated, alts -50% to -80% intraday", "crash"),
            Event("2025-10-11", "Partial rebound, exchange outages", "crash"),
            Event("2025-10-17", "BTC below $105k", "crash"),
            Event("2025-11-04", "BTC loses $100k", "crash"),
        ),
    ),
    Window(
        "w12_recent_2026", "Most recent quarter", "2026-06-01", "2026-09-01", "mixed",
        "The three months immediately before deployment. No curated events.",
        (),
    ),
]

WINDOW_BY_ID = {w.id: w for w in WINDOWS}

# Universe with continuous Binance USDT spot history back to 2021.
SYMBOLS: list[str] = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "DOTUSDT", "AVAXUSDT", "LTCUSDT", "ATOMUSDT",
]

TIMEFRAMES: list[str] = ["15m", "30m", "1h", "4h"]
