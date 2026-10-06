# صرافی‌ها | Exchanges

صرافی با متغیر `EXCHANGE` در `.env` انتخاب می‌شود. بایننس اتصال مرجع است و ربات روی آن ساخته و اجرا شده؛ اتصال
صرافی‌های ایرانی **آزمایشی (beta)** است: از روی مستندات رسمی هر صرافی نوشته شده، داده‌ی عمومی بازارشان (کندل، قیمت،
فهرست بازارها) به‌طور واقعی آزموده شده، ولی ثبت سفارش با پول واقعی هنوز روی همه‌ی آن‌ها آزموده نشده است. پیش از استفاده‌ی
واقعی، ربات را در حالت `paper` و بعد با مبلغ کم اجرا کنید و نتیجه را در Issues گزارش دهید.

| صرافی | `EXCHANGE` | کلید | حد ضرر | بازارها | واحد قیمت |
|---|---|---|---|---|---|
| بایننس | `binance` | API key + secret (HMAC) | روی صرافی (stop-limit) | USDT و ... | — |
| نوبیتکس | `nobitex` | کلید عمومی + کلید خصوصی Ed25519 (یا توکن) | روی صرافی (stop-limit) | USDT و تومان | API به ریال؛ ربات به تومان تبدیل می‌کند |
| والکس | `wallex` | فقط API key | روی صرافی (stop-limit) | USDT و تومان (TMN) | تومان |
| بیت‌پین | `bitpin` | API key + secret ← توکن JWT | روی صرافی (stop-limit) | USDT و تومان | تومان |
| رمزینکس | `ramzinex` | API key + secret ← توکن | **توسط خود ربات** | USDT و ریال (IRR) | API به ریال؛ ربات به تومان تبدیل می‌کند |
| اوکی اکسچنج | `okex` | API key + secret (HMAC)، IP اجباری | روی صرافی (stop-limit) | بیشتر USDT؛ USDT/تومان | — |

**تبدیل** (`tabdeal`) فعلاً نیست: API آن کندل ندارد و استراتژی بدون کندل کار نمی‌کند. اکسکوینو مستندات عمومی API ندارد.

**قاعده‌ی کلید:** فقط مجوز خواندن و معامله، **هرگز برداشت**، و اگر صرافی اجازه می‌دهد محدود به IP سرور.

**حد ضرر توسط ربات** یعنی صرافی سفارش استاپ ندارد و ربات با هر به‌روزرسانی قیمت (هر `EXCHANGE_POLL_SECONDS` ثانیه)
خودش بررسی می‌کند و با رسیدن قیمت به حد ضرر می‌فروشد. پس در آن صرافی ربات باید همیشه روشن و آنلاین بماند.

---

## English

Select the exchange with `EXCHANGE` in `.env`. Binance is the reference connector. The Iranian connectors are
**beta**: written against each exchange's official API documentation (fetched 2026-10-06), with their public market
data (markets, candles, prices) checked live; order placement has not yet been run with real money on every exchange.
Run in `paper` mode first, then with a small amount, and report what you see.

### How the engine treats every exchange the same

- `app/exchange/base.py` defines one contract with Binance-shaped answers; each connector translates its exchange.
- **Symbols** are canonical inside the engine: `BTCUSDT`, `BTCIRT`. `IRT` is the **Toman**. Nobitex (orders, book) and
  Ramzinex speak Rial; their connectors multiply by 10 on the way out and divide on the way in.
- **4h bars are UTC-aligned.** Nobitex, Bitpin and Ramzinex align their own 1h/4h candles to Tehran time (UTC+3:30),
  so those connectors fetch 30m candles (UTC-aligned) and build 1h/4h bars; Wallex builds 4h from its UTC 1h candles;
  OK-EX serves UTC 4h directly. The connectors never trust the requested resolution: off-grid bars are dropped.
- **Market data** is polled over REST every `EXCHANGE_POLL_SECONDS` (no candle websockets are used).
- **Fills** are reconciled by the REST sweep (every 2 minutes, 30 s with a position open).
- **Orders are sent once.** A timeout or 5xx on placement is resolved by looking the order up (client id, or on
  Ramzinex the recent orders with the same side and amount), never by resending it.
- **Market orders that rest** (acknowledged before they match) are polled until final; the remainder is cancelled
  and exactly what filled is booked.
- **Marketable limits** (Ramzinex, OK-EX): limit orders through the best price, 4 s each; a sell widens 1 % → 2.5 %
  → 5 % until it fills (exits and engine-managed stops must happen), a buy tries once at 1 %.
- **Spot only:** shorts and futures are refused on every Iranian exchange; `TRADING_MODE=testnet` is refused too
  (they have no testnet).

### Per exchange

| | Nobitex | Wallex | Bitpin | Ramzinex | OK-EX |
|---|---|---|---|---|---|
| Base URL | `apiv2.nobitex.ir` | `api.wallex.ir` | `api.bitpin.org` | `api.ramzinex.ir`, `publicapi.ramzinex.ir` | `sapi.ok-ex.io` |
| Auth | Ed25519 signature (`Nobitex-Key/-Signature/-Timestamp`) or `Authorization: Token` | `x-api-key` | key+secret → JWT (15 min), shared via Redis (token endpoints: 200/day) | key+secret → token; `Authorization2: Bearer` + `x-api-key` | HMAC-SHA256 hex, `x-api-key/x-signature/x-timestamp` |
| Market symbol | `srcCurrency/dstCurrency` (`btc`/`rls`) | `BTCTMN`, `BTCUSDT` | `BTC_IRT` | numeric `pair_id` | `BTC-USDT` |
| 4h source | 30m → 4h for the last 6 days; older: own 4h shifted 30 min onto UTC (30m history is ~7 days) | 1h → 4h | 30m → 4h | 30m (+1 s, floored) → 4h | native 4h |
| Stop order | `stop_limit` / `stop_market` / OCO | `STOP_LIMIT` / `STOP_MARKET` (`stop_Price`) | `stop_limit` / OCO | none → engine-managed | `STOP_LIMIT` / `STOP_MARKET` |
| Market order | `execution=market`, base amount | `MARKET`, base qty (+ last price, docs mark it required) | `market`, `base_amount` | marketable limit | marketable limit |
| Client order id | `clientOrderId` (≤32) | `client_id` (A–Z 0–9 _), the only handle | `identifier` | none | `clientOrderId` |
| Notes | failures arrive as HTTP 200 `status:failed`; FAQ asks for an Iranian IP | the live order book labels sides from the taker's view (handled) | IP list mandatory per key | no fills endpoint; balances from `summaryDesktop` | one Toman pair (USDT-IRT) |
| Docs | [apidocs.nobitex.ir](https://apidocs.nobitex.ir) | [developers.wallex.ir](https://developers.wallex.ir/docs/) | [docs.bitpin.ir](https://docs.bitpin.ir/v1/) | [docs.ramzinex.ir](https://docs.ramzinex.ir) | [docs.ok-ex.io](https://docs.ok-ex.io/docs/api/getting-started) |

### Not verifiable from the docs (treated conservatively)

- **Wallex**: whether `value` in a balance includes `locked` (treated as the total); the full order-status list.
- **Bitpin**: minimum order size (floors of 100,000 Toman / 5 USDT are applied); the fiat wallet label; key
  permission options.
- **Ramzinex**: token lifetime and rate limits; whether `status_id` uses the same codes as the `states` filter.
- **OK-EX**: fetching a single order by id (looked up in open + history lists); whether `balance` excludes `frozen`.
- **Nobitex**: whether private calls work from outside Iran.

### Adding an exchange

1. Subclass `RestExchange` (`app/exchange/iran/common.py`): markets, candles, balances, orders.
2. Register it in `app/exchange/registry.py` and `ExchangeName` in `app/config.py`.
3. Add network-free tests with `httpx.MockTransport` (see `tests/test_iran_connectors.py`).
4. Cite the official docs at the top of the file and add a row here.
