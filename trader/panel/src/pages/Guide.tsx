import { useState } from 'react'
import clsx from 'clsx'
import { Card } from '@/components/ui'
import { CheckIcon, CloseIcon } from '@/components/icons'

/**
 * Persian step-by-step guide for connecting an exchange account: the Iranian
 * exchanges first, then Binance in detail. Lives inside the panel so it is
 * available on the phone, offline, next to the settings it talks about.
 */

const SECTIONS = [
  { id: 'before', label: 'قبل از شروع' },
  { id: 'iran', label: 'صرافی‌های ایرانی' },
  { id: 'create', label: 'بایننس: ساخت کلید' },
  { id: 'restrict', label: 'بایننس: محدود کردن' },
  { id: 'config', label: 'وارد کردن در سیستم' },
  { id: 'bnb', label: 'بایننس: تخفیف کارمزد' },
  { id: 'verify', label: 'بررسی اتصال' },
  { id: 'arm', label: 'شروع معامله' },
  { id: 'capital', label: 'افزایش سرمایه' },
  { id: 'trouble', label: 'رفع اشکال' },
  { id: 'security', label: 'امنیت' },
]

export default function Guide() {
  const [active, setActive] = useState('before')

  function jump(id: string) {
    setActive(id)
    document.getElementById(`guide-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="lg:grid lg:grid-cols-[13rem_1fr] gap-4 items-start">
      {/* ---- section nav: chips under the header on phones, a column on desktop ---- */}
      <nav
        className={clsx(
          'sticky z-20 -mx-4 px-4 py-2 mb-3 bg-ink-950/90 backdrop-blur',
          'lg:static lg:mx-0 lg:px-0 lg:py-0 lg:mb-0 lg:bg-transparent lg:backdrop-blur-0',
        )}
        style={{ top: 'calc(var(--header, 3.5rem) + var(--safe-top))' }}
        aria-label="بخش‌های راهنما"
      >
        <div className="flex lg:flex-col gap-1.5 overflow-x-auto no-scrollbar lg:card lg:p-2 lg:sticky lg:top-6">
          {SECTIONS.map((section, index) => (
            <button
              key={section.id}
              onClick={() => jump(section.id)}
              className={clsx(
                'text-right rounded-lg text-xs whitespace-nowrap transition-colors shrink-0 min-h-[36px] px-3 flex items-center gap-2',
                active === section.id
                  ? 'bg-accent/15 text-accent'
                  : 'bg-ink-850 lg:bg-transparent text-slate-300 hover:bg-ink-800 hover:text-slate-50',
              )}
            >
              <span className="num opacity-60">{index + 1}</span>
              {section.label}
            </button>
          ))}
        </div>
      </nav>

      <div className="space-y-4 min-w-0">
        <Card>
          <h1 className="text-lg font-bold text-slate-50 mb-2">راهنمای اتصال صرافی</h1>
          <p className="text-sm text-slate-300 leading-7">
            این راهنما قدم‌به‌قدم توضیح می‌دهد چطور حساب صرافی را به ربات وصل کنی، دسترسی کلید را طوری محدود کنی که حتی
            اگر لو رفت کسی نتواند پول برداشت کند، و مطمئن شوی همه‌چیز درست سینک شده. ربات به بایننس و به صرافی‌های ایرانی
            نوبیتکس، والکس، بیت‌پین، رمزینکس و اوکی اکسچنج وصل می‌شود؛ صرافی در فایل <Code>.env</Code> با{' '}
            <Code>EXCHANGE</Code> انتخاب می‌شود.
          </p>
        </Card>

        <Note tone="warn">
          گیسو تریدر یک هسته‌ی اولیه‌ی متن‌باز برای توسعه است. مسئولیت استفاده و هر سود یا زیانی با خود توست. اتصال
          صرافی‌های ایرانی آزمایشی است؛ اول مدتی در حالت <Code>paper</Code> و بعد با مبلغ کم امتحان کن.
        </Note>

        <Section id="before" title="قبل از شروع" number={1}>
          <Check>حساب صرافی با احراز هویت کامل — بدون آن کلید API معامله ساخته نمی‌شود.</Check>
          <Check>ورود دو مرحله‌ای (Google Authenticator یا کلید امنیتی) روی حساب صرافی فعال باشد.</Check>
          <Check>
            موجودی در کیف پول اسپات، به ارزی که در <Code>QUOTE_ASSET</Code> انتخاب کرده‌ای (<Code>USDT</Code> یا تومان با{' '}
            <Code>IRT</Code>). پیشنهاد برای شروع: <Code>USDT</Code>.
          </Check>
          <Check>
            فقط از صرافی‌ای استفاده کن که از نظر قانونی برایت در دسترس است و شرایط استفاده‌اش را رعایت کن.
          </Check>
          <Note tone="warn">
            قطع‌شدن مکرر اینترنت سرور یعنی حد ضررها دیر اجرا می‌شوند؛ مخصوصاً در صرافی‌هایی که حد ضرر را خود ربات اجرا
            می‌کند (رمزینکس). ربات را روی سیستمی با اینترنت پایدار اجرا کن.
          </Note>
        </Section>

        <Section id="iran" title="صرافی‌های ایرانی" number={2}>
          <p className="text-sm text-slate-300 leading-7">
            در هر صرافی از بخش مدیریت API یک کلید بساز. قانون همیشگی: <b>فقط مجوز خواندن و معامله</b>، هرگز برداشت؛ و اگر
            صرافی اجازه می‌دهد، کلید را به IP سرور ربات محدود کن.
          </p>
          <div className="card-raised divide-y divide-ink-700/60 overflow-hidden">
            <PermRow
              name="nobitex (نوبیتکس)"
              on={true}
              reason="کلید API با مجوز READ,TRADE و لیست IP. کلید عمومی در EXCHANGE_API_KEY و کلید خصوصی (Ed25519) در EXCHANGE_API_SECRET. مجوزها بعد از ساخت عوض نمی‌شوند. طبق مستندات، درخواست باید از IP ایران باشد. حد ضرر روی خود صرافی."
            />
            <PermRow
              name="wallex (والکس)"
              on={true}
              reason="کلید با مجوز Read و Trade (بدون Withdraw). فقط EXCHANGE_API_KEY لازم است. حد ضرر روی خود صرافی."
            />
            <PermRow
              name="bitpin (بیت‌پین)"
              on={true}
              reason="کلید و رمز کلید با لیست IP اجباری. هر دو در EXCHANGE_API_KEY و EXCHANGE_API_SECRET. حد ضرر روی خود صرافی (استاپ-لیمیت)."
            />
            <PermRow
              name="ramzinex (رمزینکس)"
              on={null}
              reason="کلید با مجوز read و trade (و cancel-order). رمزینکس سفارش حد ضرر ندارد؛ حد ضرر را خود ربات اجرا می‌کند و ربات باید روشن بماند."
            />
            <PermRow
              name="okex (اوکی اکسچنج)"
              on={true}
              reason="کلید و رمز کلید با حداقل یک IP مجاز. بیشتر بازارها تتری است. حد ضرر روی خود صرافی."
            />
          </div>
          <Step n={1}>
            در فایل <Code>.env</Code> مقدار <Code>EXCHANGE</Code> را روی نام صرافی بگذار، کلیدها را وارد کن و{' '}
            <Code>QUOTE_ASSET</Code> و <Code>SYMBOLS</Code> را با بازارهای همان صرافی هماهنگ کن (مثلاً{' '}
            <Code>BTCUSDT</Code> یا <Code>BTCIRT</Code>).
          </Step>
          <Step n={2}>
            <Code>TAKER_FEE_PCT</Code> را برابر کارمزد واقعی خودت در آن صرافی بگذار (معمولاً ۰٫۲ تا ۰٫۳۵ درصد).
          </Step>
          <Note tone="info">
            جزئیات هر صرافی، واحدها (ریال و تومان) و محدودیت‌ها در فایل <Code>docs/exchanges.md</Code> مخزن آمده است.
          </Note>
        </Section>

        <Section id="create" title="بایننس: ساخت کلید API" number={3}>
          <Step n={1}>
            وارد بایننس شو، روی آیکون پروفایل (بالا راست) کلیک کن و <b>Account → API Management</b> را باز کن. آدرس
            مستقیم: <Code>binance.com/en/my/settings/api-management</Code>
          </Step>
          <Step n={2}>
            روی <b>Create API</b> بزن. نوع را <b>System generated</b> انتخاب کن (نه Self-generated — آن برای کلیدهای
            Ed25519 است و پیچیده‌تر است).
          </Step>
          <Step n={3}>
            یک اسم بگذار، مثلاً <Code>Gisoo Trader</Code>. تأیید ایمیل و کد دو مرحله‌ای را انجام بده.
          </Step>
          <Step n={4}>
            دو مقدار نمایش داده می‌شود:
            <ul className="mt-2 space-y-1.5">
              <li>
                <b>API Key</b> — همیشه قابل مشاهده است.
              </li>
              <li>
                <b>Secret Key</b> — <span className="text-warn">فقط همین یک بار</span> نشان داده می‌شود. همین الان کپی و
                در جای امنی ذخیره کن. اگر صفحه را ببندی باید کلید را حذف و از نو بسازی.
              </li>
            </ul>
          </Step>
          <Note tone="info">
            هنوز کلید را در سیستم وارد نکن. اول در بخش بعد دسترسی‌هایش را محدود کن؛ کلیدی که هنوز محدودیت ندارد اگر لو
            برود کل حساب را در خطر می‌گذارد.
          </Note>
        </Section>

        <Section id="restrict" title="بایننس: محدود کردن دسترسی کلید (مهم‌ترین قدم)" number={4}>
          <p className="text-sm text-slate-300 leading-7 mb-3">
            در همان صفحه API Management روی کلید ساخته‌شده <b>Edit restrictions</b> را بزن و دقیقاً این‌ها را تنظیم کن:
          </p>

          <div className="card-raised divide-y divide-ink-700/60 overflow-hidden">
            <PermRow name="Enable Reading" on reason="خواندن موجودی، سفارش‌ها و تاریخچه" />
            <PermRow name="Enable Spot & Margin Trading" on reason="ثبت و لغو سفارش در بازار Spot" />
            <PermRow
              name="Enable Futures"
              on={null}
              reason="فقط اگر می‌خواهی فیوچرز را در تنظیمات روشن کنی. پیش‌فرض خاموش بماند."
            />
            <PermRow
              name="Enable Withdrawals"
              on={false}
              reason="هرگز. ربات هیچ نیازی به برداشت ندارد. این تنها چیزی است که در صورت لو رفتن کلید پولت را نجات می‌دهد."
            />
            <PermRow name="Enable Margin Loan, Repay & Transfer" on={false} reason="نیازی نیست" />
            <PermRow name="Permits Universal Transfer" on={false} reason="نیازی نیست" />
            <PermRow name="Enable Vanilla Options" on={false} reason="نیازی نیست" />
          </div>

          <h4 className="text-sm font-semibold text-slate-100 mt-5 mb-2">محدودیت IP</h4>
          <p className="text-sm text-slate-300 leading-7">
            گزینه <b>Restrict access to trusted IPs only</b> را انتخاب کن و IP سیستمی که ربات روی آن اجرا می‌شود را وارد
            کن. برای پیدا کردن IP عمومی، در همان سیستم به <Code>ifconfig.me</Code> یا <Code>whatismyip.com</Code> سر بزن.
          </p>
          <Note tone="warn">
            <b>روی ویندوز خانگی:</b> IP اینترنت خانگی معمولاً بعد از هر ری‌استارت مودم عوض می‌شود. یا از اپراتور IP ثابت
            بگیر، یا فعلاً <b>Unrestricted</b> بگذار و حتماً برداشت را خاموش کن. روی VPS همیشه IP ثابت است و حتماً محدودش
            کن — بایننس کلیدهای بدون محدودیت IP را بعد از ۹۰ روز غیرفعال می‌کند اگر دسترسی معامله داشته باشند.
          </Note>
          <Step n={5}>
            روی <b>Save</b> بزن و دوباره کد دو مرحله‌ای را تأیید کن.
          </Step>
        </Section>

        <Section id="config" title="وارد کردن کلید در سیستم" number={5}>
          <p className="text-sm text-slate-300 leading-7 mb-3">
            کلیدها در فایل <Code>.env</Code> در ریشه‌ی پروژه نگهداری می‌شوند و هرگز به دیتابیس یا گیت نمی‌روند. این فایل را
            با یک ویرایشگر متن باز کن و این خط‌ها را پر کن:
          </p>
          <CodeBlock>
            {`# binance | nobitex | wallex | bitpin | ramzinex | okex
EXCHANGE=nobitex
EXCHANGE_API_KEY=کلید API اینجا
EXCHANGE_API_SECRET=کلید Secret اینجا

# اول paper (پول فرضی)، بعد از آزمودن: live
TRADING_MODE=paper

# تا وقتی از پنل مسلح نکنی سفارشی نمی‌فرستد
TRADING_ARMED=false`}
          </CodeBlock>
          <Step n={1}>
            بعد از ذخیره، سرویس‌ها را دوباره راه‌اندازی کن تا مقدار جدید خوانده شود:
            <CodeBlock>{`.\\start.ps1        # ویندوز
./start.sh         # لینوکس / سرور`}</CodeBlock>
          </Step>
          <Note tone="info">
            <b>پیشنهاد:</b> اول یکی دو روز با <Code>TRADING_MODE=paper</Code> اجرا کن. سیستم با قیمت واقعی و پول فرضی
            معامله می‌کند و می‌توانی ببینی سیگنال‌ها و مدیریت ریسک چطور رفتار می‌کنند. بعد به <Code>live</Code> تغییر بده.
          </Note>
        </Section>

        <Section id="bnb" title="بایننس: تخفیف کارمزد با BNB" number={6}>
          <p className="text-sm text-slate-300 leading-7">
            کارمزد استاندارد Spot در بایننس <b className="num">0.1%</b> در هر سمت است. اگر کارمزد با BNB پرداخت شود،{' '}
            <b className="num">0.075%</b> می‌شود. برای حساب ۱۰۰ دلاری که ده‌ها معامله در ماه انجام می‌دهد، این تفاوت مستقیم
            به سود خالص می‌رود.
          </p>
          <Step n={1}>مقدار کمی BNB بخر (۳ تا ۵ دلار کافی است) و در کیف پول Spot نگه دار.</Step>
          <Step n={2}>
            در بایننس: پروفایل → <b>Dashboard</b> → بخش <b>Fee</b> → گزینه <b>Using BNB to pay for fees</b> را روشن کن.
            ربات هم خودش هنگام شروع سعی می‌کند این گزینه را فعال کند (<Code>BINANCE_USE_BNB_FEES=true</Code>).
          </Step>
          <Note tone="info">
            وقتی BNB تمام شود، بایننس بی‌سروصدا به کارمزد ۰.۱٪ برمی‌گردد. هر چند هفته یک بار موجودی BNB را در صفحه{' '}
            <b>سرمایه</b> چک کن.
          </Note>
        </Section>

        <Section id="verify" title="بررسی اتصال" number={7}>
          <p className="text-sm text-slate-300 leading-7 mb-3">
            بعد از راه‌اندازی، به صفحه <b>سرمایه</b> برو. در کارت «اتصال به صرافی» باید این‌ها را ببینی:
          </p>
          <Check>نقطه سبز و «متصل» — یعنی کلید معتبر است و REST کار می‌کند.</Check>
          <Check>
            فقط بایننس: «استریم لحظه‌ای حساب: فعال» — یعنی وب‌سوکت اختصاصی حساب وصل شده. در صرافی‌های ایرانی (و در
            حالت paper) این خاموش است و همگام‌سازی هر ۲ دقیقه با REST انجام می‌شود.
          </Check>
          <Check>موجودی واقعی حساب در لیست پایین همان کارت نمایش داده می‌شود.</Check>
          <Check>
            «اختلاف ساعت با سرور» کوچک باشد. صرافی‌هایی که درخواست را با زمان امضا می‌کنند (بایننس، نوبیتکس، اوکی
            اکسچنج) اختلاف زیاد را رد می‌کنند؛ ساعت سیستم را با اینترنت همگام نگه دار.
          </Check>
          <p className="text-sm text-slate-300 leading-7 mt-3">
            در صفحه <b>داشبورد</b> هم نوار وضعیت بالا باید «صرافی» را سبز نشان دهد. اگر قرمز است، بخش رفع اشکال را ببین.
          </p>
        </Section>

        <Section id="arm" title="مسلح کردن و شروع معامله" number={8}>
          <p className="text-sm text-slate-300 leading-7">
            ربات از لحظه‌ی راه‌اندازی داده می‌گیرد و سیگنال می‌سازد، ولی تا وقتی <b>مسلح</b> نشده هیچ سفارشی نمی‌فرستد.
            این یک قفل عمدی است.
          </p>
          <Step n={1}>
            به صفحه <b>تنظیمات</b> برو و مقادیر ریسک را یک بار مرور کن. پیش‌فرض‌ها برای ریسک متوسط تنظیم شده‌اند.
          </Step>
          <Step n={2}>
            در داشبورد یا کارت «کنترل اصلی» تنظیمات روی <b>مسلح کن</b> بزن. در حالت live باید عبارت{' '}
            <Code>ARM LIVE</Code> را تایپ کنی.
          </Step>
          <Step n={3}>
            از این لحظه ربات با بسته شدن هر کندل ۴ ساعته (به وقت جهانی) بازار را بررسی می‌کند. اولین معامله ممکن است
            چند روز طول بکشد — <span className="text-slate-100">این طبیعی است</span>. وقتی بیت‌کوین زیر میانگین ۲۰۰
            کندلی‌اش است، ربات عمداً وارد نمی‌شود.
          </Step>
          <Note tone="warn">
            <b>توقف اضطراری</b> در داشبورد همه‌چیز را متوقف می‌کند و پوزیشن‌ها را می‌بندد. اگر هر وقت به هر دلیل احساس
            کردی چیزی درست نیست، اول آن را بزن، بعد بررسی کن.
          </Note>
        </Section>

        <Section id="capital" title="افزایش سرمایه" number={9}>
          <Step n={1}>
            پول را در صرافی به کیف پول <b>اسپات</b> واریز یا منتقل کن.
          </Step>
          <Step n={2}>
            در پنل به صفحه <b>سرمایه</b> برو و همان مبلغ را به‌عنوان <b>واریز</b> ثبت کن.
          </Step>
          <p className="text-sm text-slate-300 leading-7 mt-3">
            چرا باید ثبت کنی؟ چون در غیر این صورت ربات مبلغ جدید را «سود» می‌بیند: درصد سود اشتباه می‌شود و مهم‌تر از آن،
            محاسبه‌ی افت سرمایه (که کلید توقف اضطراری را کنترل می‌کند) به هم می‌ریزد. ثبت واریز باعث می‌شود ریسک هر
            معامله هم متناسب با سرمایه جدید بزرگ شود.
          </p>
          <Note tone="info">
            اگر یادت رفت، ربات در سینک بعدی (هر ۲ دقیقه) تفاوت موجودی را تشخیص می‌دهد و خودش به‌عنوان «تشخیص خودکار»
            ثبت می‌کند. ولی ثبت دستی دقیق‌تر است چون زمان و توضیح درست دارد.
          </Note>
        </Section>

        <Section id="trouble" title="رفع اشکال" number={10}>
          <Trouble
            symptom="صرافی قرمز است / «API key not configured»"
            fix="فایل .env را باز کن. کلیدها نباید فاصله یا گیومه داشته باشند. بعد از هر تغییر سرویس‌ها را دوباره راه‌اندازی کن."
          />
          <Trouble
            symptom="خطای کلید (بایننس -2015، یا 401/403 در صرافی‌های دیگر)"
            fix="یا IP سیستم در لیست مجاز کلید نیست، یا مجوز معامله روشن نیست، یا کلید حذف شده یا منقضی شده. در بخش API صرافی بررسی کن."
          />
          <Trouble
            symptom="خطای زمان (بایننس -1021 یا رد امضا در نوبیتکس / اوکی اکسچنج)"
            fix="ساعت سیستم غلط است. در ویندوز: تنظیمات → زمان → «همگام‌سازی اکنون». ربات تا ۵ ثانیه اختلاف را خودش جبران می‌کند."
          />
          <Trouble
            symptom="«استریم لحظه‌ای حساب» خاموش می‌ماند (بایننس)"
            fix="بعضی شبکه‌ها وب‌سوکت را می‌بندند. سیستم با REST هر ۲ دقیقه سینک می‌کند پس معامله متوقف نمی‌شود، فقط تأخیر گزارش بیشتر است. اگر پروکسی داری، EXCHANGE_HTTP_PROXY را تنظیم کن."
          />
          <Trouble
            symptom="سیگنال هست ولی «rejected: notional below min»"
            fix="حجم محاسبه‌شده از حداقل سفارش صرافی کمتر است. یا سرمایه را بیشتر کن یا در تنظیمات «ریسک هر معامله» را کمی بالا ببر."
          />
          <Trouble symptom="«live trading not armed» در سیگنال‌ها" fix="طبیعی است — هنوز مسلح نکرده‌ای. بخش ۸." />
          <Trouble
            symptom="پوزیشنی روی صرافی هست که در پنل نیست"
            fix="اگر خودت دستی خریده‌ای، ربات آن را «مدیریت‌نشده» گزارش می‌کند و به آن دست نمی‌زند. یا خودت بفروش، یا بگذار همان‌جا بماند."
          />
          <p className="text-sm text-slate-300 leading-7 mt-4">لاگ کامل هر سرویس:</p>
          <CodeBlock>{`docker compose logs -f engine       # حلقه معامله
docker compose logs -f scheduler    # سینک صرافی، هوش مصنوعی، مدل
docker compose logs -f marketdata   # دریافت کندل`}</CodeBlock>
        </Section>

        <Section id="security" title="امنیت — چیزهایی که هرگز نباید انجام بدهی" number={11}>
          <Check bad>
            هرگز مجوز <b>برداشت</b> (Withdraw) را روی کلید ربات روشن نکن.
          </Check>
          <Check bad>
            هرگز فایل <Code>.env</Code> را برای کسی نفرست، در گیت commit نکن، یا در تلگرام کپی نکن.
          </Check>
          <Check bad>
            هرگز پنل را بدون تغییر <Code>PANEL_PASSWORD</Code> روی اینترنت باز نکن.
          </Check>
          <Check bad>هرگز کلید یک صرافی را در چند ربات مختلف استفاده نکن — برای هر کدام کلید جدا بساز.</Check>
          <Check>
            در صفحه <b>امنیت</b> پنل، تایید دومرحله‌ای را فعال کن تا ورود فقط با رمز ممکن نباشد.
          </Check>
          <Check>
            هر چند ماه یک بار کلید را در صرافی حذف و کلید تازه بساز. اگر روزی مشکوک شدی، همان لحظه کلید را در صرافی
            حذف کن — سریع‌تر از هر کار دیگری است.
          </Check>
          <Check>
            برای دسترسی از بیرون خانه به پنل، از VPN یا Tailscale استفاده کن، نه باز کردن مستقیم پورت روی مودم. اگر روی
            VPS با دامنه راه انداختی، Caddy خودش HTTPS می‌گیرد.
          </Check>
        </Section>
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- pieces */

function Section({ id, title, number, children }: { id: string; title: string; number: number; children: React.ReactNode }) {
  return (
    <Card>
      <div id={`guide-${id}`} className="scroll-mt-28 lg:scroll-mt-6" />
      <h2 className="text-base font-semibold text-slate-50 mb-4 flex items-center gap-3">
        <span className="w-8 h-8 rounded-lg bg-accent/15 text-accent text-sm flex items-center justify-center num shrink-0 font-bold">
          {number}
        </span>
        {title}
      </h2>
      <div className="space-y-3">{children}</div>
    </Card>
  )
}

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 text-sm text-slate-200 leading-7">
      <span className="num text-slate-400 shrink-0 w-5 pt-0.5">{n}.</span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

function Check({ children, bad = false }: { children: React.ReactNode; bad?: boolean }) {
  return (
    <div className="flex gap-3 text-sm text-slate-200 leading-7">
      <span className={clsx('shrink-0 mt-1.5 w-4 h-4', bad ? 'text-loss' : 'text-profit')} aria-hidden="true">
        {bad ? <CloseIcon className="w-4 h-4" strokeWidth={2.4} /> : <CheckIcon className="w-4 h-4" strokeWidth={2.4} />}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

function Note({ tone, children }: { tone: 'info' | 'warn'; children: React.ReactNode }) {
  return (
    <div
      className={clsx(
        'rounded-xl px-4 py-3 text-sm leading-7 border',
        tone === 'warn' ? 'bg-warn/10 border-warn/30 text-warn' : 'bg-accent/10 border-accent/25 text-slate-200',
      )}
    >
      {children}
    </div>
  )
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="num bg-ink-800 border border-ink-700 rounded-md px-1.5 py-0.5 text-[12px] text-accent break-all">
      {children}
    </code>
  )
}

function CodeBlock({ children }: { children: React.ReactNode }) {
  return (
    <pre
      dir="ltr"
      className="num bg-ink-950 border border-ink-700 rounded-xl px-4 py-3 text-[12px] text-slate-200 overflow-x-auto leading-6 my-2"
    >
      {children}
    </pre>
  )
}

function PermRow({ name, on, reason }: { name: string; on: boolean | null; reason: string }) {
  return (
    <div className="px-3.5 py-3 flex items-start gap-3">
      <div className="grow min-w-0">
        <div className="num text-xs text-slate-50 font-medium" dir="ltr">
          {name}
        </div>
        <div className="text-xs text-slate-400 leading-5 mt-0.5">{reason}</div>
      </div>
      <span className="shrink-0 mt-0.5">
        {on === true && <span className="badge bg-profit/15 text-profit">روشن</span>}
        {on === false && <span className="badge bg-loss/15 text-loss">خاموش</span>}
        {on === null && <span className="badge bg-warn/15 text-warn">اختیاری</span>}
      </span>
    </div>
  )
}

function Trouble({ symptom, fix }: { symptom: string; fix: string }) {
  return (
    <div className="card-raised px-4 py-3">
      <div className="text-sm text-slate-50 mb-1 leading-6">{symptom}</div>
      <div className="text-xs text-slate-300 leading-6">{fix}</div>
    </div>
  )
}
