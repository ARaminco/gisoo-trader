"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, List, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

TradingMode = Literal["paper", "testnet", "live"]
# portfolio4h: the two BTC-regime-gated 4h families validated on 12 regime
# windows (app/strategies/core4h.py). ensemble30m: the previous six-strategy
# ensemble on TIMEFRAME/HTF_TIMEFRAME, kept intact for comparison.
StrategyCore = Literal["portfolio4h", "ensemble30m"]

CORE4H_TIMEFRAME = "4h"

# Exchange connectors (app/exchange/registry.py). Binance is the reference;
# the Iranian exchanges are spot-only and quote in USDT or Toman (IRT).
ExchangeName = Literal["binance", "nobitex", "wallex", "bitpin", "ramzinex", "okex"]

# LLM advisory layer (app/llm). The advisor is a risk overseer, so any of
# these back ends will do; pick one by key and by LLM_PROVIDER. "gisoo" and
# "netarz" are OpenAI-compatible gateways (one key, hundreds of models, paid
# in Toman): https://gisoo.pro/api/v1 and https://netarz.ir/api/ai/v1.
LLMProviderName = Literal["gisoo", "netarz", "anthropic", "openai", "deepseek"]
LLMProviderChoice = Literal["auto", "gisoo", "netarz", "anthropic", "openai", "deepseek"]
LLMFallbackChoice = Literal["none", "gisoo", "netarz", "anthropic", "openai", "deepseek"]
# LLM_PROVIDER=auto takes the first of these with a key.
LLM_AUTO_ORDER: tuple[LLMProviderName, ...] = ("gisoo", "netarz", "anthropic", "openai", "deepseek")
# Gateways that accept any vendor's model name.
LLM_GATEWAYS: tuple[str, ...] = ("gisoo", "netarz")
# Used when LLM_MODEL / LLM_FALLBACK_MODEL is empty. OpenAI has no fixed
# default: the provider asks /v1/models and takes the best one the key can use.
LLM_DEFAULT_MODELS: dict[str, str] = {
    "gisoo": "gpt-4o-mini",
    "netarz": "gpt-4o-mini",
    "anthropic": "claude-opus-5",
    "openai": "",
    "deepseek": "deepseek-chat",
}


def llm_model_family(model: str) -> str | None:
    """Which provider a model name belongs to, or None when it is not obvious."""
    name = model.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("deepseek"):
        return "deepseek"
    if name.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return None

# Origins a passkey response may come from when WEBAUTHN_ORIGINS is not set:
# PANEL_ORIGINS plus the local panel (Caddy on :8090) and the Vite dev server.
LOCAL_WEBAUTHN_ORIGINS = ("http://localhost:8090", "http://localhost:5173")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- runtime ---
    tz: str = "Asia/Tehran"
    trading_mode: TradingMode = "paper"
    trading_armed: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://trader:trader@postgres:5432/trader"
    redis_url: str = "redis://redis:6379/0"
    state_dir: str = "/data"

    # --- exchange ---
    # Which connector trades: binance (reference, full feature set) or one of
    # the Iranian spot exchanges. See app/exchange/registry.py.
    exchange: ExchangeName = "binance"
    # Credentials of the selected exchange. Create a trade-only key with NO
    # withdrawal permission and, where the exchange offers it, lock it to the
    # server's IP. Some exchanges issue a single token (leave the secret empty).
    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    # Binance-specific names, still honoured (they win over EXCHANGE_API_*).
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_use_bnb_fees: bool = True
    exchange_http_proxy: str = ""
    # Exchanges without a websocket kline stream are polled this often (s).
    exchange_poll_seconds: int = 30
    # Override a connector's REST base URL (mirrors, sandboxes). Empty = default.
    exchange_base_url: str = ""

    # --- universe ---
    # USDT everywhere; IRT (Toman) on the Iranian exchanges as well.
    quote_asset: str = "USDT"
    # NoDecode: SYMBOLS is a plain comma-separated string, not JSON. Without it
    # pydantic-settings tries to json.loads() the value and fails at startup.
    symbols: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )
    # Only the ensemble30m core reads these: the portfolio4h core decides on
    # 4h bars regardless of TIMEFRAME (see decision_timeframe).
    timeframe: str = "5m"
    htf_timeframe: str = "1h"
    candle_history: int = 1500

    # --- strategy core ---
    strategy_core: StrategyCore = "portfolio4h"
    # Scales both 4h families' validated risk per trade (momentum 1.5%,
    # turtle 1.0%). 1.0 = as validated; 0.67 = the conservative control from
    # docs/research-scaling.md (worst window -3.7%, max DD 11% out of sample).
    # Runtime-editable from the panel.
    core4h_risk_scale: float = 1.0

    # --- capital & risk ---
    # Paper-mode starting balance and the first capital base, in QUOTE_ASSET
    # units (the name predates Toman support).
    initial_capital_usdt: float = 100.0
    # The portfolio4h core sizes per family (core4h.MOMENTUM / TURTLE) and
    # ignores risk_per_trade_pct, max_position_pct and the cooldown; the
    # account-level gates below apply to both cores.
    risk_per_trade_pct: float = 1.5
    # 4 fits the 4h core's two families (three momentum names plus a channel
    # breakout); the ensemble ran with 3.
    max_concurrent_positions: int = 4
    max_daily_loss_pct: float = 5.0
    max_drawdown_pct: float = 20.0
    min_expected_move_pct: float = 0.8
    max_position_pct: float = 35.0
    # Your real taker fee on the selected exchange, in percent. Binance with
    # BNB: 0.075; the Iranian exchanges are typically 0.2-0.35.
    taker_fee_pct: float = 0.075

    # --- futures ---
    futures_enabled: bool = False
    futures_leverage: int = 2
    futures_margin_mode: str = "ISOLATED"

    # --- strategy ---
    min_signal_score: float = 0.62
    # Wide bracket: 3.0 / 4.5 ATR survived all 12 regime windows; the old
    # 1.8 / 3.0 was stopped out on noise in the multi-window tests.
    atr_stop_mult: float = 3.0
    atr_target_mult: float = 4.5
    trailing_enabled: bool = True
    trailing_activate_r: float = 1.0
    trailing_atr_mult: float = 1.4
    cooldown_minutes_after_loss: int = 45
    # Volatility gate: only enter while ATR is expanding relative to its
    # 50-bar mean. Entries in quiet volatility lost in every regime window.
    min_atr_ratio: float = 1.5
    # Weekend entries lost in every window; 00-04 UTC is optional.
    skip_weekend_entries: bool = True
    skip_entry_hours_utc: Annotated[List[int], NoDecode] = Field(default_factory=list)
    # Symbols kept for data but never traded (e.g. XRPUSDT lost in 12/12 windows).
    excluded_symbols: Annotated[List[str], NoDecode] = Field(default_factory=list)

    # --- ml ---
    ml_enabled: bool = True
    ml_min_probability: float = 0.55
    ml_retrain_hours: int = 24
    ml_lookahead_bars: int = 12

    # --- llm ---
    llm_enabled: bool = False
    # auto = the first of LLM_AUTO_ORDER with a key. An explicit provider
    # without a key disables the advisor instead of quietly using another.
    llm_provider: LLMProviderChoice = "auto"
    # Tried once when the primary raises, times out or never returns usable JSON.
    llm_fallback_provider: LLMFallbackChoice = "none"
    gisoo_api_key: str = ""
    gisoo_base_url: str = "https://gisoo.pro/api/v1"
    netarz_ai_api_key: str = ""
    netarz_ai_base_url: str = "https://netarz.ir/api/ai/v1"
    anthropic_api_key: str = ""
    # Empty = api.anthropic.com. Gisoo also speaks Anthropic's Messages API at
    # https://gisoo.pro/api (no /v1) with a Gisoo key.
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # Empty = the provider's default (LLM_DEFAULT_MODELS). A name that clearly
    # belongs to another provider is ignored, see llm_model_for().
    llm_model: str = ""
    llm_fallback_model: str = ""
    # Anthropic: output_config.effort. OpenAI reasoning models: reasoning_effort.
    llm_effort: str = "low"
    # Outbound proxy for the LLM APIs. Empty = EXCHANGE_HTTP_PROXY, "direct" = none.
    llm_http_proxy: str = ""
    llm_review_minutes: int = 30
    llm_can_veto: bool = True
    llm_daily_call_budget: int = 120

    # --- investment committee (app/committee) ---
    # Twice a day the scheduler builds a numeric briefing (account, positions,
    # market, performance vs the policy, health checks), has the LLM write the
    # minutes against the investment policy, applies only what the policy
    # allows automatically (stance, risk multiplier for NEW entries) and puts
    # everything else to the user as proposals.
    committee_enabled: bool = False
    # Meeting hours, UTC (4,16 = 07:30 and 19:30 in Asia/Tehran). The meeting
    # starts a few minutes after the hour so the 4h evaluation is already in.
    committee_hours_utc: Annotated[List[int], NoDecode] = Field(
        default_factory=lambda: [4, 16]
    )
    # The policy text the committee is bound by. Empty = the copy shipped
    # inside the package (app/committee/policy.md, kept identical to
    # docs/risk-policy.md).
    committee_policy_path: str = ""

    # --- panel auth ---
    panel_username: str = "admin"
    # Plaintext fallback. Ignored when panel_password_hash is set.
    panel_password: str = "change-me-now"
    # bcrypt hash, generated with `python -m app.api.hash_password`. Preferred.
    panel_password_hash: str = ""
    jwt_secret: str = "change-me"
    jwt_expire_hours: int = 24
    # Browser origins allowed to call the API cross-origin (comma separated).
    # Empty = same-origin only; Caddy serves the panel and the API together.
    panel_origins: Annotated[List[str], NoDecode] = Field(default_factory=list)
    # Peers whose X-Forwarded-For header is believed (IPs, CIDRs or hostnames).
    trusted_proxies: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["caddy", "127.0.0.1", "::1"]
    )
    # Brute-force lockout: after `login_max_failures` the IP and the username
    # are locked for `login_lockout_minutes`, doubling per repeated lockout up
    # to `login_lockout_max_hours`.
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    login_lockout_max_hours: int = 24

    # --- passkeys (WebAuthn / FIDO2: hardware security keys, platform passkeys) ---
    # Relying-party ID the credentials are bound to - a bare host name, no
    # scheme or port. Empty = the host of the first PANEL_ORIGINS entry, else
    # "localhost". A credential remembers the RP ID it was made for, so the
    # same key is enrolled once for the public host and once for localhost
    # and each request only sees the credentials of its own host.
    webauthn_rp_id: str = ""
    # Browser origins a WebAuthn response may carry (comma separated). Empty =
    # PANEL_ORIGINS + LOCAL_WEBAUTHN_ORIGINS. The public origin must be listed
    # in one of the two, or passkeys only work on localhost.
    webauthn_origins: Annotated[List[str], NoDecode] = Field(default_factory=list)
    webauthn_rp_name: str = "Gisoo Trader"
    # Refuse the password form while at least one passkey is enrolled. Also
    # switched from the panel (Security -> "فقط ورود با کلید امنیتی"), which
    # overrides this default through the settings table.
    passkey_required: bool = False

    # --- notifications ---
    telegram_bot_token: str = ""
    # Empty = discovered: the scheduler waits for the user to write to the bot.
    telegram_chat_id: str = ""
    # Contact for the Web Push VAPID subject (push services may use it to
    # reach the operator). An address or a mailto:/https: URL.
    notify_contact: str = "mailto:admin@localhost"
    # Outbound proxy for Telegram and the push services only (empty = direct).
    notify_http_proxy: str = ""

    # --- NetArz FX (Toman exchange rates, https://netarz.ir/fx-api) ---
    # Optional: shows equity in Toman next to USDT. Free plan = delayed rates.
    # Server calls must come from an IP allow-listed on the NetArz app.
    netarz_fx_key: str = ""
    netarz_fx_base_url: str = "https://netarz.ir/api/fx/v1"
    netarz_fx_code: str = "USD"

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, v):
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v

    @field_validator(
        "panel_origins", "webauthn_origins", "trusted_proxies", "excluded_symbols", mode="before"
    )
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("skip_entry_hours_utc", "committee_hours_utc", mode="before")
    @classmethod
    def _split_hours(cls, v):
        if isinstance(v, str):
            return [int(s) for s in v.split(",") if s.strip().isdigit()]
        return v

    @field_validator("committee_hours_utc")
    @classmethod
    def _committee_hours(cls, v: List[int]) -> List[int]:
        hours = sorted({h for h in v if 0 <= h <= 23})
        return hours or [4, 16]

    @field_validator("quote_asset", mode="before")
    @classmethod
    def _quote(cls, v):
        return str(v).strip().upper() if v else "USDT"

    @model_validator(mode="after")
    def _consistent(self) -> "Settings":
        # Fail at startup with a clear message instead of trading wrongly.
        if self.trading_mode == "testnet" and self.exchange != "binance":
            raise ValueError(
                f"TRADING_MODE=testnet exists only on Binance; {self.exchange} has no testnet "
                "(its orders would be real). Use TRADING_MODE=paper to test without money."
            )
        bad = [s for s in self.symbols if not s.upper().endswith(self.quote_asset)]
        if bad:
            raise ValueError(
                f"every symbol in SYMBOLS must end with QUOTE_ASSET={self.quote_asset}; "
                f"these do not: {', '.join(bad)}"
            )
        return self

    @property
    def market_gate_symbol(self) -> str:
        """BTC against the quote asset: the 4h core's market gate."""
        return f"BTC{self.quote_asset}"

    @property
    def active_symbols(self) -> List[str]:
        """Configured universe minus the exclusions - what the engine trades."""
        excluded = {s.upper() for s in self.excluded_symbols}
        return [s for s in self.symbols if s.upper() not in excluded]

    # ---- strategy core ----
    @property
    def core_is_portfolio4h(self) -> bool:
        return self.strategy_core == "portfolio4h"

    @property
    def decision_timeframe(self) -> str:
        """The bar the engine decides on: fixed at 4h for the portfolio core."""
        return CORE4H_TIMEFRAME if self.core_is_portfolio4h else self.timeframe

    @property
    def data_timeframes(self) -> List[str]:
        """Timeframes the market-data worker streams and backfills."""
        if self.core_is_portfolio4h:
            return [CORE4H_TIMEFRAME]
        return sorted({self.timeframe, self.htf_timeframe})

    @property
    def data_symbols(self) -> List[str]:
        """Symbols that need candles.

        The 4h core ranks the whole configured universe (an excluded symbol
        still takes part in the momentum ranking, it is just never bought)
        and always needs BTC for the market gate.
        """
        if not self.core_is_portfolio4h:
            return self.active_symbols
        symbols = [s.upper() for s in self.symbols]
        if self.market_gate_symbol not in symbols:
            symbols.append(self.market_gate_symbol)
        return symbols

    # ---- llm ----
    def llm_api_key(self, provider: str) -> str:
        return {
            "gisoo": self.gisoo_api_key,
            "netarz": self.netarz_ai_api_key,
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "deepseek": self.deepseek_api_key,
        }.get(provider, "")

    @property
    def active_llm_provider(self) -> LLMProviderName | None:
        """Primary advisor back end, or None when the chosen one has no key."""
        if self.llm_provider != "auto":
            return self.llm_provider if self.llm_api_key(self.llm_provider) else None
        for name in LLM_AUTO_ORDER:
            if self.llm_api_key(name):
                return name
        return None

    @property
    def active_llm_fallback_provider(self) -> LLMProviderName | None:
        """Second back end, or None when unset, keyless or the same as the primary."""
        name = self.llm_fallback_provider
        if name == "none" or name == self.active_llm_provider or not self.llm_api_key(name):
            return None
        return name

    def llm_model_for(self, provider: str, configured: str) -> str:
        """`configured` when it plausibly belongs to `provider`, else the default.

        Earlier .env files shipped LLM_MODEL=claude-opus-5; switching providers
        by adding a key must not send that name to DeepSeek or OpenAI.
        """
        name = configured.strip()
        if name and (provider in LLM_GATEWAYS or llm_model_family(name) in (provider, None)):
            return name
        return LLM_DEFAULT_MODELS.get(provider, "")

    @property
    def llm_http_proxy_url(self) -> str | None:
        value = self.llm_http_proxy.strip()
        if value.lower() == "direct":
            return None
        return value or self.exchange_http_proxy or None

    # ---- derived ----
    @property
    def webauthn_allowed_origins(self) -> List[str]:
        """Origins a passkey response may carry, normalised the way browsers
        write them into clientDataJSON (lower-case, no trailing slash)."""
        configured = self.webauthn_origins or [*self.panel_origins, *LOCAL_WEBAUTHN_ORIGINS]
        origins: List[str] = []
        for origin in configured:
            parts = urlsplit(origin.strip())
            if parts.scheme not in ("http", "https") or not parts.hostname:
                continue
            port = f":{parts.port}" if parts.port else ""
            value = f"{parts.scheme}://{parts.hostname.lower()}{port}"
            if value not in origins:
                origins.append(value)
        return origins

    @property
    def webauthn_default_rp_id(self) -> str:
        """WEBAUTHN_RP_ID, else the host of the first PANEL_ORIGINS entry, else localhost."""
        if self.webauthn_rp_id.strip():
            return self.webauthn_rp_id.strip().lower()
        for origin in self.panel_origins:
            host = urlsplit(origin.strip()).hostname
            if host:
                return host.lower()
        return "localhost"

    @property
    def vapid_subject(self) -> str:
        """`sub` claim of every VAPID token: NOTIFY_CONTACT as a mailto:/https: URL."""
        contact = (self.notify_contact or "").strip() or "admin@localhost"
        if contact.startswith(("mailto:", "https://")):
            return contact
        return f"mailto:{contact}"

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"

    @property
    def round_trip_fee_pct(self) -> float:
        """Cost of opening and closing one position, in percent."""
        return self.taker_fee_pct * 2

    def binance_keys(self) -> tuple[str, str]:
        if self.trading_mode == "testnet" and self.binance_testnet_api_key:
            return self.binance_testnet_api_key, self.binance_testnet_api_secret
        if self.binance_api_key:
            return self.binance_api_key, self.binance_api_secret
        return self.exchange_api_key, self.exchange_api_secret

    def exchange_keys(self) -> tuple[str, str]:
        """Credentials of the selected exchange."""
        if self.exchange == "binance":
            return self.binance_keys()
        return self.exchange_api_key, self.exchange_api_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# The 4h core always needs BTC for its market gate, traded or not.
MARKET_GATE_SYMBOL = settings.market_gate_symbol
