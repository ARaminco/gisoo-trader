# سیاست امنیتی

## گزارش آسیب‌پذیری

اگر در گیسو تریدر مشکل امنیتی پیدا کردید، لطفاً آن را در Issue، Pull Request یا Discussion عمومی ننویسید. به
**`dev@netarz.ir`** ایمیل بفرستید و در عنوان بنویسید `Security: gisoo-trader`.

در گزارش این‌ها به ما کمک می‌کند:

- شرح کوتاه مشکل و این‌که چه کسی از آن آسیب می‌بیند
- مراحل بازتولید، یا یک نمونهٔ کوچک که مشکل را نشان می‌دهد
- فایل، نسخه یا endpoint مربوط
- اگر راه‌حلی به ذهنتان رسیده، آن را هم

در گزارش کلید واقعی صرافی، رمز پنل، توکن ربات یا اطلاعات شخصی کسی را نفرستید.

دریافت گزارش را با ایمیل تأیید می‌کنیم، دربارهٔ اصلاح خبرتان می‌کنیم و تا اصلاح منتشر نشده جزئیات را عمومی نمی‌کنیم.
از شما هم همین را می‌خواهیم.

## کلید شما لو رفته؟

این مورد آسیب‌پذیری نیست ولی فوری است:

1. همان لحظه کلید را از پنل صرافی (یا نِت اَرز / گیسو) **باطل کنید** و کلید تازه بسازید.
2. اگر کلید مجوز برداشت داشت، موجودی و تاریخچهٔ برداشت حساب را بررسی کنید.
3. پاک کردن کامیت یا فایل کافی نیست؛ کلید در تاریخچهٔ گیت و کش‌های عمومی می‌ماند.

## توصیه‌های امنیتی برای اجرا

- کلید صرافی را فقط با مجوز معامله بسازید، هرگز با مجوز برداشت، و اگر صرافی اجازه می‌دهد به IP سرور محدودش کنید.
- `PANEL_PASSWORD_HASH` و `JWT_SECRET` را حتماً عوض کنید؛ پنل با مقدار پیش‌فرض هشدار می‌دهد.
- پنل به‌طور پیش‌فرض فقط روی همین رایانه (`127.0.0.1`) در دسترس است. برای دسترسی از بیرون از HTTPS
  (Caddy روی VPS یا Cloudflare Tunnel) استفاده کنید و ورود دومرحله‌ای یا کلید امنیتی را در پنل روشن کنید.
- فایل `.env` را هرگز کامیت نکنید؛ در `.gitignore` آمده است.

## نسخه‌های پشتیبانی‌شده

شاخهٔ `main`.

## خارج از این سیاست

- حملهٔ از کار انداختن سرویس (DoS) و آزمایش بار روی سرورهای نِت اَرز، گیسو یا صرافی‌ها
- مهندسی اجتماعی، فیشینگ یا حمله به کارمندان و کاربران
- گزارش خودکار اسکنرها بدون نشان دادن اثر واقعی

---

## Security policy (English)

Please do not report security issues in public issues, pull requests or discussions. Email **`dev@netarz.ir`** with
the subject `Security: gisoo-trader`, including a description, reproduction steps, the affected file or endpoint and,
if you have one, a suggested fix. Do not send real exchange keys, panel passwords, bot tokens or personal data. We
acknowledge every report, keep you informed and do not disclose details before a fix is released; please do the same.

If your own key leaked: revoke it on the exchange (or NetArz / Gisoo) right away and create a new one; deleting the
commit is not enough. Run with trade-only, IP-restricted exchange keys, change the default panel password and JWT
secret, and only expose the panel over HTTPS with two-factor authentication or a passkey.

Supported: the `main` branch. Out of scope: denial of service and load testing, social engineering, and unverified
automated scanner output.
