import { useMemo, useState } from 'react'
import clsx from 'clsx'
import { api } from '@/lib/api'
import { REASON_FA, SIDE_FA, clock, money, price, qty, signed, quoteLabel } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import {
  Badge,
  Card,
  Empty,
  ErrorBanner,
  ListRow,
  PnL,
  Segmented,
  SkeletonRows,
  SkeletonStats,
  Stat,
} from '@/components/ui'
import { TradesIcon } from '@/components/icons'

type Tab = 'trades' | 'orders' | 'events'

const TABS: Array<{ value: Tab; label: string }> = [
  { value: 'trades', label: 'معاملات' },
  { value: 'orders', label: 'سفارش‌ها' },
  { value: 'events', label: 'لاگ سیستم' },
]

const PERIODS = [
  { value: 7, label: '۷ روز' },
  { value: 30, label: '۳۰ روز' },
  { value: 90, label: '۹۰ روز' },
  { value: 365, label: '۱ سال' },
]

export default function Trades() {
  const [tab, setTab] = useState<Tab>('trades')
  const [days, setDays] = useState(30)

  const closed = usePolling(() => api.positions('closed', 300), 15000)
  const orders = usePolling(() => api.orders(200), 20000)
  const events = usePolling(() => api.events(300), 15000)
  const stats = usePolling(() => api.stats(days), 30000, [days])

  const symbolRows = useMemo(() => {
    const bySymbol = stats.data?.by_symbol ?? {}
    return Object.entries(bySymbol)
      .map(([symbol, value]: [string, any]) => ({ symbol, ...value }))
      .sort((a, b) => b.pnl - a.pnl)
  }, [stats.data])

  const reasonRows = useMemo(() => {
    const byReason = stats.data?.by_exit_reason ?? {}
    return Object.entries(byReason)
      .map(([reason, value]: [string, any]) => ({ reason, ...value }))
      .sort((a, b) => b.count - a.count)
  }, [stats.data])

  const overall = stats.data?.overall

  return (
    <div className="space-y-4">
      <ErrorBanner message={closed.error} />

      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-slate-200">خلاصه دوره</h2>
        <Segmented value={days} onChange={setDays} options={PERIODS} />
      </div>

      {stats.loading && !stats.data ? (
        <SkeletonStats />
      ) : overall ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Stat
            label="سود خالص"
            value={`${overall.net_pnl >= 0 ? '+' : '−'}${money(Math.abs(overall.net_pnl))}`}
            tone={overall.net_pnl > 0 ? 'profit' : overall.net_pnl < 0 ? 'loss' : 'neutral'}
            sub={
              <>
                کارمزد پرداختی <span className="num">{money(overall.fees)} {quoteLabel()}</span>
              </>
            }
          />
          <Stat label="معاملات بسته‌شده" value={overall.trades} sub={`${overall.wins} برد / ${overall.losses} باخت`} />
          <Stat label="نرخ برد" value={`${(overall.win_rate * 100).toFixed(1)}%`} tone={overall.win_rate >= 0.5 ? 'profit' : 'neutral'} />
          <Stat
            label="بهترین معامله"
            value={signed(overall.best)}
            tone={overall.best > 0 ? 'profit' : 'neutral'}
            sub={
              <>
                بدترین <span className="num text-loss">{signed(overall.worst)}</span>
              </>
            }
          />
        </div>
      ) : null}

      <div className="grid md:grid-cols-2 gap-4">
        <Card title="عملکرد به تفکیک نماد">
          {symbolRows.length === 0 ? (
            <Empty>در این دوره معامله‌ای بسته نشده است</Empty>
          ) : (
            <div className="space-y-3">
              {symbolRows.map((row) => (
                <div key={row.symbol} className="text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-medium text-slate-50">{row.symbol}</span>
                    <PnL value={row.pnl} digits={3} />
                  </div>
                  <div className="flex items-center gap-3 mt-1.5">
                    <div className="grow h-1.5 rounded-full bg-ink-800 overflow-hidden" dir="ltr">
                      <div
                        className="h-full bg-accent"
                        style={{ width: `${row.trades ? (row.wins / row.trades) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="num text-2xs text-slate-400 shrink-0 w-16 text-left">
                      {row.wins}/{row.trades} برد
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="دلیل بسته شدن معاملات">
          {reasonRows.length === 0 ? (
            <Empty>داده‌ای برای این دوره نیست</Empty>
          ) : (
            <dl>
              {reasonRows.map((row) => (
                <div
                  key={row.reason}
                  className="flex items-center justify-between gap-3 py-2.5 border-b border-ink-800 last:border-b-0 text-sm"
                >
                  <dt className="text-slate-200">{REASON_FA[row.reason] ?? row.reason}</dt>
                  <dd className="flex items-center gap-3">
                    <span className="num text-2xs text-slate-400">{row.count} بار</span>
                    <PnL value={row.pnl} digits={3} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </Card>
      </div>

      <Card
        padded={false}
        title={<Segmented value={tab} onChange={setTab} options={TABS} />}
      >
        {tab === 'trades' && <TradesList rows={closed.data} loading={closed.loading} />}
        {tab === 'orders' && <OrdersList rows={orders.data} loading={orders.loading} />}
        {tab === 'events' && <EventsList rows={events.data} loading={events.loading} />}
      </Card>
    </div>
  )
}

/* ---------------------------------------------------------------- trades */

function TradesList({ rows, loading }: { rows: any[] | null; loading: boolean }) {
  if (loading && !rows) return <SkeletonRows rows={5} className="px-4 sm:px-5 pb-4" />
  const closed = (rows ?? []).filter((row) => row.status === 'closed')
  if (closed.length === 0) {
    return <Empty icon={<TradesIcon className="w-6 h-6" />}>هنوز معامله‌ای بسته نشده است</Empty>
  }

  return (
    <>
      <ul className="lg:hidden">
        {closed.map((row) => (
          <ListRow key={row.id} as="li">
            <SideBlock side={row.side} />
            <div className="grow min-w-0">
              <div className="text-sm font-semibold text-slate-50 flex items-center gap-2">
                {row.symbol}
                <span className="text-2xs font-normal text-slate-400 truncate">
                  {REASON_FA[row.exit_reason ?? ''] ?? row.exit_reason ?? ''}
                </span>
              </div>
              <div className="text-2xs text-slate-400 mt-0.5">
                <span className="num">
                  {price(row.entry_price)} → {price(row.exit_price)}
                </span>
                <span className="mx-1.5">·</span>
                <span className="num">{clock(row.closed_at)}</span>
              </div>
            </div>
            <div className="text-left shrink-0">
              <PnL value={row.realized_pnl} percent={row.realized_pnl_pct} digits={3} />
              <div className={clsx('num text-2xs mt-0.5', row.r_multiple >= 0 ? 'text-profit/80' : 'text-loss/80')}>
                R {row.r_multiple >= 0 ? '+' : ''}
                {row.r_multiple.toFixed(2)}
              </div>
            </div>
          </ListRow>
        ))}
      </ul>

      <div className="hidden lg:block overflow-x-auto pb-2">
        <table className="w-full min-w-[820px]">
          <thead>
            <tr className="border-b border-ink-700">
              <th className="th ps-5">زمان بستن</th>
              <th className="th">نماد</th>
              <th className="th">جهت</th>
              <th className="th">حجم</th>
              <th className="th">ورود</th>
              <th className="th">خروج</th>
              <th className="th">سود</th>
              <th className="th">R</th>
              <th className="th">دلیل</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-800">
            {closed.map((row) => (
              <tr key={row.id} className="hover:bg-ink-850/60">
                <td className="td ps-5 text-slate-400 num text-xs">{clock(row.closed_at)}</td>
                <td className="td text-slate-50 font-medium">{row.symbol}</td>
                <td className="td">
                  <Badge tone={row.side === 'long' ? 'profit' : 'loss'}>{SIDE_FA[row.side] ?? row.side}</Badge>
                </td>
                <td className="td num text-slate-300">{qty(row.qty)}</td>
                <td className="td num text-slate-300">{price(row.entry_price)}</td>
                <td className="td num text-slate-300">{price(row.exit_price)}</td>
                <td className="td">
                  <PnL value={row.realized_pnl} percent={row.realized_pnl_pct} digits={3} />
                </td>
                <td className={clsx('td num', row.r_multiple >= 0 ? 'text-profit' : 'text-loss')}>
                  {row.r_multiple >= 0 ? '+' : ''}
                  {row.r_multiple.toFixed(2)}
                </td>
                <td className="td text-xs text-slate-400">{REASON_FA[row.exit_reason ?? ''] ?? row.exit_reason ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

/* ---------------------------------------------------------------- orders */

const INTENT_FA: Record<string, string> = { entry: 'ورود', exit: 'خروج', stop: 'حد ضرر' }

function OrdersList({ rows, loading }: { rows: any[] | null; loading: boolean }) {
  if (loading && !rows) return <SkeletonRows rows={5} className="px-4 sm:px-5 pb-4" />
  if (!rows || rows.length === 0) return <Empty>هنوز سفارشی ثبت نشده است</Empty>

  return (
    <>
      <ul className="lg:hidden">
        {rows.map((row) => (
          <ListRow key={row.id} as="li">
            <span
              className={clsx(
                'w-10 h-10 rounded-xl flex items-center justify-center text-2xs font-bold shrink-0 num',
                row.side === 'BUY' ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss',
              )}
            >
              {row.side}
            </span>
            <div className="grow min-w-0">
              <div className="text-sm font-semibold text-slate-50 flex items-center gap-2">
                {row.symbol}
                <span className="text-2xs font-normal text-slate-400">
                  {INTENT_FA[row.intent] ?? row.intent} · <span className="num">{row.type}</span>
                </span>
              </div>
              <div className="text-2xs text-slate-400 mt-0.5">
                <span className="num">
                  {qty(row.filled_qty)} @ {price(row.avg_price)}
                </span>
                <span className="mx-1.5">·</span>
                <span className="num">{clock(row.created_at)}</span>
              </div>
              {row.error && (
                <div className="text-2xs text-loss mt-1 leading-4 break-words" dir="auto">
                  {row.error}
                </div>
              )}
            </div>
            <div className="text-left shrink-0">
              <Badge tone={row.status === 'FILLED' ? 'profit' : row.status === 'REJECTED' ? 'loss' : 'neutral'}>
                {row.status}
              </Badge>
              {row.fee != null && <div className="num text-2xs text-slate-400 mt-1">کارمزد {Number(row.fee).toFixed(5)}</div>}
            </div>
          </ListRow>
        ))}
      </ul>

      <div className="hidden lg:block overflow-x-auto pb-2">
        <table className="w-full min-w-[760px]">
          <thead>
            <tr className="border-b border-ink-700">
              <th className="th ps-5">زمان</th>
              <th className="th">نماد</th>
              <th className="th">سمت</th>
              <th className="th">نوع</th>
              <th className="th">هدف</th>
              <th className="th">حجم پرشده</th>
              <th className="th">قیمت</th>
              <th className="th">کارمزد</th>
              <th className="th">وضعیت</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-800">
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-ink-850/60">
                <td className="td ps-5 text-slate-400 num text-xs">{clock(row.created_at)}</td>
                <td className="td text-slate-50 font-medium">{row.symbol}</td>
                <td className="td">
                  <Badge tone={row.side === 'BUY' ? 'profit' : 'loss'}>{row.side}</Badge>
                </td>
                <td className="td text-xs text-slate-300 num">{row.type}</td>
                <td className="td text-xs text-slate-400">{INTENT_FA[row.intent] ?? row.intent}</td>
                <td className="td num text-slate-300">{qty(row.filled_qty)}</td>
                <td className="td num text-slate-200">{price(row.avg_price)}</td>
                <td className="td num text-slate-400">{row.fee?.toFixed(5) ?? '—'}</td>
                <td className="td">
                  <Badge tone={row.status === 'FILLED' ? 'profit' : row.status === 'REJECTED' ? 'loss' : 'neutral'}>
                    {row.status}
                  </Badge>
                  {row.error && <div className="text-2xs text-loss mt-1 max-w-[16rem] truncate">{row.error}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

/* ---------------------------------------------------------------- events */

function EventsList({ rows, loading }: { rows: any[] | null; loading: boolean }) {
  if (loading && !rows) return <SkeletonRows rows={6} className="px-4 sm:px-5 pb-4" />
  if (!rows || rows.length === 0) return <Empty>لاگی ثبت نشده است</Empty>

  return (
    <ul className="px-4 sm:px-5 pb-2 max-h-[36rem] overflow-y-auto">
      {rows.map((row) => (
        <li key={row.id} className="flex gap-3 py-2.5 border-b border-ink-800 last:border-b-0">
          <Badge
            tone={row.level === 'error' ? 'loss' : row.level === 'warn' ? 'warn' : row.level === 'trade' ? 'accent' : 'neutral'}
            className="shrink-0 h-fit mt-0.5"
          >
            {row.source}
          </Badge>
          <div className="min-w-0 grow">
            <div className="text-sm text-slate-200 leading-6 break-words" dir="auto">
              {row.message}
            </div>
            <div className="text-2xs text-slate-400 num">{clock(row.created_at)}</div>
          </div>
        </li>
      ))}
    </ul>
  )
}

function SideBlock({ side }: { side: string }) {
  const long = side === 'long'
  return (
    <span
      className={clsx(
        'w-10 h-10 rounded-xl flex items-center justify-center text-2xs font-bold shrink-0',
        long ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss',
      )}
    >
      {SIDE_FA[side] ?? side}
    </span>
  )
}
