import { useMemo, useState } from 'react'
import clsx from 'clsx'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, Overview, Position } from '@/lib/api'
import { DISPLAY_TZ, MODE_FA, SIDE_FA, STANCE_FA, datetime, money, pct, price, qty, signed, timeAgo, quoteIsToman, quoteLabel } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import {
  Badge,
  Card,
  ConfirmDialog,
  Empty,
  ErrorBanner,
  KV,
  PnL,
  Row,
  Segmented,
  Skeleton,
  SkeletonRows,
  toast,
} from '@/components/ui'
import { SignalIcon, StopIcon, TradesIcon } from '@/components/icons'

const RANGES = [
  { value: 24, label: '۲۴ ساعت' },
  { value: 72, label: '۳ روز' },
  { value: 24 * 7, label: '۱ هفته' },
  { value: 24 * 30, label: '۱ ماه' },
]

type Confirm =
  | { kind: 'kill' }
  | { kind: 'flatten' }
  | { kind: 'arm' }
  | { kind: 'disarm' }
  | { kind: 'close'; id: number; symbol: string }

export default function Dashboard({ marks }: { marks: Record<string, number> }) {
  const [range, setRange] = useState(72)
  const [confirm, setConfirm] = useState<Confirm | null>(null)

  const overview = usePolling(() => api.overview(), 5000)
  const positions = usePolling(() => api.positions('open'), 5000)
  const equity = usePolling(() => api.equity(range), 30000, [range])
  const events = usePolling(() => api.events(12), 10000)

  const data = overview.data

  const chartData = useMemo(() => {
    if (!equity.data) return []
    return equity.data.map((point) => ({
      t: new Date(point.t).getTime(),
      equity: point.equity,
      drawdown: -point.drawdown_pct,
    }))
  }, [equity.data])

  async function run(action: () => Promise<unknown>, done: string, after?: () => void) {
    setConfirm(null)
    try {
      await action()
      toast(done)
      after?.()
    } catch (error) {
      toast(error instanceof Error ? error.message : 'خطا', 'error')
    }
  }

  if (overview.loading && !data) return <DashboardSkeleton />

  return (
    <div className="space-y-4">
      <ErrorBanner message={overview.error} />

      {data && (
        <Hero
          data={data}
          onArm={() => setConfirm({ kind: 'arm' })}
          onDisarm={() => setConfirm({ kind: 'disarm' })}
          onKill={() => setConfirm({ kind: 'kill' })}
          onResume={() => run(() => api.resume(), 'توقف برداشته شد', overview.refresh)}
        />
      )}

      {/* ---- open positions ---- */}
      <Card
          padded={false}
          title={
            <>
              پوزیشن‌های باز
              {positions.data && positions.data.length > 0 && (
                <>
                  {' '}
                  <span className="num text-slate-400 font-medium">({positions.data.length})</span>
                </>
              )}
            </>
          }
          action={
            positions.data && positions.data.length > 0 ? (
              <button className="btn-ghost btn-sm" onClick={() => setConfirm({ kind: 'flatten' })}>
                بستن همه
              </button>
            ) : undefined
          }
        >
          {positions.loading && !positions.data ? (
            <SkeletonRows rows={2} className="px-4 sm:px-5 pb-4" />
          ) : !positions.data || positions.data.length === 0 ? (
            <Empty icon={<TradesIcon className="w-6 h-6" />}>
              الان پوزیشن بازی نیست.
              {data?.armed ? ' ربات منتظر سیگنال مناسب است.' : ' ربات غیرمسلح است و وارد معامله نمی‌شود.'}
            </Empty>
          ) : (
            <>
              <ul className="lg:hidden px-3 pb-3 grid md:grid-cols-2 gap-2">
                {positions.data.map((position) => (
                  <PositionCard
                    key={position.id}
                    position={position}
                    mark={marks[position.symbol]}
                    onClose={() => setConfirm({ kind: 'close', id: position.id, symbol: position.symbol })}
                  />
                ))}
              </ul>
              <div className="hidden lg:block overflow-x-auto pb-2">
                <PositionsTable
                  rows={positions.data}
                  marks={marks}
                  onClose={(id, symbol) => setConfirm({ kind: 'close', id, symbol })}
                />
              </div>
            </>
          )}
      </Card>

      {/* ---- equity curve ---- */}
      <Card
        title="منحنی سرمایه"
        action={<Segmented value={range} onChange={setRange} options={RANGES} />}
      >
        {equity.loading && !equity.data ? (
          <Skeleton className="h-44 sm:h-64 w-full" />
        ) : chartData.length < 2 ? (
          <Empty>هنوز داده کافی برای رسم نمودار جمع نشده است. بعد از چند ساعت کار ربات پر می‌شود.</Empty>
        ) : (
          <div className="h-44 sm:h-64 -mx-1" dir="ltr">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#38bdf8" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#172136" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="t"
                  type="number"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={(value) =>
                    range > 72
                      ? new Date(value).toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit' })
                      : new Date(value).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
                  }
                  stroke="#34466a"
                  tick={{ fill: '#94a3b8', fontSize: 11 }}
                  minTickGap={48}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  stroke="#34466a"
                  tick={{ fill: '#94a3b8', fontSize: 11 }}
                  width={46}
                  domain={['auto', 'auto']}
                  tickFormatter={(value) => Number(value).toFixed(0)}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  labelFormatter={(value) => new Date(value as number).toLocaleString('en-GB')}
                  formatter={(value: number, name) => [
                    name === 'equity' ? `${money(value)} ${quoteLabel()}` : `${value.toFixed(2)}%`,
                    name === 'equity' ? 'سرمایه' : 'افت',
                  ]}
                />
                <Area
                  type="monotone"
                  dataKey="equity"
                  stroke="#38bdf8"
                  strokeWidth={2}
                  fill="url(#equityFill)"
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>

      <div className="grid lg:grid-cols-5 gap-4 items-start">
        {/* ---- performance ---- */}
        {data && (
          <Card title="عملکرد" className="lg:col-span-2">
            <dl>
              <Row label="تعداد کل معاملات" value={<span className="num">{data.trades_total}</span>} />
              <Row label="نرخ برد" value={<span className="num">{(data.win_rate * 100).toFixed(1)}%</span>} />
              <Row
                label="ضریب سود"
                value={<span className="num">{data.profit_factor >= 999 ? '∞' : data.profit_factor.toFixed(2)}</span>}
              />
              <Row
                label="میانگین R"
                value={
                  <span className={clsx('num', data.avg_r >= 0 ? 'text-profit' : 'text-loss')}>
                    {data.avg_r >= 0 ? '+' : ''}
                    {data.avg_r.toFixed(2)}
                  </span>
                }
              />
              <Row label="اوج حساب" value={<span className="num">{money(data.peak_equity)}</span>} />
              <Row label="سود شناور" value={<PnL value={data.unrealized_pnl} digits={3} />} />
              <Row
                label="نگاه بازار"
                value={
                  <Badge
                    tone={
                      data.market_stance === 'risk_on' ? 'profit' : data.market_stance === 'risk_off' ? 'loss' : 'neutral'
                    }
                  >
                    {STANCE_FA[data.market_stance] ?? data.market_stance}{' '}
                    <span className="num">×{data.risk_multiplier.toFixed(2)}</span>
                  </Badge>
                }
              />
            </dl>
          </Card>
        )}

        {/* ---- recent activity ---- */}
        <Card title="رویدادهای اخیر" padded={false} className="lg:col-span-3">
        {events.loading && !events.data ? (
          <SkeletonRows rows={4} className="px-4 sm:px-5 pb-4" />
        ) : !events.data || events.data.length === 0 ? (
          <Empty icon={<SignalIcon className="w-6 h-6" />}>هنوز رویدادی ثبت نشده است</Empty>
        ) : (
          <ul className="px-4 sm:px-5 pb-2">
            {events.data.map((event) => (
              <li key={event.id} className="flex gap-3 py-2.5 border-b border-ink-800 last:border-b-0">
                <Badge
                  tone={
                    event.level === 'error'
                      ? 'loss'
                      : event.level === 'warn'
                        ? 'warn'
                        : event.level === 'trade'
                          ? 'accent'
                          : 'neutral'
                  }
                  className="shrink-0 h-fit mt-0.5"
                >
                  {event.source}
                </Badge>
                <div className="min-w-0 grow">
                  <div className="text-sm text-slate-200 leading-6" dir="auto">
                    {event.message}
                  </div>
                  <div className="text-2xs text-slate-400">{timeAgo(event.created_at)}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
        </Card>
      </div>

      {/* ---- confirmations ---- */}
      <ConfirmDialog
        open={confirm?.kind === 'kill'}
        title="توقف اضطراری"
        danger
        confirmText="بله، همه چیز را متوقف کن"
        body="ربات غیرمسلح می‌شود، هیچ معامله جدیدی باز نمی‌کند و همه پوزیشن‌های باز با قیمت بازار بسته می‌شوند. برای شروع دوباره باید دستی مسلحش کنی."
        onConfirm={() => run(() => api.kill(), 'کلید توقف اضطراری فعال شد', overview.refresh)}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === 'flatten'}
        title="بستن همه پوزیشن‌ها"
        danger
        confirmText="همه را ببند"
        body="همه پوزیشن‌های باز با قیمت بازار بسته می‌شوند. ربات مسلح می‌ماند و می‌تواند دوباره وارد شود."
        onConfirm={() => run(() => api.command('flatten_all'), 'دستور بستن همه پوزیشن‌ها ارسال شد', positions.refresh)}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === 'close'}
        title={confirm?.kind === 'close' ? `بستن ${confirm.symbol}` : ''}
        danger
        confirmText="ببند"
        body="این پوزیشن با قیمت بازار بسته می‌شود."
        onConfirm={() =>
          confirm?.kind === 'close' &&
          run(() => api.command('close_position', confirm.id), `دستور بستن ${confirm.symbol} ارسال شد`, positions.refresh)
        }
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === 'arm'}
        title="مسلح کردن ربات"
        danger={data?.mode === 'live'}
        confirmText="مسلح کن"
        requirePhrase={data?.mode === 'live' ? 'ARM LIVE' : undefined}
        body={
          data?.mode === 'live' ? (
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
        onConfirm={() =>
          run(
            () => api.arm(true, data?.mode === 'live' ? 'ARM LIVE' : undefined),
            'ربات مسلح شد و شروع به معامله می‌کند',
            overview.refresh,
          )
        }
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === 'disarm'}
        title="غیرمسلح کردن ربات"
        confirmText="غیرمسلح کن"
        body="ربات دیگر معامله جدیدی باز نمی‌کند. پوزیشن‌های باز طبق حد ضرر و حد سود خودشان مدیریت می‌شوند."
        onConfirm={() => run(() => api.arm(false), 'ربات غیرمسلح شد', overview.refresh)}
        onCancel={() => setConfirm(null)}
      />
    </div>
  )
}

/* ---------------------------------------------------------------- hero */

function Hero({
  data,
  onArm,
  onDisarm,
  onKill,
  onResume,
}: {
  data: Overview
  onArm: () => void
  onDisarm: () => void
  onKill: () => void
  onResume: () => void
}) {
  const todayTone = data.today_pnl > 0 ? 'profit' : data.today_pnl < 0 ? 'loss' : 'neutral'
  const totalTone = data.total_pnl > 0 ? 'profit' : data.total_pnl < 0 ? 'loss' : 'neutral'
  const paused = Boolean(data.paused_until) || data.kill_switch
  // Optional Toman equivalent from the NetArz FX API (FX key in .env).
  const fx = usePolling(() => api.fx(), 300000)
  const toman = !quoteIsToman() && fx.data?.available && fx.data.mid ? data.equity * fx.data.mid : null

  const botState = data.kill_switch
    ? { label: 'اضطراری', tone: 'loss' as const }
    : data.paused_until
      ? { label: 'متوقف', tone: 'warn' as const }
      : data.armed
        ? { label: 'مسلح', tone: 'profit' as const }
        : { label: 'غیرمسلح', tone: 'warn' as const }

  return (
    <section className="card overflow-hidden">
      {/* annunciator strip: the four things that must be readable at a glance */}
      <div className="grid grid-cols-4 border-b border-ink-800 [&>*+*]:border-s [&>*+*]:border-ink-800">
        <Cell label="ربات" value={botState.label} tone={botState.tone} live={data.armed && !paused} />
        <Cell label="صرافی" value={data.exchange_connected ? 'متصل' : 'قطع'} tone={data.exchange_connected ? 'profit' : 'loss'} />
        <Cell label="استریم" value={data.stream_connected ? 'زنده' : 'خاموش'} tone={data.stream_connected ? 'profit' : 'neutral'} />
        <Cell
          label="حالت"
          value={data.mode === 'live' ? 'واقعی' : data.mode === 'testnet' ? 'تست‌نت' : 'آزمایشی'}
          tone={data.mode === 'live' ? 'loss' : 'neutral'}
          plain
        />
      </div>

      <div className="relative p-5 sm:p-6">
        <div
          className={clsx(
            'pointer-events-none absolute -top-24 -end-24 w-72 h-72 rounded-full blur-3xl opacity-25',
            todayTone === 'profit' ? 'bg-profit' : todayTone === 'loss' ? 'bg-loss' : 'bg-accent',
          )}
          aria-hidden="true"
        />
        <div className="relative">
          <div className="text-xs text-slate-400">ارزش کل حساب</div>
          <div className="num text-hero sm:text-[3rem] font-extrabold text-slate-50 mt-1">
            {money(data.equity)}
            <span className="text-base sm:text-lg font-semibold text-slate-400 ms-2">{quoteLabel()}</span>
          </div>
          {toman !== null && (
            <div className="text-xs text-slate-400 mt-1">
              ≈ <span className="num text-slate-300">{money(toman, 0)}</span> تومان
              <span className="opacity-70"> · نرخ {fx.data?.code} از نِت اَرز</span>
            </div>
          )}

          <div className="flex flex-wrap gap-2 mt-3">
            <Chip tone={todayTone}>
              <span className="opacity-80">امروز</span>{' '}
              <span className="num font-bold">
                {signed(data.today_pnl)} ({pct(data.today_pnl_pct)})
              </span>
            </Chip>
            <Chip tone={totalTone}>
              <span className="opacity-80">کل</span>{' '}
              <span className="num font-bold">
                {signed(data.total_pnl)} ({pct(data.total_pnl_pct)})
              </span>
            </Chip>
          </div>

          <div className="grid grid-cols-3 gap-3 mt-5 pt-4 border-t border-ink-800">
            <Mini
              label="افت از اوج"
              value={pct(-data.drawdown_pct)}
              tone={data.drawdown_pct > 10 ? 'loss' : data.drawdown_pct > 5 ? 'warn' : 'neutral'}
            />
            <Mini label="پوزیشن باز" value={`${data.open_positions} / ${data.max_positions}`} />
            <Mini label="موجودی آزاد" value={money(data.free_balance)} />
          </div>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-4 text-2xs text-slate-400">
            <span>
              سرمایه واریزی <span className="num text-slate-200">{money(data.capital_base)}</span>
            </span>
            <span>
              آخرین سینک <span className="text-slate-200">{timeAgo(data.last_sync)}</span>
            </span>
            <span>
              <span className="num">{data.trades_today}</span> معامله امروز
            </span>
            {data.strategy_core === 'portfolio4h' && (
              <span>ارزیابی بعدی <span className="num text-slate-200">{new Date(Math.ceil(Date.now() / 14_400_000) * 14_400_000).toLocaleTimeString('fa-IR-u-nu-latn', { timeZone: DISPLAY_TZ, hour: '2-digit', minute: '2-digit' })}</span></span>
            )}
            {data.paused_until && (
              <span className="text-warn">
                تا <span className="num">{datetime(data.paused_until)}</span> متوقف
              </span>
            )}
            <span className="hidden sm:inline text-slate-400">{MODE_FA[data.mode] ?? data.mode}</span>
          </div>
        </div>
      </div>

      {/* thumb-reachable controls */}
      <div className="border-t border-ink-800 p-3 grid grid-cols-2 gap-2">
        {paused ? (
          <button className="btn-ghost" onClick={onResume}>
            رفع توقف
          </button>
        ) : data.armed ? (
          <button className="btn-ghost" onClick={onDisarm}>
            غیرمسلح کن
          </button>
        ) : (
          <button className="btn-success" onClick={onArm}>
            مسلح کن
          </button>
        )}
        <button className="btn-danger-soft" onClick={onKill} disabled={data.kill_switch}>
          <StopIcon className="w-5 h-5" />
          توقف اضطراری
        </button>
      </div>
    </section>
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
  tone: 'profit' | 'loss' | 'warn' | 'neutral'
  live?: boolean
  plain?: boolean
}) {
  const color = {
    profit: 'text-profit',
    loss: 'text-loss',
    warn: 'text-warn',
    neutral: 'text-slate-300',
  }[tone]
  return (
    <div className="px-2 py-2.5 text-center min-w-0">
      <div className="text-2xs text-slate-400 leading-4">{label}</div>
      <div className={clsx('flex items-center justify-center gap-1.5 text-xs sm:text-sm font-semibold leading-5 truncate', color)}>
        {!plain && (
          <span
            className={clsx(
              'inline-block w-1.5 h-1.5 rounded-full shrink-0',
              tone === 'profit' ? 'bg-profit' : tone === 'loss' ? 'bg-loss' : tone === 'warn' ? 'bg-warn' : 'bg-slate-500',
              live && 'animate-breathe',
            )}
          />
        )}
        <span className="truncate">{value}</span>
      </div>
    </div>
  )
}

function Chip({ tone, children }: { tone: 'profit' | 'loss' | 'neutral'; children: React.ReactNode }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-lg px-2.5 py-1 text-xs',
        tone === 'profit'
          ? 'bg-profit/15 text-profit'
          : tone === 'loss'
            ? 'bg-loss/15 text-loss'
            : 'bg-slate-400/15 text-slate-200',
      )}
    >
      {children}
    </span>
  )
}

function Mini({ label, value, tone = 'neutral' }: { label: string; value: string; tone?: 'profit' | 'loss' | 'warn' | 'neutral' }) {
  const color = { profit: 'text-profit', loss: 'text-loss', warn: 'text-warn', neutral: 'text-slate-100' }[tone]
  return (
    <div className="min-w-0">
      <div className="text-2xs text-slate-400 leading-4">{label}</div>
      <div className={clsx('num text-base font-semibold leading-6 truncate', color)}>{value}</div>
    </div>
  )
}

/* ---------------------------------------------------------------- positions */

function computeUnrealized(position: Position, liveMark?: number) {
  const mark = liveMark ?? position.mark_price ?? position.entry_price
  const direction = position.side === 'long' ? 1 : -1
  const unrealized = (mark - position.entry_price) * position.qty * direction
  const basis = position.entry_price * position.qty
  return { mark, unrealized, unrealizedPct: basis ? (unrealized / basis) * 100 : 0 }
}

/** Peak open gain and give-back as sent by the API (absent on older payloads). */
function peakOf(position: Position) {
  return { peakPct: position.peak_pct ?? 0, givebackPct: position.giveback_pct ?? 0 }
}

/** "حد سود" — the 4h families carry no target, so a zero target reads as "—" rather than 0.0000. */
function targetText(position: Position) {
  return position.target_price ? price(position.target_price) : '—'
}

function PositionCard({ position, mark: liveMark, onClose }: { position: Position; mark?: number; onClose: () => void }) {
  const { mark, unrealized, unrealizedPct } = computeUnrealized(position, liveMark)
  const { peakPct } = peakOf(position)
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
          <div className="text-sm font-semibold text-slate-50 flex items-center gap-1.5">
            {position.symbol}
            {position.trail_active && <Badge tone="accent">تریل</Badge>}
            {position.pending_exit_reason && <Badge tone="warn">در حال فروش</Badge>}
            {position.stop_missing && <Badge tone="loss">بدون استاپ</Badge>}
          </div>
          <div className="text-2xs text-slate-400">
            حجم <span className="num">{qty(position.qty)}</span> · {timeAgo(position.opened_at)}
          </div>
        </div>
        <PnL value={unrealized} percent={unrealizedPct} digits={3} className="text-base shrink-0" />
      </div>
      <div className="grid grid-cols-4 gap-2 mt-3">
        <KV label="ورود" value={price(position.entry_price)} />
        <KV label="فعلی" value={price(mark)} valueClass="text-slate-50 font-semibold" />
        <KV label="حد ضرر" value={price(position.stop_price)} valueClass="text-loss" />
        <KV label="حد سود" value={targetText(position)} valueClass={position.target_price ? 'text-profit' : 'text-slate-500'} />
        {/* Second row: the best open gain the trade reached. Informational only -
            it is not a loss, and the rules do not act on it. */}
        <KV
          label="بیشترین سود باز تاکنون"
          className="col-span-2"
          value={pct(peakPct)}
          valueClass="text-slate-300"
        />
      </div>
      <button className="btn-ghost btn-sm w-full mt-3 text-loss" onClick={onClose}>
        بستن این پوزیشن
      </button>
    </li>
  )
}

function PositionsTable({
  rows,
  marks,
  onClose,
}: {
  rows: Position[]
  marks: Record<string, number>
  onClose: (id: number, symbol: string) => void
}) {
  return (
    <table className="w-full min-w-[780px]">
      <thead>
        <tr className="border-b border-ink-700">
          <th className="th ps-5">نماد</th>
          <th className="th">جهت</th>
          <th className="th">حجم</th>
          <th className="th">ورود</th>
          <th className="th">قیمت فعلی</th>
          <th className="th">حد ضرر</th>
          <th className="th">حد سود</th>
          <th className="th">سود شناور</th>
          <th className="th">بیشترین سود باز</th>
          <th className="th"></th>
        </tr>
      </thead>
      <tbody className="divide-y divide-ink-800">
        {rows.map((position) => {
          const { mark, unrealized, unrealizedPct } = computeUnrealized(position, marks[position.symbol])
          const { peakPct } = peakOf(position)
          return (
            <tr key={position.id} className="hover:bg-ink-850/60">
              <td className="td ps-5 font-semibold text-slate-50">{position.symbol}</td>
              <td className="td">
                <Badge tone={position.side === 'long' ? 'profit' : 'loss'}>{SIDE_FA[position.side] ?? position.side}</Badge>
              </td>
              <td className="td num text-slate-300">{qty(position.qty)}</td>
              <td className="td num text-slate-300">{price(position.entry_price)}</td>
              <td className="td num text-slate-50">{price(mark)}</td>
              <td className="td num text-loss">
                {price(position.stop_price)}
                {position.trail_active && <span className="text-2xs text-accent ms-1">تریل</span>}
              </td>
              <td className={clsx('td num', position.target_price ? 'text-profit' : 'text-slate-500')}>{targetText(position)}</td>
              <td className="td">
                <PnL value={unrealized} percent={unrealizedPct} digits={3} />
              </td>
              <td className="td num whitespace-nowrap">
                <span className="text-slate-300">{pct(peakPct)}</span>
              </td>
              <td className="td">
                <button className="btn-ghost btn-sm text-loss" onClick={() => onClose(position.id, position.symbol)}>
                  بستن
                </button>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/* ---------------------------------------------------------------- skeleton */

function DashboardSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true">
      <div className="card overflow-hidden">
        <div className="grid grid-cols-4 border-b border-ink-800 [&>*+*]:border-s [&>*+*]:border-ink-800">
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="px-3 py-2.5 space-y-1.5">
              <Skeleton className="h-3 w-10 mx-auto" />
              <Skeleton className="h-4 w-14 mx-auto" />
            </div>
          ))}
        </div>
        <div className="p-5 space-y-3">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-10 w-56" />
          <div className="flex gap-2">
            <Skeleton className="h-7 w-32" />
            <Skeleton className="h-7 w-28" />
          </div>
          <div className="grid grid-cols-3 gap-3 pt-3">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        </div>
        <div className="border-t border-ink-800 p-3 grid grid-cols-2 gap-2">
          <Skeleton className="h-11" />
          <Skeleton className="h-11" />
        </div>
      </div>
      <div className="card p-4">
        <Skeleton className="h-4 w-28 mb-4" />
        <SkeletonRows rows={2} />
      </div>
      <div className="card p-4">
        <Skeleton className="h-4 w-24 mb-4" />
        <Skeleton className="h-44 w-full" />
      </div>
    </div>
  )
}
