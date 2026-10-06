import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { api, NotifyCategory, NotifyPrefs, NotifyTestResult, QuietHours } from '@/lib/api'
import * as push from '@/lib/push'
import { datetime, timeAgo } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import { Badge, Card, ErrorBanner, Notice, Row, Skeleton, Spinner, Toggle, toast } from '@/components/ui'
import { BellIcon } from '@/components/icons'

const CATEGORIES: Array<{ key: NotifyCategory; label: string; hint: string }> = [
  { key: 'trade', label: 'معاملات', hint: 'باز و بسته شدن پوزیشن با سود و ضرر، سفارش‌های ردشده' },
  { key: 'risk', label: 'ریسک', hint: 'حد ضرر روزانه، توقف اضطراری، رسیدن به حد افت کل' },
  { key: 'control', label: 'کنترل', hint: 'مسلح و غیرمسلح شدن، تغییر تنظیمات، ثبت واریز و برداشت' },
  { key: 'exchange', label: 'صرافی', hint: 'قطع و وصل صرافی، خطای کلید یا IP، اختلاف در همگام‌سازی، بسته شدن خارج از ربات' },
  { key: 'system', label: 'سیستم', hint: 'روشن و خاموش شدن سرویس‌ها، تغییر IP، ساعت سیستم، ورود به پنل' },
  { key: 'ai', label: 'هوش مصنوعی', hint: 'تغییر دیدگاه بازار کلود و وتوی معامله' },
]

type Device = {
  supported: boolean
  permission: push.PermissionState
  subscribed: boolean
  busy: boolean
  iosInstall: boolean
}

export default function Notifications() {
  const status = usePolling(() => api.notifyStatus(), 15000)
  const [prefs, setPrefs] = useState<NotifyPrefs | null>(null)
  const [prefsError, setPrefsError] = useState<string | null>(null)
  const [device, setDevice] = useState<Device>(() => ({
    supported: push.isSupported(),
    permission: push.permissionState(),
    subscribed: false,
    busy: false,
    iosInstall: push.iosNeedsInstall(),
  }))
  const [testing, setTesting] = useState(false)
  const [lastTest, setLastTest] = useState<NotifyTestResult | null>(null)

  const refreshDevice = useCallback(async () => {
    const subscription = await push.currentSubscription()
    setDevice((current) => ({
      ...current,
      permission: push.permissionState(),
      subscribed: Boolean(subscription),
    }))
  }, [])

  useEffect(() => {
    refreshDevice()
  }, [refreshDevice])

  useEffect(() => {
    let cancelled = false
    api
      .notifyPreferences()
      .then((result) => {
        if (!cancelled) setPrefs(result)
      })
      .catch((err) => {
        if (!cancelled) setPrefsError(err instanceof Error ? err.message : 'خطا')
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function toggleDevice(on: boolean) {
    setDevice((current) => ({ ...current, busy: true }))
    try {
      if (on) {
        await push.subscribe()
        toast('اعلان روی این دستگاه فعال شد')
      } else {
        await push.unsubscribe()
        toast('اعلان این دستگاه خاموش شد')
      }
      status.refresh()
    } catch (err) {
      toast(err instanceof Error ? err.message : 'خطا', 'error')
    } finally {
      await refreshDevice()
      setDevice((current) => ({ ...current, busy: false }))
    }
  }

  async function patch(update: Parameters<typeof api.notifyPatchPreferences>[0], optimistic: NotifyPrefs) {
    const previous = prefs
    setPrefs(optimistic)
    try {
      setPrefs(await api.notifyPatchPreferences(update))
    } catch (err) {
      setPrefs(previous)
      toast(err instanceof Error ? err.message : 'ذخیره نشد', 'error')
    }
  }

  function toggleCategory(key: NotifyCategory, value: boolean) {
    if (!prefs) return
    patch({ categories: { [key]: value } }, { ...prefs, categories: { ...prefs.categories, [key]: value } })
  }

  function saveQuiet(quiet: QuietHours) {
    if (!prefs) return
    patch({ quiet_hours: quiet }, { ...prefs, quiet_hours: quiet })
  }

  async function sendTest() {
    setTesting(true)
    try {
      const result = await api.notifyTest()
      setLastTest(result)
      const ok = Object.values(result).some((value) => value.startsWith('sent'))
      toast(ok ? 'اعلان آزمایشی فرستاده شد' : 'هیچ کانالی آماده نبود', ok ? 'ok' : 'error')
    } catch (err) {
      toast(err instanceof Error ? err.message : 'خطا', 'error')
    } finally {
      setTesting(false)
    }
  }

  const telegram = status.data?.telegram
  const quiet = prefs?.quiet_hours

  return (
    <div className="space-y-4 max-w-2xl mx-auto">
      <ErrorBanner message={status.error} />
      {prefsError && <Notice tone="danger">{prefsError}</Notice>}

      {/* ---- this device ---- */}
      <Card>
        <div className="flex items-start gap-3">
          <div
            className={clsx(
              'w-11 h-11 rounded-xl flex items-center justify-center shrink-0',
              device.subscribed ? 'bg-profit/15 text-profit' : 'bg-ink-850 border border-ink-700/70 text-slate-300',
            )}
          >
            <BellIcon className="w-6 h-6" />
          </div>
          <div className="grow min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-base font-semibold text-slate-50">اعلان روی این دستگاه</h2>
              <DeviceBadge device={device} />
            </div>
            <p className="text-xs text-slate-400 leading-6 mt-1">
              هر کاری ربات بکند — باز و بسته شدن پوزیشن، توقف اضطراری، قطع شدن صرافی، عوض شدن IP — همان لحظه
              روی این گوشی می‌آید، حتی وقتی اپ بسته است.
            </p>
          </div>
        </div>

        <div className="mt-3">
          <Toggle
            checked={device.subscribed}
            onChange={toggleDevice}
            disabled={device.busy || !device.supported || device.iosInstall || device.permission === 'denied'}
            label={
              <span className="flex items-center gap-2">
                {device.subscribed ? 'روشن' : 'خاموش'}
                {device.busy && <Spinner className="w-4 h-4" />}
              </span>
            }
            hint={
              status.data
                ? `${status.data.push.devices} دستگاه ثبت شده`
                : undefined
            }
          />
        </div>

        {!device.supported && (
          <Notice tone="warn" className="mt-2">
            این مرورگر از اعلان پوش پشتیبانی نمی‌کند. کروم یا فایرفاکس روی اندروید، یا سافاری ۱۶.۴ به بالا روی آیفون را امتحان کن.
          </Notice>
        )}
        {device.supported && device.iosInstall && (
          <Notice tone="info" className="mt-2">
            روی آیفون و آی‌پد، اعلان فقط برای اپ نصب‌شده کار می‌کند (iOS 16.4 به بالا): در سافاری دکمه Share را بزن،
            «Add to Home Screen» را انتخاب کن، بعد اپ را از صفحه اصلی باز کن و این کلید را روشن کن.
          </Notice>
        )}
        {device.supported && !device.iosInstall && device.permission === 'denied' && (
          <Notice tone="danger" className="mt-2">
            اجازه اعلان برای این سایت رد شده است. در تنظیمات مرورگر (یا تنظیمات اعلان‌های سیستم برای اپ) روشنش کن و
            دوباره امتحان کن.
          </Notice>
        )}
        {status.data && !status.data.push.vapid_ready && (
          <Notice tone="warn" className="mt-2">
            کلید امضای پوش هنوز ساخته نشده است؛ سرویس api باید یک بار با نسخه جدید بالا بیاید.
          </Notice>
        )}
      </Card>

      {/* ---- categories ---- */}
      <Card title="چه چیزهایی اعلان بشود">
        {prefs ? (
          <div className="divide-y divide-ink-800">
            {CATEGORIES.map((category) => (
              <Toggle
                key={category.key}
                checked={prefs.categories[category.key] ?? true}
                onChange={(value) => toggleCategory(category.key, value)}
                label={category.label}
                hint={category.hint}
              />
            ))}
          </div>
        ) : (
          <div className="space-y-3">
            {CATEGORIES.map((category) => (
              <Skeleton key={category.key} className="h-12" />
            ))}
          </div>
        )}
        <p className="text-2xs text-slate-400 leading-5 mt-3">
          این انتخاب‌ها برای همه کانال‌ها (این دستگاه، بقیه دستگاه‌ها، تلگرام) یکی است. هر رویداد، روشن یا خاموش،
          در صفحه رویدادهای داشبورد ثبت می‌شود.
        </p>
      </Card>

      {/* ---- quiet hours ---- */}
      <Card title="ساعات سکوت">
        <Toggle
          checked={quiet?.enabled ?? false}
          disabled={!quiet}
          onChange={(enabled) => quiet && saveQuiet({ ...quiet, enabled })}
          label="شب‌ها بی‌صدا"
          hint="در این بازه اعلان‌ها بی‌صدا می‌رسند تا گوشی نلرزد؛ فقط خطاهای بحرانی (توقف اضطراری، قطع کلید، تغییر IP) با صدا می‌آیند."
        />
        {quiet?.enabled && (
          <div className="grid grid-cols-2 gap-3 mt-2">
            <label>
              <span className="label">از ساعت</span>
              <input
                type="time"
                className="input num"
                value={quiet.start}
                onChange={(event) => saveQuiet({ ...quiet, start: event.target.value || quiet.start })}
              />
            </label>
            <label>
              <span className="label">تا ساعت</span>
              <input
                type="time"
                className="input num"
                value={quiet.end}
                onChange={(event) => saveQuiet({ ...quiet, end: event.target.value || quiet.end })}
              />
            </label>
            <p className="col-span-2 text-2xs text-slate-400 leading-5">
              به وقت <span className="num">{status.data?.tz ?? 'سرور'}</span>.
            </p>
          </div>
        )}
      </Card>

      {/* ---- telegram ---- */}
      <Card
        title="تلگرام"
        action={
          telegram ? (
            !telegram.configured ? (
              <Badge tone="warn">پیکربندی نشده</Badge>
            ) : telegram.chat_id ? (
              <Badge tone="profit">متصل</Badge>
            ) : (
              <Badge tone="accent">در انتظار /start</Badge>
            )
          ) : (
            <Skeleton className="h-5 w-16" />
          )
        }
      >
        {telegram && !telegram.configured && (
          <ol className="text-sm text-slate-300 leading-7 space-y-1 list-decimal ps-5">
            <li>
              در تلگرام به <span className="num">@BotFather</span> پیام بده، <span className="num">/newbot</span> بزن و یک نام
              انتخاب کن. یک توکن می‌دهد.
            </li>
            <li>
              توکن را در فایل <span className="num">.env</span> جلوی <span className="num">TELEGRAM_BOT_TOKEN=</span> بگذار و
              سرویس‌ها را دوباره راه‌اندازی کن.
            </li>
            <li>
              به ربات خودت <span className="num">/start</span> بفرست. شناسه چت خودکار پیدا می‌شود و ربات جواب «وصل شد» می‌دهد.
            </li>
          </ol>
        )}
        {telegram && telegram.configured && !telegram.chat_id && (
          <p className="text-sm text-slate-300 leading-7">
            توکن ثبت شده است. حالا در تلگرام ربات خودت را باز کن و <span className="num">/start</span> بفرست؛ تا یک دقیقه بعد
            وصل می‌شود و پیام «✅ Gisoo Trader وصل شد» می‌فرستد.
          </p>
        )}
        {telegram && telegram.chat_id && (
          <dl className="text-sm">
            <Row label="چت" value={<span className="num">{telegram.chat_title || telegram.chat_id}</span>} />
            <Row label="شناسه" value={<span className="num">{telegram.chat_id}</span>} />
            <Row label="از کجا" value={telegram.source === 'env' ? 'فایل .env' : 'خودکار (با /start)'} />
          </dl>
        )}
        <p className="text-2xs text-slate-400 leading-5 mt-3">
          تلگرام از پوش مطمئن‌تر است: به نصب اپ و مرورگر وابسته نیست و تاریخچه‌اش می‌ماند. هر دو را روشن نگه دار.
        </p>
      </Card>

      {/* ---- public ip ---- */}
      <Card title="IP عمومی این سیستم">
        <dl className="text-sm">
          <Row
            label="IP فعلی"
            value={
              status.data ? (
                <span className="num font-semibold" dir="ltr">
                  {status.data.public_ip ?? 'هنوز بررسی نشده'}
                </span>
              ) : (
                <Skeleton className="h-4 w-28" />
              )
            }
          />
          <Row
            label="آخرین بررسی"
            value={<span title={datetime(status.data?.public_ip_seen_at)}>{timeAgo(status.data?.public_ip_seen_at)}</span>}
          />
          {status.data?.public_ip_changed_at && (
            <Row
              label="آخرین تغییر"
              value={
                <span>
                  {datetime(status.data.public_ip_changed_at)}
                  {status.data.public_ip_previous && (
                    <span className="text-slate-400 text-2xs">
                      {' '}
                      (قبلاً <span className="num" dir="ltr">{status.data.public_ip_previous}</span>)
                    </span>
                  )}
                </span>
              }
            />
          )}
        </dl>
        <p className="text-2xs text-slate-400 leading-5 mt-3">
          هر ۵ دقیقه بررسی می‌شود. کلید صرافی اگر به IP محدود شده باشد فقط از همین IP کار می‌کند؛ اگر عوض شود همان لحظه اعلان خطا می‌گیری و باید IP جدید
          را در صرافی به لیست مجاز کلید اضافه کنی —{' '}
          <Link to="/guide#restrict" className="text-accent">
            قدم‌های راهنما، بخش ۳
          </Link>
          .
        </p>
      </Card>

      {/* ---- test ---- */}
      <Card>
        <button className="btn-primary w-full min-h-[48px]" onClick={sendTest} disabled={testing}>
          {testing ? <Spinner className="w-5 h-5 border-ink-950/40 border-t-ink-950" /> : 'ارسال اعلان آزمایشی'}
        </button>
        {lastTest && (
          <dl className="text-sm mt-3">
            {Object.entries(lastTest).map(([channel, result]) => (
              <Row
                key={channel}
                label={channel === 'telegram' ? 'تلگرام' : channel === 'push' ? 'پوش' : channel}
                value={<TestResult value={result} />}
              />
            ))}
          </dl>
        )}
        <p className="text-2xs text-slate-400 leading-5 mt-3">
          به همه کانال‌ها می‌فرستد، فارغ از دسته‌بندی و ساعات سکوت.
        </p>
      </Card>
    </div>
  )
}

function DeviceBadge({ device }: { device: Device }) {
  if (!device.supported) return <Badge tone="neutral">پشتیبانی نمی‌شود</Badge>
  if (device.subscribed) return <Badge tone="profit">فعال</Badge>
  if (device.permission === 'denied') return <Badge tone="loss">اجازه رد شده</Badge>
  if (device.iosInstall) return <Badge tone="warn">نیاز به نصب</Badge>
  return <Badge tone="warn">غیرفعال</Badge>
}

function TestResult({ value }: { value: string }) {
  const fa = value.startsWith('sent')
    ? `فرستاده شد (${value.replace(/^sent\s*/, '') || 'ok'})`
    : value === 'skipped: no token'
      ? 'توکن تلگرام تنظیم نشده'
      : value === 'skipped: waiting for /start'
        ? 'هنوز /start نفرستاده‌ای'
        : value === 'skipped: no subscriptions'
          ? 'هیچ دستگاهی ثبت نشده'
          : value === 'timeout'
            ? 'جواب نداد (تایم‌اوت)'
            : value
  const ok = value.startsWith('sent')
  return <span className={clsx(ok ? 'text-profit' : 'text-warn')}>{fa}</span>
}
