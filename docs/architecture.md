# Architecture

Technical overview for developers. The user guide is the Persian [README](../README.md) and the «راهنما» page
inside the panel.

## Services

```
caddy (:8090)  ─┬─ /api, /ws ──► api        FastAPI + WebSocket for the panel
                └─ /*        ──► panel      React PWA (nginx), Persian, RTL

marketdata   klines for the whole universe + BTC/quote (websocket on Binance, REST polling elsewhere)
             ──► postgres.candles ──► redis "candle closed" / live marks
engine       closed 4h bar ──► BTC regime gate ──► momentum ranks + turtle breakouts
             ──► exits (rank / channel / regime) ──► risk (per-family sizing) ──► LLM veto ──► order
             tick watchdog: enforces every stop between bars on the live marks
scheduler    equity snapshots · exchange reconciliation · (Binance) user-data stream · LLM market review ·
             AI committee · public-IP watch · Telegram chat discovery · housekeeping
notify       (in every Python service) audit row ──► category ──► Telegram + Web Push
migrate      one-shot schema migration before any worker starts
backup       pg_dump every BACKUP_INTERVAL_HOURS into ./backups
redis        pub/sub bus between the workers and the API
postgres     everything durable
```

All Python services share one image (`trader/engine`) and differ only in the command they run.

## Exchange layer

`app/exchange/base.py` defines the contract every connector implements: Binance-shaped answers
(`exchange_info`, `klines`, `ticker_price`, `spot_account`, `create_order`, `query_order`, `cancel_order`,
`open_orders`, `my_trades`) plus capability flags. The executor, the reconciler and the risk manager only
ever see that shape, so a new exchange is one file.

| flag | true on | when false |
|---|---|---|
| `supports_stop_orders` | Binance and the connectors whose API offers resting stop orders | the protective stop is enforced by the engine's tick watchdog (market sell at the stop); the engine must keep running |
| `supports_kline_stream` | Binance | `PollingKlineStream` reads REST candles every `EXCHANGE_POLL_SECONDS` |
| `supports_user_stream` | Binance | fills are found by the REST reconciliation sweep (every 2 min, 30 s with a position open) |
| `supports_earn`, `supports_fee_token`, `supports_futures`, `supports_key_permissions` | Binance | the feature and its health checks are skipped |

`app/exchange/registry.py` lists the connectors and builds the one named by `EXCHANGE`. Symbols are canonical
inside the engine (`BTCUSDT`, `ETHIRT`); `IRT` is the Toman, and a connector whose exchange speaks Rial converts
at its boundary. Order placement is sent exactly once: a timeout or 5xx means "unknown", and the connector looks
the order up by its client id instead of resending it. Details per exchange: [exchanges.md](exchanges.md).

## Decision pipeline (portfolio4h, the default core)

One evaluation per closed 4h bar, for the whole universe at once (`app/workers/trader.py`). The engine waits
until every symbol's bar for that open time is stored, or 90 s after the close; BTC's bar is mandatory because it
is the gate.

1. **Features** (`app/features/build.py`): EMA200, ATR(14), ADX and the rest on 4h candles; at least 260 bars per
   symbol.
2. **Market gate**: BTC's 4h close above its EMA200 (and, for the channel entry, that EMA above its value 24 h
   earlier). Off = no new entries, and every open position of both families is closed at the bar close.
3. **Momentum family**: once a day rank the universe by 7-day minus 1-day return; long the top 3; exit when a
   name ranks below 5. 3 ATR stop, no target, no trailing; 1.5 % risk, 35 % position cap.
4. **Turtle family**: long on a 4h close above the previous 55-bar high while the symbol is above its own EMA200;
   exit on a close below the previous 20-bar low. 2.5 ATR stop; 1 % risk, 25 % position cap; 240 min cooldown
   after a loss.
5. **Exits before entries**: every open position is checked against the rule of the family that opened it, then
   its hard stop. Between bars the tick watchdog enforces the stop on every price update.
6. **Risk** (`app/risk/manager.py`): armed, kill switch on **closed** equity, daily-loss pause, max concurrent
   positions, per-family cap of 3, exchange filters (tick, step, minimum notional).
7. **LLM** (`app/llm/advisor.py`): may veto or shrink a trade (0.25x to 1.0x), never open one.
8. **Execution** (`app/execution/executor.py`): paper or live. Live places a protective stop on the exchange when
   the exchange supports one.
9. **Guarantees**: an exit the exchange refuses is retried every 60 s until done; a failed exit re-places the stop
   it cancelled; bars that closed while the engine was down are caught up in order on restart.

`ensemble30m` (six rule strategies + LightGBM gate on 30m bars) is kept for comparison; it had no robust edge in
the multi-window tests.

The research harness in `app/research` replays the same `app/strategies/core4h.py` functions over twelve
historical regime windows (2021-2026) on Binance public candles:

```bash
docker compose -f docker-compose.yml -f docker-compose.research.yml run --rm --no-deps engine \
    python -u -m app.research.alt.run --strategies xs_momentum_regime_exit,turtle_regime_risk1
```

Past results are no promise of future ones; see [DISCLAIMER](../DISCLAIMER.md).

## AI layer

The advisor only ever vetoes or resizes. `app/llm/providers.py` holds the back ends behind one `complete_json()`:

| `LLM_PROVIDER` | endpoint | notes |
|---|---|---|
| `gisoo` | `https://gisoo.pro/api/v1` (OpenAI-compatible) | 400+ models with one key, billed in Toman; any model id (`gpt-4o-mini`, `claude-sonnet-5-5`, ...) |
| `netarz` | `https://netarz.ir/api/ai/v1` (OpenAI-compatible) | GPT, Gemini, DeepSeek, Claude via `openrouter/anthropic/...` |
| `anthropic` | Anthropic SDK; `ANTHROPIC_BASE_URL=https://gisoo.pro/api` routes it through Gisoo | schema enforced server side |
| `openai` | OpenAI chat completions | strict `json_schema` where the model supports it |
| `deepseek` | DeepSeek chat completions | `json_object` mode |

`auto` takes the first of gisoo, netarz, anthropic, openai, deepseek that has a key. `LLM_FALLBACK_PROVIDER` is
tried once when the primary fails. JSON-mode answers are validated locally and retried once with the validation
error attached. No key, an exhausted `LLM_DAILY_CALL_BUDGET` or `LLM_ENABLED=false` all mean the same thing: the
quantitative rules trade on their own.

**Committee** (`app/committee/`, `COMMITTEE_ENABLED`): twice a day a numeric briefing (account, positions, regime,
performance, health checks) and the policy in `app/committee/policy.md` go to the same provider chain; only the
market stance and the risk multiplier for new entries are applied automatically, everything else is a proposal
the user approves or declines on the panel's «کمیته» page.

## NetArz FX

`app/exchange/fx.py` reads Toman rates from `https://netarz.ir/api/fx/v1/rates` with `NETARZ_FX_KEY`
(display only: the dashboard shows the account in Toman next to USDT). Keys are locked to the server IPs listed on
the NetArz app; a refusal comes back as `available=false` with the IP NetArz saw.

## Exchange synchronisation

- **Pull**: every 2 minutes (30 s while a position is open) a REST sweep compares the account with the database.
  A position whose coins are gone is booked from its stop order when that order filled, otherwise as
  `closed_outside_engine` at the mark; untracked holdings are reported, never sold; deposits and withdrawals are
  recorded as capital flows, not profit.
- **Push** (Binance only): the user-data stream reports fills within milliseconds when Binance offers it.

## Security

The panel is a single account (`PANEL_USERNAME`), code in `app/api/`:

- **Password**: store a bcrypt hash (`python -m app.api.hash_password`) in `PANEL_PASSWORD_HASH`.
- **Brute force**: failed logins are counted per IP and per username in Redis; 5 failures lock both for 15
  minutes, doubling up to 24 hours.
- **Two-factor (TOTP)** with backup codes, enabled from the panel.
- **Passkeys (WebAuthn / FIDO2)**: hardware security keys or platform passkeys, bound per host; optional
  passkey-only login. Recovery when every key is lost:
  `docker compose exec postgres psql -U trader -d trader -c "delete from settings where key = 'passkey_required'"`.
- **Sessions**: revocable JWTs; "sign out everywhere" bumps a token version.
- **Network**: the panel binds to `127.0.0.1` by default. Publish it only over HTTPS (Caddy with a hostname, or
  Cloudflare Tunnel).

Exchange keys: trade-only, never withdrawal, IP-restricted where the exchange allows it. The scheduler watches the
public IP and alerts when it changes.

## Operating

| task | how |
|---|---|
| arm / disarm | panel → تنظیمات → مسلح کن (live requires typing `ARM LIVE`) |
| emergency stop | panel → داشبورد → توقف اضطراری |
| add capital | transfer on the exchange, then panel → سرمایه → واریز |
| change risk | panel → تنظیمات (applied at runtime) |
| change exchange / symbols / quote / strategy core | edit `.env`, restart |
| logs | `docker compose logs -f engine` (also `scheduler`, `marketdata`, `api`) |
| backup now / restore | `./start.sh backup` · `scripts/restore.sh <dump>` (Windows: `.\start.ps1 -Backup` / `-Restore`) |
| update | `git pull`, then `./start.sh rebuild` |

Schema changes: the one-shot `migrate` service runs `python -m app.db.migrate`, which applies Alembic revisions
when there are any and otherwise the `create_all` bootstrap. See `trader/engine/alembic/README.md`.

## Deploying to a VPS

1. Install Docker and enable time sync (`sudo timedatectl set-ntp true`).
2. Copy the project and `.env`. Point a DNS record at the server and add to `.env`:
   `PUBLIC_SITE_ADDRESS=trader.example.com` and `COMPOSE_FILE=docker-compose.yml:docker-compose.vps.yml`.
   Caddy then gets a Let's Encrypt certificate and serves HTTPS on 443.
3. Firewall: allow 22, 80, 443 only. `./start.sh autostart` installs a systemd unit.
4. Restrict the exchange API key to the server's IP; set `PANEL_ORIGINS` / `WEBAUTHN_ORIGINS` to the public origin.

## Layout

```
.env.example               every setting, documented
docker-compose.yml         the whole stack (incl. migrate + backup services)
docker-compose.vps.yml     override for a public hostname: ports 80/443 for TLS
start.ps1 / start.sh       one-command start, backup/restore, autostart
scripts/                   backup.sh, restore.sh (host), pg-backup.sh (backup container)
trader/engine/app/
  config.py                settings (pydantic)
  exchange/                base contract, registry, Binance, iran/ (Iranian connectors), fx (NetArz FX)
  data/ingest.py           backfill, websocket and polling kline streams
  strategies/core4h.py     the 4h portfolio core (momentum + turtle, BTC gate)
  research/                multi-regime test harness
  llm/                     advisor and providers (Gisoo, NetArz, Anthropic, OpenAI, DeepSeek)
  committee/               the AI committee and its policy
  notify/                  Telegram + Web Push notifications in Persian
  risk/manager.py          every gate before an order
  execution/executor.py    paper and live executors
  workers/                 market_data, trader, scheduler, reconcile, bus
  api/                     FastAPI app, auth, passkeys
trader/panel/              React + Vite + Tailwind PWA (RTL, Persian)
trader/caddy/Caddyfile     reverse proxy, TLS
```
