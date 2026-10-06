# راهنمای مشارکت

خوشحالیم که می‌خواهید گیسو تریدر را بهتر کنید. این پروژه یک هستهٔ اولیه است و بیشترین کمک را این‌ها می‌کنند:

- **اتصال صرافی تازه** یا بهتر کردن اتصال‌های آزمایشی (نوبیتکس، والکس، بیت‌پین، رمزینکس، اوکی اکسچنج)؛ مخصوصاً گزارش
  اجرای واقعی با مبلغ کم و اصلاح تفاوت‌ها با مستندات صرافی
- **آزمون (تست)** برای مسیر سفارش، همگام‌سازی حساب و حد ضرر
- **مستندات فارسی** روشن‌تر برای راه‌اندازی و رفع اشکال
- **استراتژی تازه**، به شرط آزمون چندبازه‌ای با ابزارهای پوشهٔ `trader/engine/app/research`

## پیش از فرستادن Pull Request

1. برای تغییر بزرگ، اول یک Issue باز کنید تا پیش از نوشتن کد دربارهٔ راه‌حل هم‌نظر شویم.
2. **هیچ کلید، توکن، رمز، IP یا دامنهٔ خصوصی** در کد، README یا تاریخچهٔ گیت نگذارید. اگر اشتباهی کلیدی را کامیت
   کردید، پیش از هر کاری آن را در صرافی یا پنل مربوط باطل کنید؛ پاک کردن کامیت کافی نیست.
3. مسیر سفارش (`app/execution`، `app/exchange`، `app/workers/reconcile.py`) با پول واقعی کار می‌کند: برای هر
   تغییر آن‌جا آزمون بنویسید و بگویید در حالت `paper` و با مبلغ کم چه دیدید.
4. اتصال صرافی تازه باید قرارداد `app/exchange/base.py` را پیاده کند، در `app/exchange/registry.py` ثبت شود،
   آزمون بدون شبکه (با `httpx.MockTransport`) داشته باشد و در [docs/exchanges.md](docs/exchanges.md) بیاید.
   هر ادعا دربارهٔ API صرافی باید از مستندات رسمی همان صرافی باشد؛ نشانی‌اش را در توضیح بالای فایل بگذارید.
5. کد، نام متغیرها، توضیح‌های داخل کد و پیام کامیت انگلیسی باشد. متن‌هایی که کاربر در پنل یا اعلان‌ها می‌بیند فارسی است.
6. بررسی‌ها را اجرا کنید:

   ```bash
   cd trader/engine && python -m unittest discover -s tests -v
   cd trader/panel && npm ci && npm run build
   ```

   همین‌ها در [CI](.github/workflows/ci.yml) روی هر Pull Request اجرا می‌شود و به هیچ کلیدی نیاز ندارد.
7. اگر تغییر شما برای کاربران مهم است، یک خط زیر `[Unreleased]` در [CHANGELOG.md](CHANGELOG.md) اضافه کنید.

## سبک نوشتن متن فارسی

- کاربر را ساده و کوتاه خطاب کنید. اصطلاح فنی را بار اول با توضیح بیاورید، مثل «حد ضرر (Stop Loss)».
- عددها با رقم لاتین. نام برند همیشه «نِت اَرز» (NetArz) و «گیسو» (Gisoo) است.

## مجوز

با فرستادن Pull Request می‌پذیرید که کد شما با مجوز همین مخزن (MIT) منتشر شود.
همه در این مخزن از [آیین رفتار](CODE_OF_CONDUCT.md) پیروی می‌کنند.

---

## Contributing (English)

Most welcome: new exchange connectors and real-money (small amount) reports on the beta Iranian connectors, tests for
the order path, clearer Persian docs, and strategies backed by multi-window tests (`trader/engine/app/research`).

Before a pull request: open an issue for large changes; never commit keys, tokens, passwords, IPs or private domains
(revoke a leaked key first); add tests for anything on the order path and say what you saw in `paper` mode and with a
small amount; a new connector implements `app/exchange/base.py`, is registered in `app/exchange/registry.py`, has
network-free tests (`httpx.MockTransport`), cites the exchange's official docs and is listed in `docs/exchanges.md`.
Code, comments and commit messages in English; user-facing panel and notification texts in Persian. Run the engine
unit tests and the panel build (both run in CI without keys). Contributions are released under the MIT license.
