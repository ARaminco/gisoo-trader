import { useState } from 'react'
import clsx from 'clsx'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, BacktestResult } from '@/lib/api'
import { FAMILY_FA, REASON_FA, money, price, quoteLabel } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import { Badge, Card, Empty, ErrorBanner, KV, ListRow, Notice, Skeleton, Spinner, Stat, toast } from '@/components/ui'
import { FlaskIcon } from '@/components/icons'

export default function Backtest() {
  const settings = usePolling(() => api.settings(), 60000)
  const [symbol, setSymbol] = useState('')
  const [candles, setCandles] = useState(1500)
  const [capital, setCapital] = useState(100)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const symbols: string[] = settings.data?.effective?.symbols ?? []
  const core4h = (settings.data?.static?.strategy_core ?? 'portfolio4h') === 'portfolio4h'
  const universe: string[] = settings.data?.static?.universe ?? []
  // The 4h core backtests the whole universe as one portfolio; the symbol
  // field is only meaningful for the per-symbol ensemble backtester.
  const chosen = core4h ? 'PORTFOLIO' : symbol || symbols[0] || ''

  async function run() {
    if (!chosen) {
      toast('یک نماد انتخاب کن', 'error')
      return
    }
    setRunning(true)
    setError(null)
    try {
      const response = await api.backtest(chosen, candles, capital)
      setResult(response)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'بک‌تست اجرا نشد')
      setResult(null)
    } finally {
      setRunning(false)
    }
  }

  const curve = (result?.equity_curve ?? []).map((value, index) => ({ i: index, equity: value }))

  return (
    <div className="space-y-4">
      <Card title="بک‌تست استراتژی">
        {core4h ? (
          <p className="text-xs text-slate-400 leading-6 mb-4">
            شبیه‌ساز پژوهشی هسته ۴ساعته روی کندل‌های ذخیره‌شده‌ی کل نمادها (
            <span className="num">{universe.length}</span> نماد) به‌صورت یک پرتفوی اجرا می‌شود: هر دو خانواده (مومنتوم
            مقطعی و شکست کانال) با همان اندازه‌گیری اعتبارسنجی‌شده، با احتساب کارمزد و لغزش قیمت. تعداد کندل، تعداد کندل
            ۴ساعته‌ی هر نماد است (۲۶۰ کندل اول برای گرم‌شدن اندیکاتورها).
          </p>
        ) : (
          <p className="text-xs text-slate-400 leading-6 mb-4">
            همان استراتژی زنده روی کندل‌های ذخیره‌شده اجرا می‌شود، با احتساب کارمزد و لغزش قیمت. لایه یادگیری ماشین عمداً در
            بک‌تست خاموش است، چون مدل روی همین داده آموزش دیده و نتیجه را خوش‌بین می‌کند.
          </p>
        )}

        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 items-end">
          <div>
            <label className="label" htmlFor="bt-symbol">
              نماد
            </label>
            {core4h ? (
              <div className="input num flex items-center text-slate-300" dir="ltr">
                PORTFOLIO · {universe.length} symbols
              </div>
            ) : (
              <select id="bt-symbol" className="input num" value={chosen} onChange={(event) => setSymbol(event.target.value)}>
                {symbols.length === 0 && <option value="">در حال بارگذاری…</option>}
                {symbols.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div>
            <label className="label" htmlFor="bt-candles">
              تعداد کندل
            </label>
            <input
              id="bt-candles"
              className="input num"
              type="number"
              inputMode="numeric"
              dir="ltr"
              step={100}
              min={300}
              max={20000}
              value={candles}
              onChange={(event) => setCandles(Number(event.target.value))}
            />
          </div>
          <div>
            <label className="label" htmlFor="bt-capital">
              سرمایه اولیه ({quoteLabel()})
            </label>
            <input
              id="bt-capital"
              className="input num"
              type="number"
              inputMode="decimal"
              dir="ltr"
              step={10}
              min={10}
              value={capital}
              onChange={(event) => setCapital(Number(event.target.value))}
            />
          </div>
          <button className="btn-primary min-h-[48px] sm:min-h-[44px]" onClick={run} disabled={running || !chosen}>
            {running ? <Spinner className="w-4 h-4 border-ink-950/40 border-t-ink-950" /> : 'اجرای بک‌تست'}
          </button>
        </div>
      </Card>

      <ErrorBanner message={error} />

      {running && (
        <div className="space-y-4" aria-busy="true">
          <div className="card p-4 flex items-center gap-3">
            <Spinner className="w-6 h-6 shrink-0" />
            <span className="text-sm text-slate-300 leading-6">
              در حال شبیه‌سازی <span className="num">{candles}</span> کندل روی <span className="num">{chosen}</span>… چند
              ثانیه طول می‌کشد.
            </span>
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="card p-4 space-y-2.5">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-6 w-24" />
              </div>
            ))}
          </div>
          <div className="card p-4">
            <Skeleton className="h-48 w-full" />
          </div>
        </div>
      )}

      {!result && !running && !error && (
        <Card>
          <Empty icon={<FlaskIcon className="w-6 h-6" />}>
            نماد و بازه را انتخاب کن و بک‌تست را اجرا کن. نتیجه شامل سود خالص، نرخ برد، بیشترین افت و لیست معاملات
            شبیه‌سازی‌شده است.
          </Empty>
        </Card>
      )}

      {result && !running && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <Stat
              label="سود خالص"
              value={`${result.net_pnl >= 0 ? '+' : '−'}${money(Math.abs(result.net_pnl))}`}
              tone={result.net_pnl > 0 ? 'profit' : result.net_pnl < 0 ? 'loss' : 'neutral'}
              sub={`${result.net_pnl_pct >= 0 ? '+' : '−'}${Math.abs(result.net_pnl_pct).toFixed(2)}%`}
            />
            <Stat label="تعداد معامله" value={result.trades} sub={`${result.wins} برد / ${result.losses} باخت`} />
            <Stat
              label="نرخ برد"
              value={`${(result.win_rate * 100).toFixed(1)}%`}
              sub={
                <>
                  ضریب سود <span className="num">{result.profit_factor.toFixed(2)}</span> · میانگین R{' '}
                  <span className="num">{result.avg_r.toFixed(2)}</span>
                </>
              }
              tone={result.win_rate >= 0.5 ? 'profit' : 'neutral'}
            />
            <Stat
              label="بیشترین افت"
              value={`−${result.max_drawdown_pct.toFixed(2)}%`}
              tone={result.max_drawdown_pct > 20 ? 'loss' : result.max_drawdown_pct > 10 ? 'warn' : 'neutral'}
              sub={
                <>
                  کارمزد <span className="num">{money(result.fees_paid)} {quoteLabel()}</span>
                </>
              }
            />
          </div>

          <Card
            title="منحنی سرمایه در بک‌تست"
            action={
              <span className="text-2xs text-slate-400">
                <span className="num">{result.symbol}</span> · <span className="num">{result.timeframe}</span> ·{' '}
                <span className="num">{result.candles}</span> کندل
              </span>
            }
          >
            {curve.length < 2 ? (
              <Empty>داده کافی نیست</Empty>
            ) : (
              <div className="h-48 sm:h-64 -mx-1" dir="ltr">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={curve} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke="#172136" strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="i" stroke="#34466a" tick={{ fill: '#94a3b8', fontSize: 11 }} minTickGap={40} axisLine={false} tickLine={false} />
                    <YAxis
                      stroke="#34466a"
                      tick={{ fill: '#94a3b8', fontSize: 11 }}
                      width={46}
                      domain={['auto', 'auto']}
                      axisLine={false}
                      tickLine={false}
                    />
                    <Tooltip
                      formatter={(value: number) => [`${money(value)} ${quoteLabel()}`, 'سرمایه']}
                      labelFormatter={(value) => `کندل ${value}`}
                    />
                    <Line
                      type="monotone"
                      dataKey="equity"
                      stroke={result.net_pnl >= 0 ? '#34d399' : '#fb7185'}
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </Card>

          <Card title="چطور نتیجه را بخوانیم">
            <ul className="space-y-2 text-xs text-slate-400 leading-6 list-disc ps-5">
              <li>
                <span className="text-slate-200">ضریب سود زیر ۱.۲</span> یعنی استراتژی روی این نماد حاشیه امن ندارد؛ با کارمزد
                واقعی احتمالاً ضررده می‌شود.
              </li>
              <li>
                <span className="text-slate-200">کمتر از ۳۰ معامله</span> از نظر آماری معنادار نیست. تعداد کندل را بیشتر کن.
              </li>
              <li>نتیجه بک‌تست همیشه از عملکرد واقعی خوش‌بینانه‌تر است؛ عمق دفتر سفارش و لغزش در نوسانات شدید مدل نشده است.</li>
              <li>
                میانگین R زیر <span className="num">0.1</span> یعنی نسبت سود به ریسک کافی نیست.
              </li>
            </ul>
          </Card>

          <Card
            padded={false}
            title={
              <>
                معاملات شبیه‌سازی‌شده <span className="num text-slate-400 font-medium">({result.trade_log.length})</span>
              </>
            }
          >
            {result.trade_log.length === 0 ? (
              <Empty>هیچ معامله‌ای در این بازه باز نشد. احتمالاً حد امتیاز سیگنال بالاست.</Empty>
            ) : (
              <>
                <ul className="lg:hidden">
                  {result.trade_log
                    .slice()
                    .reverse()
                    .map((trade, index) => (
                      <ListRow key={index} as="li">
                        <span
                          className={clsx(
                            'w-10 h-10 rounded-xl flex items-center justify-center text-2xs font-bold shrink-0',
                            trade.side === 'long' ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss',
                          )}
                        >
                          {trade.side === 'long' ? 'خرید' : 'فروش'}
                        </span>
                        <div className="grow min-w-0 grid grid-cols-3 gap-2">
                          <KV label={trade.symbol ? trade.symbol : 'ورود'} value={price(trade.entry_price)} />
                          <KV label="خروج" value={price(trade.exit_price)} />
                          <KV label="کندل" value={trade.bars} />
                        </div>
                        <div className="text-left shrink-0">
                          <div className={clsx('num text-sm font-semibold', trade.pnl >= 0 ? 'text-profit' : 'text-loss')}>
                            {trade.pnl >= 0 ? '+' : '−'}
                            {money(Math.abs(trade.pnl), 3)}
                          </div>
                          <div className="text-2xs text-slate-400">
                            <span className={clsx('num', trade.r >= 0 ? 'text-profit/80' : 'text-loss/80')}>
                              R {trade.r >= 0 ? '+' : ''}
                              {trade.r.toFixed(2)}
                            </span>
                            <span className="mx-1">·</span>
                            {REASON_FA[trade.reason] ?? trade.reason}
                          </div>
                        </div>
                      </ListRow>
                    ))}
                </ul>

                <div className="hidden lg:block overflow-x-auto pb-2">
                  <table className="w-full min-w-[620px]">
                    <thead>
                      <tr className="border-b border-ink-700">
                        <th className="th ps-5">جهت</th>
                        {core4h && <th className="th">نماد</th>}
                        <th className="th">ورود</th>
                        <th className="th">خروج</th>
                        <th className="th">سود</th>
                        <th className="th">R</th>
                        <th className="th">کندل</th>
                        <th className="th">دلیل</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-ink-800">
                      {result.trade_log
                        .slice()
                        .reverse()
                        .map((trade, index) => (
                          <tr key={index} className="hover:bg-ink-850/60">
                            <td className="td ps-5">
                              <Badge tone={trade.side === 'long' ? 'profit' : 'loss'}>
                                {trade.side === 'long' ? 'خرید' : 'فروش'}
                              </Badge>
                            </td>
                            {core4h && (
                              <td className="td num text-slate-300">
                                {trade.symbol}
                                {trade.family && (
                                  <span className="block text-2xs text-slate-400">{FAMILY_FA[trade.family] ?? trade.family}</span>
                                )}
                              </td>
                            )}
                            <td className="td num text-slate-300">{price(trade.entry_price)}</td>
                            <td className="td num text-slate-300">{price(trade.exit_price)}</td>
                            <td className={clsx('td num', trade.pnl >= 0 ? 'text-profit' : 'text-loss')}>
                              {trade.pnl >= 0 ? '+' : '−'}
                              {money(Math.abs(trade.pnl), 3)}
                              <span className="opacity-70 text-2xs ms-1">
                                ({trade.pnl_pct >= 0 ? '+' : '−'}
                                {Math.abs(trade.pnl_pct).toFixed(2)}%)
                              </span>
                            </td>
                            <td className={clsx('td num', trade.r >= 0 ? 'text-profit' : 'text-loss')}>
                              {trade.r >= 0 ? '+' : ''}
                              {trade.r.toFixed(2)}
                            </td>
                            <td className="td num text-slate-400">{trade.bars}</td>
                            <td className="td text-xs text-slate-400">{REASON_FA[trade.reason] ?? trade.reason}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Card>
        </>
      )}

      {!running && (
        <Notice tone="warn" className="lg:hidden">
          اجرای بک‌تست روی سرور انجام می‌شود؛ می‌توانی صفحه را ببندی و بعداً برگردی، ولی نتیجه ذخیره نمی‌شود.
        </Notice>
      )}
    </div>
  )
}
