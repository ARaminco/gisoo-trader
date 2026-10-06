import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { api, CoreFamily, StaticSettings } from '@/lib/api'
import { CORE_FA, FAMILY_FA, MODE_FA } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import { Badge, Card, ConfirmDialog, ErrorBanner, Notice, Skeleton, Spinner, Toggle, toast } from '@/components/ui'
import { ChevronIcon, ShieldIcon } from '@/components/icons'

type Fields = Record<string, any>

type Field = {
  key: string
  label: string
  hint?: string
  type: 'number' | 'bool' | 'text'
  step?: number
  min?: number
  max?: number
  unit?: string
  /** Read by the ensemble30m core only; shown greyed out under the 4h core. */
  ensembleOnly?: boolean
}

const GROUPS: Array<{
  title: string
  hint?: string
  fields: Field[]
}> = [
  {
    title: 'مدیریت ریسک',
    hint: 'این بخش مهم‌ترین محافظ سرمایه است. با احتیاط تغییر بده.',
    fields: [
      { key: 'risk_per_trade_pct', label: 'ریسک هر معامله', unit: '٪ سرمایه', type: 'number', step: 0.1, min: 0.1, max: 10, hint: 'پولی که در بدترین حالت در یک معامله از دست می‌دهی', ensembleOnly: true },
      { key: 'max_concurrent_positions', label: 'حداکثر پوزیشن هم‌زمان', type: 'number', step: 1, min: 1, max: 10, hint: 'برای هسته ۴ساعته ۴ پیشنهاد می‌شود: سه نام مومنتوم به‌علاوه یک شکست کانال' },
      { key: 'max_position_pct', label: 'سقف حجم یک پوزیشن', unit: '٪ سرمایه', type: 'number', step: 1, min: 5, max: 100, ensembleOnly: true },
      { key: 'max_daily_loss_pct', label: 'حد ضرر روزانه', unit: '٪', type: 'number', step: 0.5, min: 1, max: 50, hint: 'با رسیدن به این عدد، معامله تا فردا متوقف می‌شود' },
      { key: 'max_drawdown_pct', label: 'حد افت کل', unit: '٪', type: 'number', step: 1, min: 2, max: 90, hint: 'با رسیدن به این عدد، توقف اضطراری خودکار فعال می‌شود. در هسته ۴ساعته روی سرمایه بسته‌شده (بدون سود باز) اندازه‌گیری می‌شود' },
      { key: 'core4h_risk_scale', label: 'ضریب احتیاط (اندازه‌ی پوزیشن)', unit: '×', type: 'number', step: 0.05, min: 0.25, max: 1.5, hint: '۱ = اندازه‌ی تأییدشده در تست. ۰.۶۷ = محافظه‌کارانه: بدترین بازه −۳.۷٪ و افت ۱۱٪ به‌جای −۵.۷٪ و ۱۶٪، با سود میانگین کمتر' },
      { key: 'cooldown_minutes_after_loss', label: 'استراحت بعد از ضرر', unit: 'دقیقه', type: 'number', step: 5, min: 0, max: 1440, ensembleOnly: true },
    ],
  },
  {
    title: 'ورود و خروج',
    fields: [
      { key: 'min_signal_score', label: 'حداقل امتیاز سیگنال', type: 'number', step: 0.01, min: 0.3, max: 0.95, hint: 'بالاتر یعنی معاملات کمتر ولی مطمئن‌تر', ensembleOnly: true },
      { key: 'min_expected_move_pct', label: 'حداقل حرکت مورد انتظار', unit: '٪', type: 'number', step: 0.1, min: 0.1, max: 10, hint: 'زیر این مقدار، کارمزد سود را می‌خورد', ensembleOnly: true },
      { key: 'atr_stop_mult', label: 'ضریب ATR برای حد ضرر', type: 'number', step: 0.1, min: 0.5, max: 6, ensembleOnly: true },
      { key: 'atr_target_mult', label: 'ضریب ATR برای حد سود', type: 'number', step: 0.1, min: 0.5, max: 12, ensembleOnly: true },
      { key: 'trailing_enabled', label: 'حد ضرر متحرک (تریلینگ)', type: 'bool', hint: 'با حرکت قیمت به نفع معامله، حد ضرر هم جلو می‌آید', ensembleOnly: true },
      { key: 'min_atr_ratio', label: 'حداقل نسبت نوسان (ATR)', type: 'number', step: 0.1, min: 0, max: 5, hint: 'فقط وقتی نوسان در حال گسترش است وارد می‌شود؛ در بازار آرام سیگنال‌ها ضررده بودند. پیشنهاد: ۱.۵', ensembleOnly: true },
      { key: 'skip_weekend_entries', label: 'بدون ورود در آخر هفته', type: 'bool', hint: 'ورودهای شنبه و یکشنبه (UTC) در همه‌ی بازه‌های تاریخی ضررده بودند' },
    ],
  },
  {
    title: 'لایه‌های هوشمند',
    fields: [
      { key: 'ml_enabled', label: 'مدل یادگیری ماشین', type: 'bool', hint: 'سیگنال‌ها را با احتمال موفقیت فیلتر می‌کند', ensembleOnly: true },
      { key: 'ml_min_probability', label: 'حداقل احتمال مدل', type: 'number', step: 0.01, min: 0.4, max: 0.9, ensembleOnly: true },
      { key: 'llm_enabled', label: 'تحلیل هوش مصنوعی کلود', type: 'bool', hint: 'بازبینی دوره‌ای بازار و اخبار' },
      { key: 'llm_can_veto', label: 'اجازه وتوی معامله به کلود', type: 'bool' },
      { key: 'llm_review_minutes', label: 'فاصله بازبینی بازار', unit: 'دقیقه', type: 'number', step: 5, min: 5, max: 720 },
    ],
  },
  {
    title: 'فیوچرز',
    hint: 'با اهرم، احتمال لیکویید شدن واقعی است. فقط وقتی فعال کن که آمار سوددهی اسپات را دیده باشی.',
    fields: [
      { key: 'futures_enabled', label: 'معامله فیوچرز و فروش استقراضی', type: 'bool' },
      { key: 'futures_leverage', label: 'اهرم', unit: '×', type: 'number', step: 1, min: 1, max: 10 },
    ],
  },
]

export default function Settings() {
  const settings = usePolling(() => api.settings(), 30000)
  const overview = usePolling(() => api.overview(), 8000)

  const [draft, setDraft] = useState<Fields>({})
  const [symbols, setSymbols] = useState('')
  const [saving, setSaving] = useState(false)
  const [armDialog, setArmDialog] = useState(false)

  useEffect(() => {
    if (settings.data && Object.keys(draft).length === 0) {
      setDraft({ ...settings.data.effective })
      setSymbols((settings.data.effective.symbols ?? []).join(', '))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.data])

  const dirty =
    settings.data &&
    (Object.keys(draft).some(
      (key) => key !== 'symbols' && JSON.stringify(draft[key]) !== JSON.stringify(settings.data!.effective[key]),
    ) ||
      symbols !== (settings.data.effective.symbols ?? []).join(', '))

  const parsedSymbols = useMemo(
    () =>
      symbols
        .split(/[,\s]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    [symbols],
  )

  async function save() {
    setSaving(true)
    try {
      const patch: Fields = {}
      for (const [key, value] of Object.entries(draft)) {
        if (key === 'symbols') continue
        if (JSON.stringify(value) !== JSON.stringify(settings.data?.effective[key])) {
          patch[key] = value
        }
      }
      if (parsedSymbols.join(',') !== (settings.data?.effective.symbols ?? []).join(',')) {
        patch.symbols = parsedSymbols
      }

      if (Object.keys(patch).length === 0) {
        toast('تغییری برای ذخیره نیست')
        return
      }
      await api.patchSettings(patch)
      toast('تنظیمات ذخیره شد')
      settings.refresh()
    } catch (error) {
      toast(error instanceof Error ? error.message : 'خطا', 'error')
    } finally {
      setSaving(false)
    }
  }

  function reset() {
    setDraft({ ...settings.data!.effective })
    setSymbols((settings.data!.effective.symbols ?? []).join(', '))
  }

  async function toggleArm(armed: boolean) {
    setArmDialog(false)
    try {
      await api.arm(armed, armed ? 'ARM LIVE' : undefined)
      toast(armed ? 'ربات مسلح شد و شروع به معامله می‌کند' : 'ربات غیرمسلح شد')
      overview.refresh()
    } catch (error) {
      toast(error instanceof Error ? error.message : 'خطا', 'error')
    }
  }

  async function resume() {
    try {
      await api.resume()
      toast('توقف برداشته شد')
      overview.refresh()
    } catch (error) {
      toast(error instanceof Error ? error.message : 'خطا', 'error')
    }
  }

  if (settings.loading && !settings.data) return <SettingsSkeleton />

  const staticInfo: Partial<StaticSettings> = settings.data?.static ?? {}
  const live = staticInfo.mode === 'live'
  const core = staticInfo.strategy_core ?? 'portfolio4h'
  const core4h = core === 'portfolio4h'
  const families: CoreFamily[] = staticInfo.core_families ?? []

  return (
    <div className={clsx('space-y-4 max-w-3xl mx-auto', dirty && 'pb-24')}>
      <ErrorBanner message={settings.error} />

      {/* ---- arming ---- */}
      <Card title="کنترل اصلی">
        <div className="flex items-center gap-2 flex-wrap mb-2">
          {overview.data?.kill_switch ? (
            <Badge tone="loss">توقف اضطراری فعال</Badge>
          ) : overview.data?.paused_until ? (
            <Badge tone="warn">موقتاً متوقف</Badge>
          ) : overview.data?.armed ? (
            <Badge tone="profit">مسلح — در حال معامله</Badge>
          ) : (
            <Badge tone="warn">غیرمسلح</Badge>
          )}
          <Badge tone={live ? 'loss' : 'neutral'}>{MODE_FA[staticInfo.mode ?? ''] ?? staticInfo.mode}</Badge>
          <Badge tone="accent">{CORE_FA[core] ?? core}</Badge>
        </div>
        <p className="text-xs text-slate-400 leading-6 mb-4">
          {overview.data?.armed
            ? live
              ? 'ربات فعال است و با پول واقعی معامله می‌کند.'
              : 'ربات فعال است و با پول فرضی معامله می‌کند.'
            : 'ربات سیگنال می‌سازد ولی هیچ سفارشی نمی‌فرستد.'}
        </p>

        <div className="grid sm:grid-cols-2 gap-2">
          {overview.data?.armed ? (
            <button className="btn-ghost" onClick={() => toggleArm(false)}>
              غیرمسلح کن
            </button>
          ) : (
            <button className="btn-success" onClick={() => setArmDialog(true)}>
              مسلح کن
            </button>
          )}
          {(overview.data?.kill_switch || overview.data?.paused_until) && (
            <button className="btn-ghost" onClick={resume}>
              رفع توقف
            </button>
          )}
        </div>
      </Card>

      {/* ---- strategy core (read-only: an .env change plus a restart) ---- */}
      <Card title="هسته استراتژی">
        <div className="flex items-center gap-2 flex-wrap mb-3">
          <Badge tone="accent">{CORE_FA[core] ?? core}</Badge>
          <span className="text-2xs text-slate-400">
            تایم‌فریم تصمیم <span className="num">{staticInfo.timeframe}</span>
            {staticInfo.universe && (
              <>
                <span className="mx-1">·</span>
                <span className="num">{staticInfo.universe.length}</span> نماد در رتبه‌بندی
              </>
            )}
          </span>
        </div>
        {core4h ? (
          <>
            <p className="text-xs text-slate-400 leading-6 mb-3">
              دو خانواده استراتژی ۴ساعته با دروازه رژیم بیت‌کوین (بسته بالای EMA200). قوانین و اندازه‌گیری هر خانواده
              در مطالعه ۱۲ بازه‌ای ۲۰۲۱–۲۰۲۶ اعتبارسنجی شده و از پنل قابل تغییر نیست.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full sm:min-w-[420px] [&_.th]:px-2 [&_.td]:px-2 [&_.th]:whitespace-normal [&_.td]:whitespace-normal [&_.td.num]:whitespace-nowrap sm:[&_.th]:px-3 sm:[&_.td]:px-3">
                <thead>
                  <tr className="border-b border-ink-700">
                    <th className="th ps-0">خانواده</th>
                    <th className="th">ریسک</th>
                    <th className="th">سقف پوزیشن</th>
                    <th className="th">حد ضرر</th>
                    <th className="th hidden sm:table-cell">استراحت</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-800">
                  {families.map((family) => (
                    <tr key={family.name}>
                      <td className="td ps-0">
                        <span className="text-slate-100">{FAMILY_FA[family.name] ?? family.name}</span>
                        <span className="block text-2xs text-slate-400 num">{family.research_name}</span>
                      </td>
                      <td className="td num text-slate-300">{family.risk_pct}%</td>
                      <td className="td num text-slate-300">{family.max_position_pct}%</td>
                      <td className="td num text-slate-300">{family.atr_stop} ATR</td>
                      <td className="td num text-slate-300 hidden sm:table-cell">{family.cooldown_min > 0 ? `${family.cooldown_min} دقیقه` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="text-xs text-slate-400 leading-6">
            انسمبل شش استراتژی روی تایم‌فریم <span className="num">{staticInfo.timeframe}</span> با فیلتر روند{' '}
            <span className="num">{staticInfo.htf_timeframe}</span>. در مطالعه ۱۲ بازه‌ای برتری پایداری نداشت؛ برای
            مقایسه نگه داشته شده است.
          </p>
        )}
        <p className="text-2xs text-slate-400 mt-3 leading-5">
          انتخاب هسته با <span className="num">STRATEGY_CORE</span> در فایل <span className="num">.env</span> انجام می‌شود و
          نیاز به راه‌اندازی دوباره دارد.
        </p>
      </Card>

      {/* ---- editable groups ---- */}
      {GROUPS.map((group) => {
        const activeFields = core4h ? group.fields.filter((field) => !field.ensembleOnly) : group.fields
        const inactiveFields = core4h ? group.fields.filter((field) => field.ensembleOnly) : []
        return (
          <Card key={group.title} title={group.title}>
            {group.hint && (
              <Notice tone="warn" className="mb-4">
                {group.hint}
              </Notice>
            )}
            {activeFields.length === 0 && (
              <p className="text-xs text-slate-400 leading-6 mb-3">
                هیچ‌کدام از این تنظیمات در هسته ۴ساعته خوانده نمی‌شود.
              </p>
            )}
            <div className="grid sm:grid-cols-2 gap-x-6 gap-y-3">
              {activeFields.map((field) => (
                <FieldInput key={field.key} field={field} draft={draft} setDraft={setDraft} />
              ))}
            </div>
            {inactiveFields.length > 0 && (
              <details className="mt-4">
                <summary className="text-2xs text-slate-400 cursor-pointer select-none min-h-[36px] flex items-center">
                  تنظیمات مخصوص انسمبل ۳۰ دقیقه ({inactiveFields.length}) — در این هسته بی‌اثر
                </summary>
                <div className="grid sm:grid-cols-2 gap-x-6 gap-y-3 mt-3 opacity-50 pointer-events-none" aria-disabled="true">
                  {inactiveFields.map((field) => (
                    <FieldInput key={field.key} field={field} draft={draft} setDraft={setDraft} disabled />
                  ))}
                </div>
              </details>
            )}
          </Card>
        )
      })}

      <Card title="نمادهای قابل معامله">
        <label className="label" htmlFor="setting-symbols">
          با کاما یا فاصله جدا کن
        </label>
        <input
          id="setting-symbols"
          className="input num"
          dir="ltr"
          value={symbols}
          onChange={(event) => setSymbols(event.target.value)}
          placeholder="BTCUSDT, ETHUSDT, SOLUSDT"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck={false}
        />
        {parsedSymbols.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2.5" dir="ltr">
            {parsedSymbols.map((item) => (
              <Badge key={item} tone="accent" className="num">
                {item}
              </Badge>
            ))}
          </div>
        )}
        <p className="text-2xs text-slate-400 mt-3 leading-5">
          فقط جفت‌های موجود در صرافی با کوت <span className="num">{staticInfo.quote_asset}</span>. تغییر لیست بعد از
          راه‌اندازی دوباره سرویس‌ها اعمال می‌شود چون تاریخچه کندل باید دانلود شود.
        </p>
      </Card>

      <Card title="اطلاعات ثابت سیستم">
        <dl className="grid sm:grid-cols-2 gap-x-8">
          <Info
            label="صرافی"
            value={
              staticInfo.exchange
                ? `${staticInfo.exchange.title_fa}${staticInfo.exchange.status === 'beta' ? ' (آزمایشی)' : ''}`
                : undefined
            }
          />
          <Info
            label="حد ضرر"
            value={
              staticInfo.exchange
                ? staticInfo.exchange.stop_orders
                  ? 'سفارش استاپ روی صرافی'
                  : 'توسط خود ربات (ربات باید روشن بماند)'
                : undefined
            }
          />
          <Info label="ارز پایه (کوت)" value={staticInfo.quote_asset === 'IRT' ? 'تومان (IRT)' : staticInfo.quote_asset} />
          <Info label="هسته استراتژی" value={CORE_FA[core] ?? core} />
          <Info label="تایم‌فریم تصمیم" value={staticInfo.timeframe} />
          {!core4h && <Info label="تایم‌فریم روند" value={staticInfo.htf_timeframe ?? undefined} />}
          <Info label="کارمزد هر سمت" value={`${staticInfo.taker_fee_pct}%`} />
          <Info label="کارمزد رفت‌وبرگشت" value={`${staticInfo.round_trip_fee_pct}%`} />
          <Info label="مدل هوش مصنوعی" value={staticInfo.llm_model} />
          <Info label="تعداد کندل ذخیره‌شده" value={String(staticInfo.candle_history ?? '—')} />
        </dl>
        <p className="text-2xs text-slate-400 mt-4 leading-5">
          این مقادیر در فایل <span className="num">.env</span> تعریف می‌شوند و برای تغییرشان باید سرویس‌ها را دوباره
          راه‌اندازی کنی.
        </p>
      </Card>

      <Link
        to="/security"
        className="card p-4 flex items-center gap-3 active:bg-ink-800/70 hover:bg-ink-850/60 transition-colors"
      >
        <span className="w-10 h-10 rounded-xl bg-ink-850 border border-ink-700/70 text-slate-200 flex items-center justify-center shrink-0">
          <ShieldIcon className="w-5 h-5" />
        </span>
        <span className="grow min-w-0">
          <span className="block text-sm font-medium text-slate-50">امنیت ورود</span>
          <span className="block text-2xs text-slate-400 leading-4">تایید دومرحله‌ای و خروج از دستگاه‌ها</span>
        </span>
        <ChevronIcon className="w-5 h-5 text-slate-500" />
      </Link>

      {/* ---- sticky save bar, above the phone tab bar ---- */}
      {dirty && (
        <div className="fixed inset-x-0 z-40 px-4 above-tabbar pointer-events-none">
          <div className="max-w-3xl mx-auto pointer-events-auto">
            <div className="card-raised bg-ink-800 p-2.5 ps-4 flex items-center gap-2 shadow-modal animate-pop-in">
              <span className="text-sm text-slate-100 grow">تغییرات ذخیره نشده</span>
              <button className="btn-ghost btn-sm" onClick={reset}>
                بازگردانی
              </button>
              <button className="btn-primary btn-sm min-w-[5rem]" onClick={save} disabled={saving}>
                {saving ? <Spinner className="w-4 h-4 border-ink-950/40 border-t-ink-950" /> : 'ذخیره'}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={armDialog}
        title="مسلح کردن ربات"
        danger={live}
        confirmText="مسلح کن"
        requirePhrase={live ? 'ARM LIVE' : undefined}
        body={
          live ? (
            <>
              از این لحظه ربات با <b className="text-loss">پول واقعی</b> در صرافی خرید و فروش می‌کند. مطمئن شو که:
              <ul className="list-disc ps-5 mt-2 space-y-1 text-xs">
                <li>کلیدهای API فقط دسترسی معامله دارند، نه برداشت</li>
                <li>مقدار ریسک هر معامله را بررسی کرده‌ای</li>
                <li>حد ضرر روزانه و حد افت کل را قبول داری</li>
              </ul>
            </>
          ) : (
            'ربات شروع به معامله می‌کند. در این حالت پول واقعی درگیر نمی‌شود.'
          )
        }
        onConfirm={() => toggleArm(true)}
        onCancel={() => setArmDialog(false)}
      />
    </div>
  )
}

function FieldInput({
  field,
  draft,
  setDraft,
  disabled = false,
}: {
  field: Field
  draft: Fields
  setDraft: (next: Fields) => void
  disabled?: boolean
}) {
  if (field.type === 'bool') {
    return (
      <div className="sm:col-span-2 border-b border-ink-800 last:border-b-0">
        <Toggle
          label={field.label}
          hint={field.hint}
          checked={Boolean(draft[field.key])}
          onChange={(checked) => {
            if (!disabled) setDraft({ ...draft, [field.key]: checked })
          }}
        />
      </div>
    )
  }
  return (
    <div>
      <label className="label" htmlFor={`setting-${field.key}`}>
        {field.label}
        {field.unit && <span className="text-slate-500 ms-1">({field.unit})</span>}
      </label>
      <input
        id={`setting-${field.key}`}
        className="input num"
        type="number"
        inputMode="decimal"
        dir="ltr"
        step={field.step}
        min={field.min}
        max={field.max}
        disabled={disabled}
        value={draft[field.key] ?? ''}
        onChange={(event) =>
          setDraft({
            ...draft,
            [field.key]: event.target.value === '' ? null : Number(event.target.value),
          })
        }
      />
      {field.hint && <p className="text-2xs text-slate-400 mt-1.5 leading-4">{field.hint}</p>}
    </div>
  )
}

function Info({ label, value }: { label: string; value?: string }) {
  return (
    <div className="flex justify-between gap-4 py-2.5 border-b border-ink-800 text-sm">
      <dt className="text-slate-400">{label}</dt>
      <dd className="num text-slate-100">{value ?? '—'}</dd>
    </div>
  )
}

function SettingsSkeleton() {
  return (
    <div className="space-y-4 max-w-3xl mx-auto" aria-busy="true">
      <div className="card p-4 space-y-3">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-11 w-full" />
      </div>
      {Array.from({ length: 2 }).map((_, index) => (
        <div key={index} className="card p-4 space-y-4">
          <Skeleton className="h-4 w-28" />
          <div className="grid sm:grid-cols-2 gap-4">
            {Array.from({ length: 4 }).map((_, field) => (
              <div key={field} className="space-y-2">
                <Skeleton className="h-3 w-32" />
                <Skeleton className="h-12 w-full" />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
