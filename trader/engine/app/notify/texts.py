"""Persian wording for notifications, and the event -> category mapping.

Everything the user reads on the phone comes from here, so the tone stays
the panel's: short, direct, latin digits for every number. `classify_event`
turns an audit-trail row (level, source, message) into a category plus a
Persian title/body; anything it does not recognise still goes out under a
generic title with the original message as the body, because a message the
code did not anticipate is exactly the one the user must not miss.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = ("trade", "risk", "control", "exchange", "system", "ai")

CATEGORY_FA = {
    "trade": "معاملات",
    "risk": "ریسک",
    "control": "کنترل",
    "exchange": "صرافی",
    "system": "سیستم",
    "ai": "هوش مصنوعی",
}

CATEGORY_EMOJI = {
    "trade": "💱",
    "risk": "🛑",
    "control": "🎛",
    "exchange": "🏦",
    "system": "⚙️",
    "ai": "🤖",
}

# Where a tap on the notification lands in the panel.
CATEGORY_URL = {
    "trade": "/trades",
    "risk": "/",
    "control": "/settings",
    "exchange": "/",
    "system": "/notifications",
    "ai": "/signals",
}

SIDE_FA = {"long": "خرید", "short": "فروش", "BUY": "خرید", "SELL": "فروش"}

REASON_FA = {
    "stop_loss": "حد ضرر",
    "take_profit": "حد سود",
    "time_stop": "توقف زمانی",
    "signal_exit": "خروج قانون استراتژی",
    "regime_exit": "خروج رژیم بیت‌کوین",
    "end_of_window": "پایان بازه",
    "manual": "دستی",
    "manual flatten": "بستن دستی همه",
    "exchange_stop": "استاپ صرافی",
    "exchange_fill": "پرشدن در صرافی",
    "closed_outside_engine": "خارج از ربات بسته شد",
}

FAMILY_FA = {
    "momentum": "مومنتوم مقطعی",
    "turtle": "شکست کانال (لاک‌پشت)",
    "ensemble": "انسمبل ۳۰ دقیقه",
}

STANCE_FA = {"risk_on": "ریسک‌پذیر", "neutral": "خنثی", "risk_off": "ریسک‌گریز"}
MODE_FA = {"paper": "آزمایشی (پول فرضی)", "testnet": "تست‌نت", "live": "واقعی"}
CORE_FA = {"portfolio4h": "پرتفوی ۴ساعته", "ensemble30m": "انسمبل ۳۰ دقیقه"}

# Exchange error codes (Binance's) that mean "the connection is broken",
# not "this order was bad": they belong to the exchange category whatever
# the source, because the fix is on the user's side (IP list, clock, key).
INFRA_ERROR_CODES = ("-2015", "-1021", "-2010", "-1022")

TEST_TITLE = "اعلان آزمایشی"
TEST_BODY = "اگر این را می‌بینی، این کانال درست کار می‌کند. ربات از این به بعد هر کاری بکند، همین‌جا خبرت می‌کند."

CONNECTED_TEXT = "✅ Gisoo Trader وصل شد"


@dataclass(frozen=True)
class Classified:
    category: str
    title: str
    body: str


def side_fa(side: str | None) -> str:
    return SIDE_FA.get(side or "", side or "")


def reason_fa(reason: str | None) -> str:
    return REASON_FA.get(reason or "", reason or "")


def family_fa(family: str | None) -> str:
    return FAMILY_FA.get(family or "", family or "")


def generic_title(category: str, level: str) -> str:
    """Title for a message no rule matched: the category, coloured by level."""
    name = CATEGORY_FA.get(category, CATEGORY_FA["system"])
    if level == "error":
        return f"خطا در {name}"
    if level == "warn":
        return f"هشدار {name}"
    if level == "trade":
        return "معامله"
    return name


# --------------------------------------------------------------------------
# per-source rules
# --------------------------------------------------------------------------

# Sentinel: the rule matched and the event must not be sent at all.
SKIP = Classified("", "", "")


def _engine(level: str, message: str, data: dict) -> Classified | None:
    if message.startswith("Trading engine online"):
        return Classified(
            "system", "موتور معامله‌گر روشن شد",
            f"حالت {MODE_FA.get(data.get('mode', ''), data.get('mode', ''))} · "
            f"هسته {CORE_FA.get(data.get('core', ''), data.get('core', ''))} · "
            f"{len(data.get('symbols') or [])} نماد · "
            f"مسلح: {'بله' if data.get('armed') else 'خیر'}",
        )
    m = re.match(r"Opened (\w+) (\S+) (\S+) @ ([\d.]+) \(stop ([\d.]+)", message)
    if m:
        side, symbol, qty, price, stop = m.groups()
        family = data.get("strategy")
        return Classified(
            "trade", f"پوزیشن باز شد: {symbol}",
            f"{side_fa(side)} {qty} @ {price} · حد ضرر {stop}"
            + (f" · {family_fa(family)}" if family else ""),
        )
    m = re.match(
        r"Closed (\w+) (\S+) @ ([\d.]+) \((.+?)\) PnL ([+-][\d.]+) (\w+) \(([+-][\d.]+)%, ([+-][\d.]+)R\)",
        message,
    )
    if m:
        side, symbol, price, reason, pnl, quote, pct, r = m.groups()
        won = float(pnl) >= 0
        return Classified(
            "trade", f"{'✅ سود' if won else '🔻 ضرر'} {symbol}",
            f"{side_fa(side)} بسته شد در {price} ({reason_fa(reason)}) · "
            f"{'سود' if won else 'ضرر'} {pnl} {quote} ({pct}٪، {r}R)",
        )
    return None


def _executor(level: str, message: str, data: dict) -> Classified | None:
    category = "exchange" if any(code in message for code in INFRA_ERROR_CODES) else "trade"
    m = re.match(r"Entry rejected for (\S+): (.+)", message, re.S)
    if m:
        return Classified(category, f"سفارش ورود رد شد: {m.group(1)}", m.group(2))
    m = re.match(r"Failed to close (\S+): (.+)", message, re.S)
    if m:
        return Classified(
            category, f"بستن پوزیشن ناموفق بود: {m.group(1)}",
            f"{m.group(2)}\nربات دوباره تلاش می‌کند و حد ضرر همچنان برقرار است.",
        )
    m = re.match(r"Order for (\S+) returned no fill", message)
    if m:
        return Classified("trade", f"سفارش پر نشد: {m.group(1)}", "صرافی سفارش ورود را بدون پرشدن برگرداند؛ این سیگنال رد شد.")
    m = re.match(r"Exit order for (\S+) did not fill \((\w+)\)", message)
    if m:
        return Classified("trade", f"سفارش خروج پر نشد: {m.group(1)}", f"وضعیت {m.group(2)}؛ ربات دوباره تلاش می‌کند.")
    m = re.match(r"Partial exit on (\S+): (\S+)/(\S+)", message)
    if m:
        return Classified("trade", f"خروج ناقص: {m.group(1)}", f"{m.group(2)} از {m.group(3)} فروخته شد؛ بقیه در تیک بعدی.")
    m = re.match(r"Protective stop for (\S+) was not accepted: (.+?)\. The engine", message, re.S)
    if m:
        return Classified(
            category, f"استاپ محافظ ثبت نشد: {m.group(1)}",
            f"{m.group(2)}\nحد ضرر را خود ربات دنبال می‌کند؛ اگر ربات خاموش شود، این پوزیشن بدون استاپ می‌ماند.",
        )
    m = re.match(r"(\S+): no (\S+) left to sell", message)
    if m:
        return Classified("trade", f"چیزی برای فروش نماند: {m.group(1)}", "استاپ صرافی یا فروش دستی زودتر آن را فروخته؛ منتظر همگام‌سازی برای بستن رکورد.")
    return None


def _risk(level: str, message: str, data: dict) -> Classified | None:
    if message.startswith("Trailing stop for"):
        return SKIP  # every candle under the ensemble core; the panel shows it
    m = re.match(r"Kill switch: (.+?) drawdown ([\d.]+)% reached the ([\d.]+)% limit", message)
    if m:
        return Classified(
            "risk", "توقف اضطراری خودکار فعال شد",
            f"افت سرمایه {m.group(2)}٪ به حد {m.group(3)}٪ رسید. همه پوزیشن‌ها بسته می‌شوند و "
            "تا وقتی خودت دوباره مسلح نکنی، معامله‌ای انجام نمی‌شود.",
        )
    m = re.match(r"Daily loss limit hit \(([-\d.]+)%\)\. Trading paused until (\S+)", message)
    if m:
        return Classified(
            "risk", "حد ضرر روزانه فعال شد",
            f"ضرر امروز {m.group(1)}٪ شد. تا {m.group(2)} (UTC) ورود جدیدی انجام نمی‌شود؛ پوزیشن‌های باز مدیریت می‌شوند.",
        )
    return None


def _exchange(level: str, message: str, data: dict) -> Classified | None:
    if message.startswith("Exchange: "):
        # Per-order status reports from the user stream. Our own fills are
        # already announced as "opened" / "closed"; a fill by hand in the
        # exchange app is the user's own doing.
        return SKIP
    m = re.match(r"Exchange closed (\S+) @ ([\d.]+) \((\w+)\) PnL ([+-][\d.]+) (\w+)", message)
    if m:
        symbol, price, reason, pnl, quote = m.groups()
        won = float(pnl) >= 0
        return Classified(
            "exchange", f"صرافی پوزیشن را بست: {symbol}",
            f"در {price} ({reason_fa(reason)}) · {'سود' if won else 'ضرر'} {pnl} {quote}",
        )
    return None


def _reconcile(level: str, message: str, data: dict) -> Classified | None:
    m = re.match(r"(\S+) is still held on .+?; the position was re-opened", message)
    if m:
        return Classified("exchange", f"پوزیشن دوباره باز شد: {m.group(1)}", "کوین‌ها هنوز در صرافی هستند؛ ربات دوباره مدیریتش را به عهده گرفت.")
    m = re.match(r"(\S+) is no longer held on .+? - the position was closed outside the engine\. Marked closed at ([\d.]+)", message)
    if m:
        return Classified(
            "exchange", f"پوزیشن خارج از ربات بسته شد: {m.group(1)}",
            f"صرافی دیگر این کوین را ندارد (فروش دستی یا استاپ). با قیمت {m.group(2)} بسته ثبت شد.",
        )
    if re.match(r"Balances held on .+? that the engine is not managing", message):
        body = message.split(": ", 1)[1] if ": " in message else message
        body = body.split(". The engine will not sell", 1)[0]
        return Classified("exchange", "دارایی‌های خارج از مدیریت ربات", body + "\nربات این‌ها را نمی‌فروشد؛ خودت تصمیم بگیر.")
    m = re.match(r"Capital (deposit|withdrawal) of ([\d.]+) (\w+) detected on ", message)
    if m:
        kind, amount, asset = m.groups()
        return Classified(
            "control", f"{'واریز' if kind == 'deposit' else 'برداشت'} شناسایی شد",
            f"{amount} {asset} در صرافی {'اضافه' if kind == 'deposit' else 'کم'} شد و به‌عنوان جریان سرمایه ثبت شد، نه سود/ضرر.",
        )
    return None


def _llm(level: str, message: str, data: dict) -> Classified | None:
    m = re.match(r"Market stance: (\w+) \(risk x([\d.]+)\) - (.*)", message, re.S)
    if m:
        stance, mult, reasoning = m.groups()
        return Classified(
            "ai", f"دیدگاه بازار کلود: {STANCE_FA.get(stance, stance)}",
            f"ضریب ریسک ×{mult}\n{reasoning.strip()}",
        )
    m = re.match(r"Claude vetoed (\w+) (\S+): (.*)", message, re.S)
    if m:
        side, symbol, reasoning = m.groups()
        return Classified("ai", f"کلود معامله را وتو کرد: {symbol}", f"{side_fa(side)} · {reasoning.strip()}")
    return None


def _ml(level: str, message: str, data: dict) -> Classified | None:
    m = re.match(r"Model retrained for (\S+): AUC ([\d.]+) on (\d+) rows", message)
    if m:
        return Classified("ai", f"مدل یادگیری ماشین بازآموزی شد: {m.group(1)}", f"AUC {m.group(2)} روی {m.group(3)} نمونه")
    return None


def _panel(level: str, message: str, data: dict) -> Classified | None:
    m = re.match(r"Settings updated by (\S+): (.*)", message, re.S)
    if m:
        return Classified("control", "تنظیمات تغییر کرد", f"توسط {m.group(1)}: {m.group(2)}")
    m = re.match(r"Trading (ARMED|DISARMED) by (\S+) \((\w+) mode\)", message)
    if m:
        armed = m.group(1) == "ARMED"
        return Classified(
            "control", "ربات مسلح شد" if armed else "ربات غیرمسلح شد",
            f"توسط {m.group(2)} · حالت {MODE_FA.get(m.group(3), m.group(3))}"
            + (" · از این لحظه سفارش واقعی می‌فرستد." if armed and m.group(3) == "live" else ""),
        )
    m = re.match(r"KILL SWITCH engaged by (\S+)", message)
    if m:
        return Classified("risk", "توقف اضطراری فعال شد", f"توسط {m.group(1)}. همه پوزیشن‌ها بسته می‌شوند؛ ادامه فقط با مسلح کردن دوباره.")
    m = re.match(r"Trading resumed by (\S+)", message)
    if m:
        return Classified("control", "توقف برداشته شد", f"توسط {m.group(1)}؛ ربات دوباره می‌تواند وارد معامله شود.")
    m = re.match(r"Command '(\w+)' issued by (\S+)", message)
    if m:
        names = {
            "close_position": "بستن پوزیشن", "flatten_all": "بستن همه پوزیشن‌ها",
            "evaluate_now": "ارزیابی فوری", "reload_models": "بارگذاری دوباره مدل‌ها",
        }
        detail = f" (پوزیشن {data['position_id']})" if data.get("position_id") else ""
        return Classified("control", f"فرمان: {names.get(m.group(1), m.group(1))}{detail}", f"توسط {m.group(2)}")
    return None


def _capital(level: str, message: str, data: dict) -> Classified | None:
    m = re.match(r"(Deposit|Withdrawal) of ([\d.]+) (\w+) recorded by (\S+)\. Capital base is now ([\d.]+)", message)
    if m:
        kind, amount, asset, user, base = m.groups()
        return Classified(
            "control", f"{'واریز' if kind == 'Deposit' else 'برداشت'} ثبت شد",
            f"{amount} {asset} توسط {user} · سرمایه پایه اکنون {base} {asset}",
        )
    return None


def _auth(level: str, message: str, data: dict) -> Classified | None:
    if "waiting for the 2FA code" in message:
        return SKIP  # the login itself is announced when it completes
    m = re.match(r"Login successful for (\S+) from (\S+) using a BACKUP code \((\d+) left\)", message)
    if m:
        return Classified("system", "ورود با کد پشتیبان", f"{m.group(1)} از {m.group(2)} · {m.group(3)} کد پشتیبان مانده")
    m = re.match(r"Login successful for (\S+) from (\S+)", message)
    if m:
        return Classified("system", "ورود به پنل", f"{m.group(1)} از {m.group(2)}" + (" · با تایید دومرحله‌ای" if "(2FA)" in message else ""))
    m = re.match(r"Failed login for '(.*?)' from (\S+) \((\w+)\)", message)
    if m:
        return Classified("system", "تلاش ورود ناموفق", f"کاربر «{m.group(1)}» از {m.group(2)} (مرحله {m.group(3)})")
    m = re.match(r"Login locked for (\d+) min after repeated failures \(user '(.*?)', ip (\S+)\)", message)
    if m:
        return Classified("system", "ورود قفل شد", f"{m.group(1)} دقیقه بعد از چند تلاش ناموفق · کاربر «{m.group(2)}» از {m.group(3)}")
    m = re.match(r"All sessions signed out by (\S+)", message)
    if m:
        return Classified("system", "خروج از همه دستگاه‌ها", f"توسط {m.group(1)}")
    m = re.match(r"Two-factor authentication (ENABLED|DISABLED) by (\S+) from (\S+)", message)
    if m:
        on = m.group(1) == "ENABLED"
        return Classified("system", f"تایید دومرحله‌ای {'فعال' if on else 'غیرفعال'} شد", f"توسط {m.group(2)} از {m.group(3)}")
    return None


def _marketdata(level: str, message: str, data: dict) -> Classified | None:
    m = re.match(r"Market data starting for (\d+) symbols", message)
    if m:
        return Classified("system", "داده بازار روشن شد", f"{m.group(1)} نماد · تایم‌فریم {', '.join(data.get('timeframes') or [])}")
    m = re.match(r"Symbols skipped: (.*)", message)
    if m:
        return Classified("system", "نمادهای نادیده‌گرفته‌شده", f"{m.group(1)} در صرافی معتبر نبودند و معامله نمی‌شوند.")
    return None


_BY_SOURCE = {
    "engine": _engine,
    "executor": _executor,
    "risk": _risk,
    "exchange": _exchange,
    "reconcile": _reconcile,
    "llm": _llm,
    "ml": _ml,
    "panel": _panel,
    "capital": _capital,
    "auth": _auth,
    "marketdata": _marketdata,
}

# Sources without a rule table still need a category.
_DEFAULT_CATEGORY = {
    "engine": "system",
    "executor": "trade",
    "risk": "risk",
    "exchange": "exchange",
    "reconcile": "exchange",
    "llm": "ai",
    "ml": "ai",
    "panel": "control",
    "capital": "control",
    "auth": "system",
    "marketdata": "system",
    "scheduler": "system",
    "notify": "system",
    "system": "system",
}


def category_for(source: str, message: str) -> str:
    """Category for an event no wording rule claimed."""
    if any(code in message for code in INFRA_ERROR_CODES):
        return "exchange"
    return _DEFAULT_CATEGORY.get(source, "system")


def classify_event(level: str, source: str, message: str, data: dict | None = None) -> Classified | None:
    """Map an audit-trail row to (category, Persian title, body).

    Returns None when the row is deliberately not worth a phone buzz.
    """
    source = (source or "").lower()
    data = data or {}
    rule = _BY_SOURCE.get(source)
    result = rule(level, message, data) if rule else None
    if result is SKIP:
        return None
    if result is not None:
        return result
    category = category_for(source, message)
    return Classified(category, generic_title(category, level), message)
