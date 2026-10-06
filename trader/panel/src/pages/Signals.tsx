import { ReactNode, useEffect, useState } from 'react'
import clsx from 'clsx'
import {
  api,
  Watch,
  WatchBreakoutRow,
  WatchMomentumRow,
  WatchPosition,
  WatchRegime,
  WatchSignal,
} from '@/lib/api'
import { CORE_FA, DISPLAY_TZ, FAMILY_FA, REGIME_FA, SIDE_FA, clock, datetime, money, pct, price, qty, timeAgo, quoteLabel } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import { Badge, Card, Empty, ErrorBanner, KV, Notice, PnL, Segmented, Skeleton, SkeletonRows } from '@/components/ui'
import { ChevronIcon, SignalIcon, SparkIcon } from '@/components/icons'

/**
 * What the 4h core is watching. One payload (`/api/watch`) carries the next
 * evaluation, the BTC regime, the momentum ranks, the turtle channels, the
 * open positions with their exit rule, the risk gates and the last signal
 * rows; the page only lays it out. Every time shown is Tehran time.
 */
export default function Signals() {
  const watch = usePolling(() => api.watch(), 30000)
  const reviews = usePolling(() => api.llmReviews(3), 60000)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [filter, setFilter] = useState<'all' | 'executed'>('all')

  const data = watch.data
  if (watch.loading && !data) return <SignalsSkeleton />

  const rows = (data?.recent ?? []).filter((row) => (filter === 'executed' ? row.executed : true))

  return (
    <div className="space-y-4">
      <ErrorBanner message={watch.error} />

      {data && !data.is_portfolio4h && (
        <Notice tone="info">
          این صفحه برای هسته‌ی پرتفوی ۴ساعته ساخته شده؛ هسته‌ی فعلی «{CORE_FA[data.core] ?? data.core}» است و فقط
          تاریخچه‌ی سیگنال‌ها نمایش داده می‌شود.
        </Notice>
      )}

      {data && data.is_portfolio4h && (
        <>
          <Hero data={data} onRefresh={watch.refresh} />
          {data.gates && <GatesCard data={data} />}
          <div className="grid lg:grid-cols-2 gap-4 items-start">
            <MomentumCard data={data} />
            <BreakoutsCard data={data} />
          </div>
          {data.positions.length > 0 && <PositionsCard rows={data.positions} />}
        </>
      )}

      <AiCard reviews={reviews.data} loading={reviews.loading} />

      {/* ---- signal history ---- */}
      <Card
        padded={false}
        title="تاریخچه‌ی سیگنال‌ها"
        action={
          <Segmented
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'all', label: 'همه' },
              { value: 'executed', label: 'اجراشده' },
            ]}
          />
        }
      >
        {rows.length === 0 ? (
          <Empty icon={<SignalIcon className="w-6 h-6" />}>
            {filter === 'executed'
              ? 'هنوز سیگنالی اجرا نشده است'
              : 'هنوز سیگنالی ثبت نشده است. هر سیگنال در لحظه‌ی بسته‌شدن کندل ۴ساعته این‌جا ثبت می‌شود، چه اجرا شود چه رد.'}
          </Empty>
        ) : (
          <ul>
            {rows.map((signal) => (
              <SignalItem
                key={signal.id}
                signal={signal}
                open={expanded === signal.id}
                onToggle={() => setExpanded(expanded === signal.id ? null : signal.id)}
              />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

/* ---------------------------------------------------------------- hero */

function useCountdown(targetIso: string | null | undefined): number | null {
  const target = targetIso ? new Date(targetIso).getTime() : NaN
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  if (Number.isNaN(target)) return null
  return Math.max(0, Math.floor((target - now) / 1000))
}

function Hero({ data, onRefresh }: { data: Watch; onRefresh: () => void }) {
  const seconds = useCountdown(data.next_evaluation_at)

  // The engine evaluates within seconds of the close; fetch the new bar a
  // little later instead of waiting for the next poll.
  useEffect(() => {
    if (seconds !== 0) return
    const timer = window.setTimeout(onRefresh, 8000)
    return () => window.clearTimeout(timer)
  }, [seconds, onRefresh])

  const regime = data.market_regime
  const engine = data.engine
  const ready = Boolean(data.history_ready)
  const stored = Object.values(data.bars_stored)
  const fewest = stored.length ? Math.min(...stored.map((b) => b.bars)) : 0

  return (
    <section className="card overflow-hidden">
      <div className="grid grid-cols-3 border-b border-ink-800 [&>*+*]:border-s [&>*+*]:border-ink-800">
        <Cell label="موتور" value={engine.online ? 'روشن' : 'خاموش'} tone={engine.online ? 'profit' : 'loss'} live={engine.online} />
        <Cell
          label="کندل بعدی"
          value={data.is_rebalance_bar ? 'رتبه‌بندی روزانه' : 'بررسی ۴ ساعته'}
          tone={data.is_rebalance_bar ? 'accent' : 'neutral'}
          plain
        />
        <Cell
          label="رژیم بیت‌کوین"
          value={!regime ? '—' : regime.regime_on ? 'روشن' : 'خاموش'}
          tone={!regime ? 'neutral' : regime.regime_on ? 'profit' : 'loss'}
        />
      </div>

      <div className="relative p-5 sm:p-6">
        <div
          className={clsx(
            'pointer-events-none absolute -top-24 -end-24 w-72 h-72 rounded-full blur-3xl opacity-20',
            !regime ? 'bg-accent' : regime.regime_on ? 'bg-profit' : 'bg-loss',
          )}
          aria-hidden="true"
        />
        <div className="relative">
          <div className="text-xs text-slate-400">ارزیابی بعدی</div>
          <div className="num text-hero sm:text-[3rem] font-extrabold text-slate-50 mt-1">
            {seconds == null ? '—' : seconds === 0 ? (
              <span className="text-2xl sm:text-3xl text-accent">در حال ارزیابی…</span>
            ) : (
              countdownText(seconds)
            )}
          </div>
          <div className="text-2xs text-slate-400 mt-1.5 leading-5">
            ساعت <N className="text-slate-200">{localTime(data.next_evaluation_at)}</N> به وقت تهران
            <span className="mx-1.5">·</span>
            {data.is_rebalance_bar ? (
              'رتبه‌بندی روزانه‌ی مومنتوم، به‌علاوه‌ی شکست‌ها و خروج‌ها'
            ) : (
              <>
                فقط شکست کانال و خروج‌ها؛ رتبه‌بندی روزانه‌ی بعدی{' '}
                <N className="text-slate-200">{clock(data.next_rebalance_at)}</N>
              </>
            )}
          </div>

          {regime ? (
            <RegimePill regime={regime} />
          ) : (
            <Notice tone="warn" className="mt-4">
              تاریخچه‌ی کندل در حال دانلود… (<N>{fewest}</N> از <N>{data.min_bars}</N> کندل ۴ساعته). تا کامل شدن، ربات
              ارزیابی نمی‌کند.
            </Notice>
          )}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-4 pt-3 border-t border-ink-800 text-2xs text-slate-400">
            <span>
              آخرین ارزیابی{' '}
              <span className="text-slate-200">{data.last_evaluated_close ? timeAgo(data.last_evaluated_close) : 'هنوز انجام نشده'}</span>
            </span>
            {ready && (
              <span>
                آخرین کندل بسته‌شده <N className="text-slate-200">{clock(data.evaluated_bar_close)}</N>
              </span>
            )}
            {data.data_lag_bars ? (
              <span className="text-warn">
                داده <N>{data.data_lag_bars}</N> کندل عقب‌تر از ساعت است
              </span>
            ) : null}
            {!engine.online && (
              <span className="text-loss">
                ضربان موتور{' '}
                {engine.heartbeat_age_s != null ? (
                  <>
                    <N>{engine.heartbeat_age_s}</N> ثانیه پیش
                  </>
                ) : (
                  'دریافت نشده'
                )}
              </span>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}

function RegimePill({ regime }: { regime: WatchRegime }) {
  const on = regime.regime_on
  return (
    <div
      className={clsx(
        'mt-4 flex items-center gap-3 rounded-xl border px-3 py-2.5',
        on ? 'bg-profit/10 border-profit/30' : 'bg-loss/10 border-loss/30',
      )}
    >
      <div dir="ltr" className="shrink-0">
        <Sparkline values={regime.sparkline} ema={regime.ema_sparkline} on={on} />
      </div>
      <div className="min-w-0 grow">
        <div className={clsx('text-sm font-semibold', on ? 'text-profit' : 'text-loss')}>
          رژیم بیت‌کوین: {on ? 'روشن' : 'خاموش'}
          <span className="text-2xs font-normal text-slate-400 ms-2">
            {on ? 'ورود مجاز است' : 'هیچ ورودی انجام نمی‌شود'}
          </span>
        </div>
        <div className="text-2xs text-slate-400 leading-5 mt-0.5">
          بسته‌شدن <N className="text-slate-200">{price(regime.close)}</N>
          <span className="mx-1">·</span>
          EMA200 <N className="text-slate-200">{price(regime.ema200)}</N>
          <span className="mx-1">·</span>
          فاصله <Pct value={regime.distance_pct} />
        </div>
        <div className="text-2xs text-slate-400 leading-5">
          EMA200 {regime.ema_rising ? 'صعودی' : 'صعودی نیست'}
          <span className="mx-1">·</span>
          {on ? 'روشن' : 'خاموش'} از <N>{regime.regime_bars}</N> کندل پیش ({timeAgo(regime.regime_since)})
        </div>
      </div>
    </div>
  )
}

function Sparkline({ values, ema, on }: { values: Array<number | null>; ema?: Array<number | null>; on: boolean }) {
  const w = 88
  const h = 30
  const series = values.map((v) => (v == null ? NaN : v))
  const emaSeries = (ema ?? []).map((v) => (v == null ? NaN : v))
  const all = [...series, ...emaSeries].filter((v) => !Number.isNaN(v))
  if (all.length < 2) return null
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  const points = (s: number[]) =>
    s
      .map((v, i) =>
        Number.isNaN(v) ? null : `${(i / Math.max(1, s.length - 1)) * w},${h - 2 - ((v - min) / span) * (h - 4)}`,
      )
      .filter(Boolean)
      .join(' ')
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      {emaSeries.length > 1 && (
        <polyline points={points(emaSeries)} fill="none" stroke="#64748b" strokeWidth="1" strokeDasharray="2 2" />
      )}
      <polyline
        points={points(series)}
        fill="none"
        stroke={on ? '#34d399' : '#fb7185'}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}

function Cell({
  label,
  value,
  tone,
  live = false,
  plain = false,
}: {
  label: string
  value: string
  tone: 'profit' | 'loss' | 'warn' | 'neutral' | 'accent'
  live?: boolean
  plain?: boolean
}) {
  const color = {
    profit: 'text-profit',
    loss: 'text-loss',
    warn: 'text-warn',
    neutral: 'text-slate-300',
    accent: 'text-accent',
  }[tone]
  const dot = {
    profit: 'bg-profit',
    loss: 'bg-loss',
    warn: 'bg-warn',
    neutral: 'bg-slate-500',
    accent: 'bg-accent',
  }[tone]
  return (
    <div className="px-2 py-2.5 text-center min-w-0">
      <div className="text-2xs text-slate-400 leading-4">{label}</div>
      <div className={clsx('flex items-center justify-center gap-1.5 text-xs sm:text-sm font-semibold leading-5', color)}>
        {!plain && <span className={clsx('inline-block w-1.5 h-1.5 rounded-full shrink-0', dot, live && 'animate-breathe')} />}
        <span className="truncate">{value}</span>
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- gates */

const GATE_FA: Record<string, string> = {
  kill_switch: 'کلید اضطراری فعال است',
  not_armed: 'ربات مسلح نیست',
  paused: 'معامله موقتاً متوقف است',
  drawdown: 'افت سرمایه به سقف رسیده',
  daily_loss: 'ضرر روزانه به سقف رسیده',
  max_positions: 'ظرفیت پوزیشن پر است',
  min_equity: 'سرمایه کمتر از حداقل کاری است',
  session: 'خارج از زمان مجاز ورود',
  funds: 'موجودی آزاد کمتر از حداقل سفارش صرافی است',
  data: 'تاریخچه‌ی کندل هنوز کامل نیست',
  engine_offline: 'موتور معامله‌گر پاسخ نمی‌دهد',
}

function GatesCard({ data }: { data: Watch }) {
  const g = data.gates
  if (!g) return null
  const regime = data.market_regime
  const armedBreakouts = data.breakouts.filter((b) => b.armed && !b.triggered && b.tradable && !b.held)
  const nearest = armedBreakouts[0]
  const top3 = data.momentum.filter((m) => m.in_top3 && m.tradable).map((m) => m.symbol)
  const cooldowns = Object.entries(g.cooldowns)

  const checks: Array<{ label: string; ok: boolean; value: ReactNode }> = [
    { label: 'مسلح بودن ربات', ok: g.mode !== 'live' || g.armed, value: g.armed ? 'مسلح' : 'غیرمسلح' },
    { label: 'کلید اضطراری', ok: !g.kill_switch, value: g.kill_switch ? 'فعال' : 'خاموش' },
    {
      label: 'توقف موقت',
      ok: !g.paused_until,
      value: g.paused_until ? <>تا <N>{datetime(g.paused_until)}</N></> : 'ندارد',
    },
    { label: 'موتور معامله‌گر', ok: data.engine.online, value: data.engine.online ? 'روشن' : 'خاموش' },
    {
      label: 'ظرفیت پوزیشن',
      ok: g.open_positions < g.max_positions,
      value: <N>{g.open_positions} / {g.max_positions}</N>,
    },
    {
      label: 'موجودی آزاد',
      ok: g.funds_ok,
      value: (
        <>
          <N>{money(g.free_balance)}</N> {quoteLabel()}
        </>
      ),
    },
    {
      label: 'حداقل سرمایه',
      ok: g.equity >= g.min_equity,
      value: (
        <>
          <N>{money(g.equity)}</N> (حداقل <N>{money(g.min_equity)}</N>)
        </>
      ),
    },
    { label: 'زمان مجاز ورود', ok: !g.session_block, value: g.session_block ? sessionFa(g.session_block) : 'آزاد' },
    {
      label: 'افت سرمایه',
      ok: g.drawdown_pct < g.max_drawdown_pct,
      value: (
        <N>
          {g.drawdown_pct.toFixed(1)}% / {g.max_drawdown_pct}%
        </N>
      ),
    },
    {
      label: 'ضرر روزانه',
      ok: g.today_pnl_pct > -g.max_daily_loss_pct,
      value: (
        <N>
          {pct(g.today_pnl_pct)} / −{g.max_daily_loss_pct}%
        </N>
      ),
    },
    { label: 'تاریخچه‌ی کندل', ok: Boolean(data.history_ready), value: data.history_ready ? 'کامل' : 'در حال دانلود' },
  ]

  return (
    <Card title={g.ready ? 'وضعیت ورود' : 'چرا الان معامله نمی‌کند'}>
      {g.ready ? (
        <Notice tone="ok">آماده‌ی ورود — همه‌ی شرط‌های ریسک برقرارند؛ با اولین سیگنال معتبر خرید می‌کند.</Notice>
      ) : (
        <div className="space-y-2">
          {g.blocking.map((gate) => (
            <div key={gate.code} className="flex items-start gap-2.5 text-sm text-loss leading-6">
              <span className="mt-2.5 w-1.5 h-1.5 rounded-full bg-loss shrink-0" />
              <span>
                {GATE_FA[gate.code] ?? gate.code}
                <span className="block text-2xs text-slate-500 leading-4" dir="ltr">
                  {gate.detail}
                </span>
              </span>
            </div>
          ))}
        </div>
      )}

      {regime && (
        <Notice tone={regime.regime_on ? 'info' : 'warn'} className="mt-3">
          {!regime.regime_on ? (
            'رژیم بیت‌کوین خاموش است: تا برگشت بیت‌کوین بالای EMA200 هیچ ورود جدیدی انجام نمی‌شود و پوزیشن‌های باز در بسته‌شدن کندل بسته می‌شوند.'
          ) : data.is_rebalance_bar ? (
            <>
              در بسته‌شدن این کندل، سه نماد برتر مومنتوم بررسی می‌شوند
              {top3.length > 0 && (
                <>
                  {' '}
                  (<N>{top3.join(' · ')}</N>)
                </>
              )}
              ؛ به‌علاوه‌ی شکست کانال برای <N>{armedBreakouts.length}</N> نماد آماده.
            </>
          ) : (
            <>
              تا رتبه‌بندی روزانه‌ی بعدی (<N>{clock(data.next_rebalance_at)}</N>) فقط شکست کانال ۵۵ کندل می‌تواند ورود
              بدهد؛ <N>{armedBreakouts.length}</N> نماد آماده است
              {nearest && nearest.distance_pct != null && (
                <>
                  ، نزدیک‌ترین <N>{nearest.symbol}</N> با فاصله‌ی <N>{Math.abs(nearest.distance_pct).toFixed(2)}%</N>
                </>
              )}
              .
            </>
          )}
        </Notice>
      )}

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 mt-4">
        {checks.map((check) => (
          <div key={check.label} className="flex items-start gap-2 min-w-0">
            <span
              className={clsx(
                'mt-1 w-4 h-4 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0',
                check.ok ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss',
              )}
              aria-label={check.ok ? 'برقرار' : 'مانع'}
            >
              {check.ok ? '✓' : '✕'}
            </span>
            <div className="min-w-0">
              <dt className="text-2xs text-slate-400 leading-4">{check.label}</dt>
              <dd className={clsx('text-xs leading-5 truncate', check.ok ? 'text-slate-200' : 'text-loss')}>{check.value}</dd>
            </div>
          </div>
        ))}
      </dl>

      {(cooldowns.length > 0 || g.sizing.length > 0) && (
        <div className="mt-4 pt-3 border-t border-ink-800 space-y-1 text-2xs text-slate-400 leading-5">
          {cooldowns.map(([symbol, minutes]) => (
            <div key={symbol}>
              <N className="text-slate-200">{symbol}</N> بعد از ضرر در استراحت است: <N>{minutes}</N> دقیقه‌ی دیگر (فقط شکست کانال)
            </div>
          ))}
          {g.sizing.map((s) => (
            <div key={s.family} className={clsx(!s.ok && 'text-warn')}>
              کوچک‌ترین سفارش {FAMILY_SHORT_FA[s.family] ?? s.family}: <N>{money(s.notional)}</N> {quoteLabel()} (<N>{s.symbol}</N>؛ ریسک{' '}
              <N>{money(s.risk_amount)}</N>) — حداقل صرافی <N>{money(s.min_notional)}</N>
              {!s.ok && ' — زیر حداقل؛ ممکن است رد شود'}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

/* ---------------------------------------------------------------- momentum */

function MomentumCard({ data }: { data: Watch }) {
  const rows = data.momentum
  const regimeOn = data.market_regime?.regime_on ?? false
  return (
    <Card
      padded={false}
      title="رتبه‌بندی مومنتوم"
      action={<span className="text-2xs text-slate-400">بازده ۷ روز منهای ۱ روز</span>}
    >
      {rows.length === 0 ? (
        <Empty>تاریخچه‌ی کندل در حال دانلود…</Empty>
      ) : (
        <>
          <ul>
            {rows.map((row) => (
              <MomentumItem key={row.symbol} row={row} />
            ))}
          </ul>
          <p className="px-4 sm:px-5 py-3 text-2xs text-slate-500 leading-5 border-t border-ink-800">
            روی آخرین کندل بسته‌شده. در رتبه‌بندی روزانه (ساعت <N>{localTime(data.next_rebalance_at)}</N> تهران) سه‌تای برتر
            خریده می‌شوند{regimeOn ? '' : ' — فعلاً رژیم بیت‌کوین خاموش است'}؛ نمادی که تا رتبه‌ی ۵ بماند نگه داشته می‌شود.
            حد ضرر: <N>3</N> ATR زیر قیمت بسته‌شدن.
          </p>
        </>
      )}
    </Card>
  )
}

function MomentumItem({ row }: { row: WatchMomentumRow }) {
  return (
    <li
      className={clsx(
        'flex items-center gap-3 px-4 sm:px-5 py-2.5 border-b border-ink-800 last:border-b-0 min-h-[56px]',
        row.in_top3 && 'bg-profit/5',
      )}
    >
      <span
        className={clsx(
          'w-8 h-8 rounded-lg flex items-center justify-center num text-sm font-bold shrink-0',
          row.in_top3 ? 'bg-profit/15 text-profit' : row.in_top5 ? 'bg-warn/15 text-warn' : 'bg-ink-800 text-slate-400',
        )}
      >
        {row.rank ?? '—'}
      </span>
      <div className="grow min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-sm font-semibold text-slate-50">{row.symbol}</span>
          {row.held && <Badge tone="accent">در پرتفوی</Badge>}
          {!row.tradable && <Badge tone="neutral">فقط رتبه</Badge>}
          {row.stale && <Badge tone="warn">کندل ناقص</Badge>}
          {row.exit_now && row.held === 'momentum' && <Badge tone="loss">خروج</Badge>}
        </div>
        <div className="text-2xs text-slate-400 mt-0.5 leading-4">
          ۷ روز <Pct value={row.ret_7d_pct} />
          <span className="mx-1">·</span>۱ روز <Pct value={row.ret_1d_pct} />
          <span className="mx-1">·</span>امتیاز <Pct value={row.momentum_pct} />
        </div>
      </div>
      <div className="text-left shrink-0">
        <div className="text-2xs text-slate-400 leading-4">حد ضرر</div>
        <div className="num text-xs text-loss leading-5">{price(row.stop_price)}</div>
      </div>
    </li>
  )
}

/* ---------------------------------------------------------------- breakouts */

function BreakoutsCard({ data }: { data: Watch }) {
  const rows = data.breakouts
  return (
    <Card padded={false} title="شکست‌های در انتظار" action={<span className="text-2xs text-slate-400">سقف ۵۵ کندل ۴ساعته</span>}>
      {rows.length === 0 ? (
        <Empty>تاریخچه‌ی کندل در حال دانلود…</Empty>
      ) : (
        <>
          <ul>
            {rows.map((row) => (
              <BreakoutItem key={row.symbol} row={row} />
            ))}
          </ul>
          <p className="px-4 sm:px-5 py-3 text-2xs text-slate-500 leading-5 border-t border-ink-800">
            ورود وقتی کندل ۴ساعته بالای سقف ۵۵ کندل قبلی بسته شود، نماد بالای EMA200 خودش باشد و بیت‌کوین بالای EMA200
            صعودی. خروج با بسته‌شدن زیر کف ۲۰ کندل. حد ضرر: <N>2.5</N> ATR.
          </p>
        </>
      )}
    </Card>
  )
}

function BreakoutItem({ row }: { row: WatchBreakoutRow }) {
  const ready = Boolean(row.armed && !row.triggered)
  const distance = row.distance_pct
  return (
    <li
      className={clsx(
        'flex items-center gap-3 px-4 sm:px-5 py-2.5 border-b border-ink-800 last:border-b-0 min-h-[56px]',
        row.triggered && 'bg-profit/5',
        ready && 'border-s-2 border-s-accent/60',
        !row.armed && !row.triggered && 'opacity-70',
      )}
    >
      <div className="grow min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-sm font-semibold text-slate-50">{row.symbol}</span>
          {row.triggered ? (
            <Badge tone="profit">شکست شد</Badge>
          ) : row.armed ? (
            <Badge tone="accent">آماده</Badge>
          ) : row.stale ? (
            <Badge tone="warn">کندل ناقص</Badge>
          ) : !row.market_on ? (
            <Badge tone="neutral">رژیم خاموش</Badge>
          ) : !row.above_ema200 ? (
            <Badge tone="neutral">زیر EMA200</Badge>
          ) : null}
          {row.held && <Badge tone="accent">در پرتفوی</Badge>}
          {!row.tradable && <Badge tone="neutral">فقط داده</Badge>}
        </div>
        <div className="text-2xs text-slate-400 mt-0.5 leading-4">
          {distance == null ? (
            'کانال هنوز شکل نگرفته'
          ) : distance <= 0 ? (
            <>
              <N className="text-profit">{Math.abs(distance).toFixed(2)}%</N> بالای سقف
            </>
          ) : (
            <>
              <N className="text-slate-200">{distance.toFixed(2)}%</N> تا سقف
              {row.distance_atr != null && (
                <>
                  {' '}
                  (<N>{row.distance_atr.toFixed(1)}</N> ATR)
                </>
              )}
            </>
          )}
          <span className="mx-1">·</span>
          فعلی <N>{price(row.mark ?? row.close)}</N>
          {row.held === 'turtle' && row.exit_low != null && (
            <>
              <span className="mx-1">·</span>
              خروج زیر <N className="text-loss">{price(row.exit_low)}</N>
            </>
          )}
        </div>
      </div>
      <div className="text-left shrink-0">
        <div className="text-2xs text-slate-400 leading-4">سقف ۵۵</div>
        <div className="num text-xs text-slate-200 leading-5">{price(row.entry_high)}</div>
      </div>
    </li>
  )
}

/* ---------------------------------------------------------------- positions */

function PositionsCard({ rows }: { rows: WatchPosition[] }) {
  return (
    <Card
      padded={false}
      title={
        <>
          پوزیشن‌های باز <span className="num text-slate-400 font-medium">({rows.length})</span>
        </>
      }
    >
      <ul className="px-3 pb-3 grid md:grid-cols-2 gap-2">
        {rows.map((position) => (
          <PositionItem key={position.id} position={position} />
        ))}
      </ul>
    </Card>
  )
}

function PositionItem({ position }: { position: WatchPosition }) {
  const e = position.exit
  const long = position.side === 'long'
  return (
    <li className="card-raised p-3.5">
      <div className="flex items-center gap-2">
        <span
          className={clsx(
            'w-9 h-9 rounded-lg flex items-center justify-center text-2xs font-bold shrink-0',
            long ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss',
          )}
        >
          {SIDE_FA[position.side] ?? position.side}
        </span>
        <div className="grow min-w-0">
          <div className="text-sm font-semibold text-slate-50 flex items-center gap-1.5 flex-wrap">
            {position.symbol}
            <Badge tone="accent">{FAMILY_SHORT_FA[position.family] ?? position.family}</Badge>
          </div>
          <div className="text-2xs text-slate-400">
            حجم <N>{qty(position.qty)}</N> · {timeAgo(position.opened_at)}
          </div>
        </div>
        <PnL value={position.unrealized_pnl} percent={position.unrealized_pct} digits={3} className="text-base shrink-0" />
      </div>

      <div className="grid grid-cols-3 gap-2 mt-3">
        <KV label="ورود" value={price(position.entry_price)} />
        <KV label="فعلی" value={price(position.mark)} valueClass="text-slate-50 font-semibold" />
        <KV
          label="حد ضرر"
          value={
            <>
              {price(position.stop_price)}
              {position.stop_distance_pct != null && (
                <span className="text-2xs text-slate-400 ms-1">({position.stop_distance_pct.toFixed(1)}%)</span>
              )}
            </>
          }
          valueClass="text-loss"
        />
      </div>

      <div className="mt-3 pt-3 border-t border-ink-800 text-2xs text-slate-400 leading-5 space-y-0.5">
        <div className="text-slate-300 font-medium">شرط خروج</div>
        {e.rule === 'rank' ? (
          <div>
            رتبه‌ی فعلی <N className="text-slate-200">{e.rank ?? '—'}</N> از <N>{e.universe ?? '—'}</N> — خروج اگر در رتبه‌بندی روزانه
            از <N>{e.hold_n}</N> پایین‌تر برود{' '}
            {e.at_risk && <Badge tone="warn">در خطر خروج</Badge>}
          </div>
        ) : e.rule === 'channel' ? (
          <div>
            کف کانال ۲۰ کندل <N className="text-slate-200">{price(e.exit_low)}</N>
            {e.distance_pct != null && (
              <>
                {' '}
                (<N>{Math.abs(e.distance_pct).toFixed(2)}%</N> پایین‌تر از قیمت)
              </>
            )}{' '}
            — خروج با بسته‌شدن ۴ساعته زیر آن
          </div>
        ) : (
          <div>پوزیشن هسته‌ی قدیمی: فقط حد ضرر و حد سود خودش</div>
        )}
        <div>
          خروج رژیم:{' '}
          {e.regime_on ? (
            'بیت‌کوین بالای EMA200 است'
          ) : (
            <span className="text-loss">بیت‌کوین زیر EMA200 — در ارزیابی بعدی بسته می‌شود</span>
          )}
        </div>
        {e.exit_now && (
          <Notice tone="danger" className="mt-1.5">
            شرط خروج در آخرین کندل فعال شده ({REASON_SHORT_FA[e.exit_reason] ?? e.exit_reason}); موتور باید آن را بسته باشد یا در حال بستن باشد.
          </Notice>
        )}
      </div>
    </li>
  )
}

/* ---------------------------------------------------------------- AI review */

function AiCard({ reviews, loading }: { reviews: any[] | null; loading: boolean }) {
  const review = reviews?.[0]
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <SparkIcon className="w-4 h-4 text-accent" />
          نگاه هوش مصنوعی به بازار
        </span>
      }
    >
      {loading && !reviews ? (
        <SkeletonRows rows={1} />
      ) : !review ? (
        <Empty>هنوز تحلیلی ثبت نشده است. اگر کلید Anthropic وارد نشده باشد، این لایه خاموش است.</Empty>
      ) : (
        <div>
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <Badge
              tone={
                review.stance === 'risk_on' || review.stance === 'approve'
                  ? 'profit'
                  : review.stance === 'risk_off' || review.stance === 'veto'
                    ? 'loss'
                    : 'neutral'
              }
            >
              {STANCE_LABEL[review.stance] ?? review.stance}
            </Badge>
            {review.symbol && <Badge tone="accent">{review.symbol}</Badge>}
            <span className="text-2xs text-slate-400">
              ضریب ریسک <N>×{Number(review.risk_multiplier).toFixed(2)}</N>
              <span className="mx-1">·</span>
              اطمینان <N>{(Number(review.confidence) * 100).toFixed(0)}%</N>
            </span>
            <span className="grow" />
            <span className="text-2xs text-slate-400">{timeAgo(review.created_at)}</span>
          </div>
          <p className="text-sm text-slate-200 leading-7">{review.reasoning}</p>
        </div>
      )}
    </Card>
  )
}

const STANCE_LABEL: Record<string, string> = {
  risk_on: 'ریسک‌پذیر',
  neutral: 'خنثی',
  risk_off: 'ریسک‌گریز',
  approve: 'تایید',
  reduce: 'کاهش حجم',
  veto: 'وتو',
}

/* ---------------------------------------------------------------- history */

function SignalItem({ signal, open, onToggle }: { signal: WatchSignal; open: boolean; onToggle: () => void }) {
  const long = signal.side === 'long'
  const score = Math.round(signal.score * 100)
  // The 4h core names its family in contributions.family.reason; the
  // ensemble has no such entry and shows its strategy votes as before.
  const family = signal.family ?? signal.contributions?.family?.reason
  const votes = Object.entries(signal.contributions ?? {}).filter(([name]) => name !== 'family')
  return (
    <li className="border-b border-ink-800 last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-right flex items-center gap-3 px-4 sm:px-5 py-3 min-h-[64px] transition-colors active:bg-ink-800/70 hover:bg-ink-850/60"
        aria-expanded={open}
      >
        <span
          className={clsx(
            'w-10 h-10 rounded-xl flex flex-col items-center justify-center shrink-0 leading-none',
            long ? 'bg-profit/15 text-profit' : signal.side === 'short' ? 'bg-loss/15 text-loss' : 'bg-slate-400/15 text-slate-200',
          )}
        >
          <span className="num text-sm font-bold">{score}</span>
          <span className="text-[9px] mt-0.5 opacity-80">{SIDE_FA[signal.side] ?? signal.side}</span>
        </span>

        <span className="grow min-w-0">
          <span className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-slate-50">{signal.symbol}</span>
            {family && <Badge tone="accent">{FAMILY_SHORT_FA[family] ?? FAMILY_FA[family] ?? family}</Badge>}
            <span className="text-2xs text-slate-400">{REGIME_FA[signal.regime] ?? signal.regime}</span>
          </span>
          <span className="block text-2xs text-slate-400 mt-0.5">
            <N>{clock(signal.created_at)}</N>
            {signal.ml_probability != null && (
              <>
                <span className="mx-1.5">·</span>
                مدل <N>{(signal.ml_probability * 100).toFixed(0)}%</N>
              </>
            )}
          </span>
          {!signal.executed && signal.rejection_reason && signal.llm_verdict !== 'veto' && !open && (
            <span className="block text-2xs text-slate-500 truncate text-right">{rejectionFa(signal.rejection_reason)}</span>
          )}
        </span>

        <span className="shrink-0 flex items-center gap-2">
          {signal.executed ? (
            <Badge tone="profit">اجرا شد</Badge>
          ) : signal.llm_verdict === 'veto' ? (
            <Badge tone="loss">وتو</Badge>
          ) : (
            <Badge tone="neutral">رد شد</Badge>
          )}
          <ChevronIcon className={clsx('w-4 h-4 text-slate-500 transition-transform', open && '-rotate-90')} />
        </span>
      </button>

      {open && (
        <div className="px-4 sm:px-5 pb-4 space-y-4 animate-fade-in">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 card-raised p-3">
            <KV label="قیمت" value={price(signal.price)} />
            {family ? (
              <KV label="حد ضرر پیشنهادی" value={price(signal.suggested_stop)} valueClass="text-loss" />
            ) : (
              <KV label="هدف حرکت" value={`${signal.expected_move_pct.toFixed(2)}%`} />
            )}
            {family ? (
              <KV label="خانواده" value={FAMILY_FA[family] ?? family} valueClass="!font-sans" />
            ) : (
              <KV
                label="احتمال مدل"
                value={signal.ml_probability != null ? `${(signal.ml_probability * 100).toFixed(1)}%` : '—'}
              />
            )}
            <KV label="رژیم بازار" value={REGIME_FA[signal.regime] ?? signal.regime} valueClass="!font-sans" />
          </div>

          {signal.rejection_reason && (
            <div className="text-xs text-warn bg-warn/10 border border-warn/25 rounded-xl px-3 py-2.5 leading-5">
              دلیل رد: {rejectionFa(signal.rejection_reason)}
              <span className="block text-2xs text-warn/70 mt-0.5" dir="ltr">
                {signal.rejection_reason}
              </span>
            </div>
          )}

          {signal.llm_note && (
            <div className="text-xs text-slate-200 bg-accent/10 border border-accent/20 rounded-xl px-3 py-2.5 leading-6">
              <span className="text-accent font-medium">نظر هوش مصنوعی: </span>
              {signal.llm_note}
            </div>
          )}

          <div>
            <div className="text-2xs text-slate-400 mb-2">{family ? 'قوانین استراتژی' : 'رأی استراتژی‌ها'}</div>
            <div className="space-y-2">
              {votes.map(([name, vote]) => (
                <div key={name} className="flex items-center gap-3 text-xs">
                  <span className="w-28 shrink-0 text-slate-300 truncate" title={vote.reason}>
                    {strategyLabel(name)}
                  </span>
                  <div className="grow h-2 rounded-full bg-ink-800 relative overflow-hidden" dir="ltr">
                    <div
                      className={clsx('absolute top-0 h-full', vote.score >= 0 ? 'bg-profit' : 'bg-loss')}
                      style={{
                        width: `${Math.min(50, Math.abs(vote.score) * 50)}%`,
                        [vote.score >= 0 ? 'left' : 'right']: '50%',
                      }}
                    />
                    <div className="absolute left-1/2 top-0 h-full w-px bg-ink-600" />
                  </div>
                  <span className={clsx('num w-12 text-left shrink-0 font-medium', vote.score >= 0 ? 'text-profit' : 'text-loss')}>
                    {vote.score >= 0 ? '+' : ''}
                    {vote.score.toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
            {family && (
              <ul className="mt-3 space-y-1">
                {votes.map(([name, vote]) => (
                  <li key={name} className="text-2xs text-slate-400 leading-5">
                    <span className="text-slate-300">{strategyLabel(name)}:</span> {ruleReasonFa(name, vote.reason)}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </li>
  )
}

const STRATEGY_FA: Record<string, string> = {
  ema_trend: 'روند EMA',
  rsi_reversion: 'بازگشت RSI',
  squeeze_breakout: 'شکست فشردگی',
  vwap_pullback: 'پولبک VWAP',
  momentum_burst: 'انفجار مومنتوم',
  structure: 'ساختار بازار',
  htf_bias: 'تایم‌فریم بالاتر',
  // 4h core rules
  btc_regime: 'رژیم بیت‌کوین',
  momentum_rank: 'رتبه مومنتوم',
  breakout: 'شکست کانال ۵۵',
  trend_filter: 'فیلتر روند EMA200',
  channel_exit: 'خروج کانال ۲۰',
}

function strategyLabel(name: string): string {
  return STRATEGY_FA[name] ?? name
}

/* ---------------------------------------------------------------- Persian copy */

const FAMILY_SHORT_FA: Record<string, string> = {
  momentum: 'مومنتوم',
  turtle: 'شکست کانال',
  ensemble: 'انسمبل',
}

const REASON_SHORT_FA: Record<string, string> = {
  signal_exit: 'قانون استراتژی',
  regime_exit: 'رژیم بیت‌کوین',
}

/** Rule reasons as the engine writes them (app/workers/trader.py, _core_contributions). */
function ruleReasonFa(name: string, reason: string): ReactNode {
  let m: RegExpMatchArray | null
  switch (name) {
    case 'btc_regime':
      m = reason.match(/close (above|below) EMA200 \(([\d.]+) vs ([\d.]+)\), EMA200 (rising|not rising)/)
      if (m) {
        return (
          <>
            بسته‌شدن ۴ساعته‌ی بیت‌کوین {m[1] === 'above' ? 'بالای' : 'زیر'} EMA200 (<N>{m[2]}</N> در برابر <N>{m[3]}</N>)؛ EMA200 در
            ۲۴ ساعت گذشته {m[4] === 'rising' ? 'صعودی' : 'صعودی نیست'}
          </>
        )
      }
      break
    case 'momentum_rank':
      m = reason.match(/rank (\d+) of (\d+)(?:, 7d-1d return ([+-]?[\d.]+)%)? \((rebalance bar|not a rebalance bar)\)/)
      if (m) {
        return (
          <>
            رتبه <N>{m[1]}</N> از <N>{m[2]}</N>
            {m[3] && (
              <>
                {' '}
                · بازده ۷ روز منهای ۱ روز <N>{m[3]}%</N>
              </>
            )}{' '}
            · {m[4] === 'rebalance bar' ? 'کندل رتبه‌بندی روزانه' : 'کندل رتبه‌بندی نیست'}
          </>
        )
      }
      if (reason.startsWith('no rank yet')) return 'هنوز رتبه‌ای ندارد (تاریخچه کافی نیست)'
      break
    case 'breakout':
      m = reason.match(/close ([+-]?[\d.]+) ATR vs the previous 55-bar high ([\d.eE+-]+)/)
      if (m) {
        return (
          <>
            بسته‌شدن <N>{m[1]}</N> ATR نسبت به سقف ۵۵ کندل قبلی <N>{m[2]}</N>
          </>
        )
      }
      if (reason.includes('not formed')) return 'کانال ۵۵ کندل هنوز شکل نگرفته'
      break
    case 'trend_filter':
      m = reason.match(/close (above|below) own EMA200, ADX ([\d.]+)/)
      if (m) {
        return (
          <>
            بسته‌شدن {m[1] === 'above' ? 'بالای' : 'زیر'} EMA200 خود نماد · ADX <N>{m[2]}</N>
          </>
        )
      }
      break
    case 'channel_exit':
      m = reason.match(/exit below the previous 20-bar low ([\d.eE+-]+)/)
      if (m) {
        return (
          <>
            خروج با بسته‌شدن زیر کف ۲۰ کندل قبلی <N>{m[1]}</N>
          </>
        )
      }
      break
  }
  return <span dir="auto">{reason}</span>
}

/** Rejection reasons as the risk manager / engine write them. */
function rejectionFa(reason: string): ReactNode {
  let m: RegExpMatchArray | null
  if (reason === 'kill switch engaged') return 'کلید اضطراری فعال است'
  if (reason === 'live trading not armed') return 'ربات مسلح نیست'
  if (reason === 'exchange rejected the order') return 'صرافی سفارش را رد کرد'
  if (reason === 'invalid stop distance') return 'فاصله‌ی حد ضرر نامعتبر است'
  if (reason.startsWith('shorts require futures')) return 'فروش نیاز به فیوچرز دارد که خاموش است'
  if (reason.startsWith('no new entries on weekends')) return 'آخر هفته ورود جدید انجام نمی‌شود'
  if ((m = reason.match(/^paused until (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC$/))) {
    return (
      <>
        تا <N>{datetime(`${m[1].replace(' ', 'T')}:00Z`)}</N> متوقف است
      </>
    )
  }
  if ((m = reason.match(/^(\d+) open positions \(max (\d+)\)$/))) {
    return (
      <>
        <N>{m[1]}</N> پوزیشن باز است (حداکثر <N>{m[2]}</N>)
      </>
    )
  }
  if ((m = reason.match(/^already holding (\w+)$/))) {
    return (
      <>
        <N>{m[1]}</N> از قبل در پرتفوی است
      </>
    )
  }
  if ((m = reason.match(/^cooldown for another (\d+) min$/))) {
    return (
      <>
        <N>{m[1]}</N> دقیقه‌ی دیگر در استراحت بعد از ضرر
      </>
    )
  }
  if ((m = reason.match(/^no new entries at (\d{2}):xx UTC$/))) {
    return (
      <>
        در ساعت <N>{m[1]}</N> UTC ورود جدید انجام نمی‌شود
      </>
    )
  }
  if ((m = reason.match(/^bar closed (\d+) min ago; entry window passed$/))) {
    return (
      <>
        کندل <N>{m[1]}</N> دقیقه پیش بسته شده؛ پنجره‌ی ورود گذشته است
      </>
    )
  }
  if ((m = reason.match(/^equity ([\d.]+) below working minimum ([\d.]+)$/))) {
    return (
      <>
        سرمایه <N>{m[1]}</N> کمتر از حداقل کاری <N>{m[2]}</N> است
      </>
    )
  }
  if ((m = reason.match(/^max (?:closed-equity |equity )?drawdown ([\d.]+)% reached$/))) {
    return (
      <>
        افت سرمایه به <N>{m[1]}%</N> رسید
      </>
    )
  }
  if ((m = reason.match(/^daily loss ([\d.-]+)% hit the limit$/))) {
    return (
      <>
        ضرر روزانه <N>{m[1]}%</N> به سقف رسید
      </>
    )
  }
  if ((m = reason.match(/^cannot size (?:at exchange minimum|within risk budget): (.+)$/))) {
    return (
      <>
        حجم قابل سفارش نیست (<span dir="ltr">{m[1]}</span>)
      </>
    )
  }
  if ((m = reason.match(/^reduced size not viable: (.+)$/))) {
    return <>حجم کاهش‌یافته قابل اجرا نیست: {rejectionFa(m[1])}</>
  }
  if ((m = reason.match(/^LLM veto: (.+)$/))) return <>وتوی هوش مصنوعی: {m[1]}</>
  if (reason.startsWith('target ') && reason.endsWith('not worth it')) return 'هدف در برابر کارمزد نمی‌ارزد'
  if (reason.includes('is not listed on')) return 'نماد در صرافی فهرست نشده است'
  return <span dir="auto">{reason}</span>
}

function sessionFa(block: string): ReactNode {
  if (block.startsWith('no new entries on weekends')) return 'آخر هفته (UTC)'
  const m = block.match(/^no new entries at (\d{2}):xx UTC$/)
  if (m) {
    return (
      <>
        ساعت <N>{m[1]}</N> UTC
      </>
    )
  }
  return <span dir="auto">{block}</span>
}

/* ---------------------------------------------------------------- bits */

function N({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx('num', className)}>{children}</span>
}

function Pct({ value, digits = 2 }: { value: number | null | undefined; digits?: number }) {
  if (value == null) return <N className="text-slate-500">—</N>
  return <N className={value > 0 ? 'text-profit' : value < 0 ? 'text-loss' : 'text-slate-300'}>{pct(value, digits)}</N>
}

function countdownText(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** "HH:mm" in the user's zone. */
function localTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString('fa-IR-u-nu-latn', { timeZone: DISPLAY_TZ, hour: '2-digit', minute: '2-digit' })
}

/* ---------------------------------------------------------------- skeleton */

function SignalsSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true">
      <div className="card overflow-hidden">
        <div className="grid grid-cols-3 border-b border-ink-800 [&>*+*]:border-s [&>*+*]:border-ink-800">
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="px-3 py-2.5 space-y-1.5">
              <Skeleton className="h-3 w-10 mx-auto" />
              <Skeleton className="h-4 w-14 mx-auto" />
            </div>
          ))}
        </div>
        <div className="p-5 space-y-3">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-10 w-44" />
          <Skeleton className="h-3 w-64" />
          <Skeleton className="h-16 w-full" />
        </div>
      </div>
      <div className="card p-4">
        <Skeleton className="h-4 w-40 mb-4" />
        <Skeleton className="h-12 w-full mb-3" />
        <div className="grid grid-cols-2 gap-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-8" />
          ))}
        </div>
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-4">
          <Skeleton className="h-4 w-32 mb-4" />
          <SkeletonRows rows={5} />
        </div>
        <div className="card p-4">
          <Skeleton className="h-4 w-32 mb-4" />
          <SkeletonRows rows={5} />
        </div>
      </div>
    </div>
  )
}
