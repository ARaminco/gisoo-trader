import { FormEvent, useState } from 'react'
import clsx from 'clsx'
import { api } from '@/lib/api'
import { clock, money, timeAgo, quoteLabel } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import {
  Badge,
  Card,
  Dot,
  Empty,
  ErrorBanner,
  ListRow,
  Notice,
  Row,
  Segmented,
  SkeletonRows,
  SkeletonStats,
  Spinner,
  Stat,
  toast,
} from '@/components/ui'
import { WalletIcon } from '@/components/icons'

const SOURCE_FA: Record<string, string> = {
  manual: 'ثبت دستی',
  initial: 'سرمایه اولیه',
}

export default function Capital() {
  const [amount, setAmount] = useState('')
  const [kind, setKind] = useState<'deposit' | 'withdrawal'>('deposit')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const flows = usePolling(() => api.capital(), 15000)
  const sync = usePolling(() => api.sync(), 8000)
  const overview = usePolling(() => api.overview(), 8000)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const value = Number(amount)
    if (!Number.isFinite(value) || value <= 0) {
      toast('مبلغ معتبر وارد کن', 'error')
      return
    }
    setBusy(true)
    try {
      await api.addCapital(kind, value, note || undefined)
      toast(kind === 'deposit' ? `واریز ${value} ${quoteLabel()} ثبت شد` : `برداشت ${value} ${quoteLabel()} ثبت شد`)
      setAmount('')
      setNote('')
      flows.refresh()
      overview.refresh()
    } catch (error) {
      toast(error instanceof Error ? error.message : 'خطا', 'error')
    } finally {
      setBusy(false)
    }
  }

  const balances = sync.data?.balances ?? {}
  const balanceRows = Object.entries(balances)
    .filter(([, value]) => value > 0.00001)
    .sort((a, b) => b[1] - a[1])

  return (
    <div className="space-y-4">
      <ErrorBanner message={flows.error ?? sync.error} />

      {overview.loading && !overview.data ? (
        <SkeletonStats />
      ) : overview.data ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Stat label="سرمایه واریزی" value={money(overview.data.capital_base)} sub="مبنای محاسبه درصد سود" />
          <Stat label="ارزش فعلی حساب" value={money(overview.data.equity)} sub={quoteLabel()} />
          <Stat
            label="سود خالص"
            value={`${overview.data.total_pnl >= 0 ? '+' : '−'}${money(Math.abs(overview.data.total_pnl))}`}
            tone={overview.data.total_pnl > 0 ? 'profit' : overview.data.total_pnl < 0 ? 'loss' : 'neutral'}
            sub={`${overview.data.total_pnl_pct >= 0 ? '+' : '−'}${Math.abs(overview.data.total_pnl_pct).toFixed(2)}%`}
          />
          <Stat label="موجودی آزاد" value={money(overview.data.free_balance)} sub="قابل استفاده برای ورود" />
        </div>
      ) : null}

      <div className="grid md:grid-cols-2 gap-4 items-start">
        {/* ---- record a capital change ---- */}
        <Card title="ثبت واریز یا برداشت">
          <Notice tone="info" className="mb-4">
            انتقال واقعی پول را در خود صرافی انجام بده. ثبت آن اینجا فقط برای این است که درصد سود و افت سرمایه درست حساب شود؛
            وگرنه واریز جدید «سود» شمرده می‌شود. اگر یادت رفت، ربات هنگام سینک تفاوت موجودی را خودش ثبت می‌کند.
          </Notice>

          <form onSubmit={submit} className="space-y-4">
            <Segmented
              size="md"
              className="w-full"
              value={kind}
              onChange={setKind}
              options={[
                { value: 'deposit', label: 'واریز' },
                { value: 'withdrawal', label: 'برداشت' },
              ]}
            />

            <div>
              <label className="label" htmlFor="capital-amount">
                مبلغ ({quoteLabel()})
              </label>
              <input
                id="capital-amount"
                className="input num text-lg"
                inputMode="decimal"
                dir="ltr"
                placeholder="100"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
              />
              <div className="flex gap-1.5 mt-2 overflow-x-auto no-scrollbar -mx-1 px-1">
                {[50, 100, 250, 500, 1000].map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    className={clsx(
                      'num min-h-[36px] px-3 rounded-lg text-xs font-medium border shrink-0 transition-colors',
                      amount === String(preset)
                        ? 'bg-accent/15 border-accent/40 text-accent'
                        : 'bg-ink-850 border-ink-700 text-slate-300 hover:text-slate-50',
                    )}
                    onClick={() => setAmount(String(preset))}
                  >
                    {preset}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="label" htmlFor="capital-note">
                توضیح (اختیاری)
              </label>
              <input
                id="capital-note"
                className="input"
                placeholder="مثلاً: افزایش سرمایه شهریور"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
            </div>

            <button
              className={clsx('w-full min-h-[48px]', kind === 'deposit' ? 'btn-success' : 'btn-danger')}
              disabled={busy || !amount}
            >
              {busy ? <Spinner className="w-5 h-5" /> : kind === 'deposit' ? 'ثبت واریز' : 'ثبت برداشت'}
            </button>
          </form>
        </Card>

        {/* ---- live exchange state ---- */}
        <Card
          title="اتصال به صرافی"
          action={
            sync.data && (
              <span className="flex items-center gap-2 text-2xs">
                <Dot ok={sync.data.connected} live />
                <span className={sync.data.connected ? 'text-profit' : 'text-loss'}>
                  {sync.data.connected ? 'متصل' : 'قطع'}
                </span>
              </span>
            )
          }
        >
          {!sync.data ? (
            <SkeletonRows rows={4} />
          ) : (
            <div className="space-y-4">
              <dl>
                <Row
                  label="استریم لحظه‌ای حساب"
                  value={
                    <Badge tone={sync.data.stream_connected ? 'profit' : 'warn'}>
                      {sync.data.stream_connected ? 'فعال' : 'غیرفعال'}
                    </Badge>
                  }
                />
                <Row label="آخرین سینک کامل" value={timeAgo(sync.data.last_rest_sync)} />
                <Row label="آخرین رویداد حساب" value={timeAgo(sync.data.last_stream_event)} />
                <Row label="اختلاف ساعت با سرور" value={<span className="num">{sync.data.server_time_offset_ms} ms</span>} />
                <Row label="سفارش‌های باز روی صرافی" value={<span className="num">{sync.data.open_orders}</span>} />
              </dl>

              {sync.data.last_error && (
                <Notice tone="danger">
                  <span dir="auto">{sync.data.last_error}</span>
                </Notice>
              )}

              {Object.keys(sync.data.drift ?? {}).length > 0 && (
                <div className="rounded-xl bg-warn/10 border border-warn/30 px-3 py-2.5">
                  <div className="text-xs font-medium text-warn mb-1.5">مغایرت‌هایی که اصلاح شد</div>
                  <pre className="text-2xs text-slate-300 whitespace-pre-wrap num leading-5" dir="ltr">
                    {JSON.stringify(sync.data.drift, null, 2)}
                  </pre>
                </div>
              )}

              <div>
                <div className="text-xs font-medium text-slate-300 mb-1">موجودی واقعی روی صرافی</div>
                {balanceRows.length === 0 ? (
                  <div className="text-xs text-slate-400 leading-5">
                    {sync.data.mode === 'paper'
                      ? 'در حالت آزمایشی موجودی از صرافی خوانده نمی‌شود.'
                      : 'موجودی‌ای گزارش نشده است.'}
                  </div>
                ) : (
                  <dl>
                    {balanceRows.map(([asset, value]) => (
                      <Row
                        key={asset}
                        label={<span className="num font-medium text-slate-200">{asset}</span>}
                        value={<span className="num">{value.toFixed(8).replace(/\.?0+$/, '')}</span>}
                      />
                    ))}
                  </dl>
                )}
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* ---- flow history ---- */}
      <Card title="تاریخچه سرمایه" padded={false}>
        {flows.loading && !flows.data ? (
          <SkeletonRows rows={3} className="px-4 sm:px-5 pb-4" />
        ) : !flows.data || flows.data.length === 0 ? (
          <Empty icon={<WalletIcon className="w-6 h-6" />}>هنوز واریز یا برداشتی ثبت نشده است</Empty>
        ) : (
          <ul>
            {flows.data.map((flow) => {
              const deposit = flow.kind === 'deposit'
              return (
                <ListRow key={flow.id} as="li">
                  <span
                    className={clsx(
                      'w-10 h-10 rounded-xl flex items-center justify-center text-lg font-bold shrink-0 num',
                      deposit ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss',
                    )}
                  >
                    {deposit ? '+' : '−'}
                  </span>
                  <div className="grow min-w-0">
                    <div className="text-sm font-medium text-slate-50">
                      {deposit ? 'واریز' : 'برداشت'}
                      <span className="text-2xs text-slate-400 font-normal ms-2">
                        {SOURCE_FA[flow.source] ?? 'تشخیص خودکار'}
                      </span>
                    </div>
                    <div className="text-2xs text-slate-400 mt-0.5 truncate">
                      <span className="num">{clock(flow.occurred_at)}</span>
                      {flow.note && (
                        <>
                          <span className="mx-1.5">·</span>
                          {flow.note}
                        </>
                      )}
                    </div>
                  </div>
                  <span className={clsx('num text-base font-semibold shrink-0', deposit ? 'text-profit' : 'text-loss')}>
                    {deposit ? '+' : '−'}
                    {money(flow.amount)} <span className="text-2xs text-slate-400 font-normal">{flow.asset}</span>
                  </span>
                </ListRow>
              )
            })}
          </ul>
        )}
      </Card>
    </div>
  )
}
