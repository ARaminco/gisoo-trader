import { ReactNode, useEffect, useRef, useState } from 'react'
import clsx from 'clsx'
import {
  api,
  ApiError,
  CommitteeCheck,
  CommitteeDecision,
  CommitteeIndex,
  CommitteeMeeting,
  CommitteeProposal,
  CommitteeSeverity,
  CommitteeWindow,
} from '@/lib/api'
import { DISPLAY_TZ, FAMILY_FA, STANCE_FA, datetime, money, pct, timeAgo, quoteLabel } from '@/lib/format'
import { usePolling } from '@/hooks/usePolling'
import {
  Badge,
  Card,
  ConfirmDialog,
  Dot,
  Empty,
  ErrorBanner,
  KV,
  Notice,
  Skeleton,
  SkeletonRows,
  toast,
} from '@/components/ui'
import { ChevronIcon, CommitteeIcon, RefreshIcon } from '@/components/icons'

/**
 * The investment committee: twice a day the scheduler builds a numeric
 * briefing, the model writes the minutes against the written policy, the
 * stance / risk multiplier it may apply are applied, and everything else is
 * a proposal the user decides here. Every time shown is Tehran time.
 */

const FAST_POLL_MS = 8000
const SLOW_POLL_MS = 60000
const WAIT_LIMIT_MS = 5 * 60 * 1000

const REASON_FA: Record<string, string> = { scheduled: 'برنامه‌ای', manual: 'دستی', first: 'اولین' }
const SEVERITY_FA: Record<CommitteeSeverity, string> = { high: 'بالا', medium: 'متوسط', low: 'کم' }
const SEVERITY_TONE: Record<CommitteeSeverity, 'loss' | 'warn' | 'neutral'> = {
  high: 'loss',
  medium: 'warn',
  low: 'neutral',
}
const ACTION_FA: Record<string, string> = {
  core4h_risk_scale: 'تغییر ضریب احتیاط',
  add_capital: 'افزودن سرمایه',
  sleeve_target: 'هدف اسلیو',
  pause_bot: 'توقف موقت ورودها',
  other: 'سایر',
}
const KIND_FA: Record<string, string> = {
  stance: 'موضع بازار',
  risk_multiplier: 'ضریب ریسک ورود',
  note: 'یادداشت',
}
const PARAM_FA: Record<string, string> = {
  risk_scale: 'ضریب احتیاط',
  hours: 'ساعت',
  amount_usdt: 'مبلغ (USDT)',
  sleeve: 'اسلیو',
  target_pct: 'سهم هدف (%)',
  note: 'توضیح',
}

function N({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx('num', className)}>{children}</span>
}

function localHours(hoursUtc: number[], minute: number): string {
  return hoursUtc
    .map((hour) =>
      new Date(Date.UTC(2026, 0, 1, hour, minute)).toLocaleTimeString('fa-IR-u-nu-latn', {
        timeZone: DISPLAY_TZ,
        hour: '2-digit',
        minute: '2-digit',
      }),
    )
    .join(' و ')
}

export default function Committee() {
  // Faster polling while a manual meeting is in flight, until a new row shows up.
  const [waitingFrom, setWaitingFrom] = useState<{ since: number; lastId: number | null } | null>(null)
  const feed = usePolling(() => api.committee(20), waitingFrom ? FAST_POLL_MS : SLOW_POLL_MS, [Boolean(waitingFrom)])
  const [meetings, setMeetings] = useState<CommitteeMeeting[] | null>(null)
  const [requesting, setRequesting] = useState(false)
  const latestFromServer = useRef<CommitteeMeeting[] | null>(null)

  useEffect(() => {
    if (!feed.data) return
    latestFromServer.current = feed.data.meetings
    setMeetings(feed.data.meetings)
    if (waitingFrom) {
      const newest = feed.data.meetings[0]?.id ?? null
      if (newest !== null && newest !== waitingFrom.lastId) {
        setWaitingFrom(null)
        toast('صورت‌جلسه‌ی جدید آماده شد')
      } else if (Date.now() - waitingFrom.since > WAIT_LIMIT_MS) {
        setWaitingFrom(null)
        toast('جلسه هنوز ثبت نشده؛ لاگ زمان‌بند را ببین', 'error')
      }
    }
  }, [feed.data, waitingFrom])

  async function requestMeeting() {
    setRequesting(true)
    try {
      await api.committeeMeet()
      setWaitingFrom({ since: Date.now(), lastId: meetings?.[0]?.id ?? null })
      toast('درخواست جلسه ثبت شد؛ صورت‌جلسه تا یکی دو دقیقه دیگر می‌آید')
    } catch (error) {
      toast(error instanceof Error ? error.message : 'درخواست جلسه ناموفق بود', 'error')
    } finally {
      setRequesting(false)
    }
  }

  function replaceMeeting(updated: CommitteeMeeting) {
    setMeetings((current) => (current ?? []).map((row) => (row.id === updated.id ? updated : row)))
  }

  const index = feed.data
  if (feed.loading && !index) return <CommitteeSkeleton />

  const latest = meetings?.[0] ?? null

  return (
    <div className="space-y-4">
      <ErrorBanner message={feed.error} />

      {index && !index.enabled && (
        <Notice tone="warn">کمیته خاموش است (COMMITTEE_ENABLED=false). صورت‌جلسه‌های قبلی همچنان دیده می‌شوند.</Notice>
      )}

      {index && (
        <Hero
          index={index}
          latest={latest}
          requesting={requesting}
          waiting={Boolean(waitingFrom)}
          onMeet={requestMeeting}
          onRefresh={feed.refresh}
        />
      )}

      {latest && <HealthCard meeting={latest} />}

      {meetings && meetings.length === 0 && (
        <Card>
          <Empty icon={<CommitteeIcon className="w-6 h-6" />}>
            هنوز جلسه‌ای برگزار نشده. اولین جلسه در ساعت برنامه‌ای برگزار می‌شود، یا همین حالا با دکمه‌ی بالا.
          </Empty>
        </Card>
      )}

      {meetings?.map((meeting, position) => (
        <MeetingCard
          key={meeting.id}
          meeting={meeting}
          open={position === 0}
          onUpdated={replaceMeeting}
        />
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- hero */

function Hero({
  index,
  latest,
  requesting,
  waiting,
  onMeet,
  onRefresh,
}: {
  index: CommitteeIndex
  latest: CommitteeMeeting | null
  requesting: boolean
  waiting: boolean
  onMeet: () => void
  onRefresh: () => void
}) {
  const canMeet = index.enabled && index.scheduler_online && !requesting && !waiting
  return (
    <Card>
      <div className="flex items-start gap-3">
        <span className="w-11 h-11 rounded-xl bg-accent/15 text-accent flex items-center justify-center shrink-0">
          <CommitteeIcon className="w-6 h-6" />
        </span>
        <div className="grow min-w-0">
          <h2 className="text-base font-semibold text-slate-50">کمیته‌ی سرمایه‌گذاری</h2>
          <p className="text-xs text-slate-400 leading-5 mt-0.5">
            هر روز ساعت <N>{localHours(index.hours_utc, index.meeting_minute)}</N> به وقت تهران بازار و حساب را
            در چارچوب سیاست مکتوب بازبینی می‌کند. فقط موضع بازار و ضریب ریسکِ ورودهای جدید را خودش تنظیم می‌کند؛
            هر چیز دیگری پیشنهاد است و منتظر تایید تو.
          </p>
        </div>
        <button
          type="button"
          className="w-10 h-10 -me-2 flex items-center justify-center rounded-lg text-slate-400 hover:bg-ink-800 shrink-0"
          onClick={onRefresh}
          aria-label="به‌روزرسانی"
        >
          <RefreshIcon className="w-5 h-5" />
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 mt-4">
        <KV label="جلسه‌ی بعدی" value={index.next_meeting_at ? datetime(index.next_meeting_at) : '—'} />
        <KV
          label="آخرین جلسه"
          value={latest ? `${timeAgo(latest.held_at)} · ${REASON_FA[latest.reason] ?? latest.reason}` : '—'}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2 mt-4">
        <button type="button" className="btn-primary grow sm:grow-0" disabled={!canMeet} onClick={onMeet}>
          {waiting ? 'در حال برگزاری…' : 'تشکیل جلسه الان'}
        </button>
        <span className="flex items-center gap-1.5 text-2xs text-slate-400">
          <Dot ok={index.scheduler_online} />
          {index.scheduler_online ? 'زمان‌بند آنلاین است' : 'زمان‌بند آفلاین است؛ جلسه بدون آن برگزار نمی‌شود'}
        </span>
      </div>
    </Card>
  )
}

/* ---------------------------------------------------------------- health */

function HealthCard({ meeting }: { meeting: CommitteeMeeting }) {
  const checks = meeting.health?.checks ?? []
  const failed = checks.filter((check) => check.ok === false)
  const [showAll, setShowAll] = useState(false)
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Dot ok={meeting.health_ok} />
          بررسی سلامت
          <span className="text-2xs font-normal text-slate-400">{timeAgo(meeting.held_at)}</span>
        </span>
      }
      action={
        <button type="button" className="btn-ghost btn-sm" onClick={() => setShowAll((value) => !value)}>
          {showAll ? 'کمتر' : 'جزئیات'}
        </button>
      }
    >
      <div className="flex flex-wrap gap-2">
        {checks.map((check) => (
          <HealthChip key={check.code} check={check} />
        ))}
      </div>
      {failed.length > 0 && (
        <Notice tone="danger" className="mt-3">
          <ul className="space-y-1">
            {failed.map((check) => (
              <li key={check.code}>
                <span className="font-semibold">{check.label_fa}:</span> <N>{check.detail}</N>
              </li>
            ))}
          </ul>
        </Notice>
      )}
      {showAll && (
        <dl className="mt-3">
          {checks.map((check) => (
            <div key={check.code} className="flex items-start justify-between gap-4 py-2 border-b border-ink-800 last:border-b-0 text-xs">
              <dt className="text-slate-300 shrink-0 flex items-center gap-1.5">
                <ChipDot ok={check.ok} />
                {check.label_fa}
              </dt>
              <dd className="text-slate-400 text-left num leading-5 min-w-0 break-words">{check.detail}</dd>
            </div>
          ))}
        </dl>
      )}
    </Card>
  )
}

function ChipDot({ ok }: { ok: boolean | null }) {
  return (
    <span
      className={clsx(
        'inline-block w-2 h-2 rounded-full shrink-0',
        ok === true ? 'bg-profit' : ok === false ? 'bg-loss' : 'bg-slate-500',
      )}
    />
  )
}

function HealthChip({ check }: { check: CommitteeCheck }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 h-7 text-2xs',
        check.ok === true
          ? 'border-profit/30 bg-profit/10 text-profit'
          : check.ok === false
            ? 'border-loss/40 bg-loss/10 text-loss'
            : 'border-ink-700 bg-ink-850 text-slate-400',
      )}
      title={check.detail}
    >
      <ChipDot ok={check.ok} />
      {check.label_fa}
    </span>
  )
}

/* ---------------------------------------------------------------- one meeting */

function MeetingCard({
  meeting,
  open: initiallyOpen,
  onUpdated,
}: {
  meeting: CommitteeMeeting
  open: boolean
  onUpdated: (meeting: CommitteeMeeting) => void
}) {
  const [open, setOpen] = useState(initiallyOpen)
  const [details, setDetails] = useState(false)
  const pending = meeting.proposals.filter((proposal) => !proposal.status).length
  const highRisks = meeting.risks.filter((risk) => risk.severity === 'high').length

  return (
    <Card padded={false}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full text-right flex items-center gap-3 px-4 sm:px-5 py-4 min-h-[64px] transition-colors active:bg-ink-800/70 hover:bg-ink-850/60 rounded-t-2.5xl"
        aria-expanded={open}
      >
        <Dot ok={meeting.health_ok && highRisks === 0} className="shrink-0" />
        <span className="grow min-w-0">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-slate-50">
              جلسه #<N>{meeting.id}</N>
            </span>
            <Badge tone={meeting.reason === 'manual' ? 'accent' : 'neutral'}>{REASON_FA[meeting.reason] ?? meeting.reason}</Badge>
            {meeting.provider === 'template' ? (
              <Badge tone="warn">بدون مدل</Badge>
            ) : (
              <Badge tone="neutral">
                <N>{meeting.model || meeting.provider}</N>
              </Badge>
            )}
            {pending > 0 && (
              <Badge tone="warn">
                <N>{pending}</N> پیشنهاد منتظر تو
              </Badge>
            )}
          </span>
          <span className="block text-2xs text-slate-400 mt-1">
            <N>{datetime(meeting.held_at)}</N>
            {!open && meeting.summary_fa && (
              <span className="block truncate text-slate-300 mt-0.5">{meeting.summary_fa}</span>
            )}
          </span>
        </span>
        <ChevronIcon className={clsx('w-5 h-5 text-slate-500 shrink-0 transition-transform', open && 'rotate-90')} />
      </button>

      {open && (
        <div className="px-4 sm:px-5 pb-4 sm:pb-5 space-y-4 border-t border-ink-800 pt-4">
          <p className="text-sm text-slate-100 leading-7">{meeting.summary_fa}</p>

          {meeting.risks.length > 0 && (
            <section>
              <h3 className="text-xs font-semibold text-slate-400 mb-2">ریسک‌ها</h3>
              <ul className="space-y-2">
                {meeting.risks.map((risk, position) => (
                  <li key={position} className="card-raised p-3">
                    <div className="flex items-center gap-2 mb-1">
                      <Badge tone={SEVERITY_TONE[risk.severity]}>{SEVERITY_FA[risk.severity]}</Badge>
                      <span className="text-sm font-medium text-slate-100">{risk.title_fa}</span>
                    </div>
                    <p className="text-xs text-slate-300 leading-6">{risk.detail_fa}</p>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section>
            <h3 className="text-xs font-semibold text-slate-400 mb-2">تصمیم‌های اعمال‌شده</h3>
            <Decisions meeting={meeting} />
          </section>

          {meeting.proposals.length > 0 && (
            <section>
              <h3 className="text-xs font-semibold text-slate-400 mb-2">پیشنهادها برای تایید تو</h3>
              <ul className="space-y-2">
                {meeting.proposals.map((proposal) => (
                  <ProposalRow key={proposal.index} meeting={meeting} proposal={proposal} onUpdated={onUpdated} />
                ))}
              </ul>
            </section>
          )}

          <button type="button" className="btn-ghost btn-sm" onClick={() => setDetails((value) => !value)}>
            {details ? 'بستن جزئیات' : 'بازار، پوزیشن‌ها و اعداد'}
          </button>
          {details && <Details meeting={meeting} />}
        </div>
      )}
    </Card>
  )
}

function Decisions({ meeting }: { meeting: CommitteeMeeting }) {
  const applied = meeting.applied_decisions ?? []
  const decisions = meeting.decisions ?? []
  if (decisions.length === 0 && applied.length === 0) {
    return <p className="text-xs text-slate-400 leading-6">تصمیم خودکاری ثبت نشد؛ موضع و ضریب ریسک فعلی بدون تغییر ماند.</p>
  }
  return (
    <ul className="space-y-2">
      {decisions.map((decision, position) => {
        const record = applied.find((row) => row.kind === decision.kind && row.rationale_fa === decision.rationale_fa)
        return <DecisionRow key={position} decision={decision} applied={record?.applied ?? false} changed={record?.changed} why={record?.why} />
      })}
    </ul>
  )
}

function DecisionRow({
  decision,
  applied,
  changed,
  why,
}: {
  decision: CommitteeDecision
  applied: boolean
  changed?: boolean
  why?: string
}) {
  const value =
    decision.kind === 'stance'
      ? STANCE_FA[decision.value] ?? decision.value
      : decision.kind === 'risk_multiplier'
        ? `×${Number(decision.value).toFixed(2)}`
        : decision.value
  return (
    <li className="card-raised p-3">
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <Badge tone="accent">{KIND_FA[decision.kind] ?? decision.kind}</Badge>
        <span className="text-sm font-medium text-slate-100">
          <N>{value}</N>
        </span>
        <span className="grow" />
        {decision.kind === 'note' ? (
          <Badge tone="neutral">فقط ثبت</Badge>
        ) : applied ? (
          <Badge tone={changed ? 'profit' : 'neutral'}>{changed ? 'اعمال شد' : 'بدون تغییر'}</Badge>
        ) : (
          <Badge tone="warn">{why ? 'رد شد' : 'اعمال نشد'}</Badge>
        )}
      </div>
      <p className="text-xs text-slate-300 leading-6">{decision.rationale_fa}</p>
      {why && <p className="text-2xs text-warn mt-1">{why}</p>}
    </li>
  )
}

/* ---------------------------------------------------------------- proposals */

function ProposalRow({
  meeting,
  proposal,
  onUpdated,
}: {
  meeting: CommitteeMeeting
  proposal: CommitteeProposal
  onUpdated: (meeting: CommitteeMeeting) => void
}) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const params = Object.entries(proposal.params ?? {}).filter(([, value]) => value !== null && value !== undefined && value !== '')
  const applies = proposal.action === 'core4h_risk_scale' || proposal.action === 'pause_bot'

  async function decide(verb: 'approve' | 'decline') {
    setBusy(true)
    try {
      const updated = await api.committeeVerdict(meeting.id, proposal.index, verb)
      onUpdated(updated)
      const status = updated.proposals[proposal.index]?.status
      const note = status?.result?.note_fa
      toast(verb === 'approve' ? `تایید شد${note ? ` — ${note}` : ''}` : 'پیشنهاد رد شد')
    } catch (error) {
      toast(error instanceof ApiError ? error.message : 'ثبت نظر ناموفق بود', 'error')
    } finally {
      setBusy(false)
      setConfirm(false)
    }
  }

  return (
    <li className="card-raised p-3">
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <Badge tone={applies ? 'warn' : 'neutral'}>{ACTION_FA[proposal.action] ?? proposal.action}</Badge>
        <span className="text-sm font-medium text-slate-100">{proposal.title_fa}</span>
        <span className="grow" />
        {proposal.status && (
          <Badge tone={proposal.status.status === 'approved' ? 'profit' : 'loss'}>
            {proposal.status.status === 'approved' ? 'تایید شد' : 'رد شد'}
          </Badge>
        )}
      </div>
      {params.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-2xs text-slate-400 mb-1">
          {params.map(([key, value]) => (
            <span key={key}>
              {PARAM_FA[key] ?? key}: <N className="text-slate-200">{String(value)}</N>
            </span>
          ))}
        </div>
      )}
      <p className="text-xs text-slate-300 leading-6">{proposal.rationale_fa}</p>
      {proposal.expected_effect_fa && (
        <p className="text-2xs text-slate-400 leading-5 mt-1">اثر مورد انتظار: {proposal.expected_effect_fa}</p>
      )}

      {proposal.status ? (
        <p className="text-2xs text-slate-400 leading-5 mt-2">
          <N>{datetime(proposal.status.at)}</N>
          {proposal.status.result?.note_fa && <span className="block text-slate-300">{proposal.status.result.note_fa}</span>}
        </p>
      ) : (
        <div className="flex gap-2 mt-3">
          <button type="button" className="btn-success btn-sm grow sm:grow-0 sm:min-w-[7rem]" disabled={busy} onClick={() => setConfirm(true)}>
            تایید
          </button>
          <button type="button" className="btn-danger-soft btn-sm grow sm:grow-0 sm:min-w-[7rem]" disabled={busy} onClick={() => decide('decline')}>
            رد
          </button>
        </div>
      )}

      <ConfirmDialog
        open={confirm}
        title="تایید پیشنهاد کمیته"
        body={<ApprovalBody proposal={proposal} />}
        confirmText="تایید و اعمال"
        danger={applies}
        onConfirm={() => decide('approve')}
        onCancel={() => setConfirm(false)}
      />
    </li>
  )
}

function ApprovalBody({ proposal }: { proposal: CommitteeProposal }) {
  const params = proposal.params ?? {}
  switch (proposal.action) {
    case 'core4h_risk_scale':
      return (
        <>
          ضریب احتیاط به <N className="font-semibold text-accent">{String(params.risk_scale ?? '—')}</N> تغییر می‌کند و
          اندازه‌ی معاملات جدید را عوض می‌کند. سیاست می‌گوید بازگشت به 1.0 فقط بعد از 90 روز نتیجه‌ی مطابق مطالعه.
        </>
      )
    case 'pause_bot':
      return (
        <>
          ورود جدید تا <N className="font-semibold text-accent">{String(params.hours ?? 24)}</N> ساعت متوقف می‌شود.
          پوزیشن‌های باز و استاپ‌های سمت صرافی سر جایشان می‌مانند.
        </>
      )
    case 'sleeve_target':
      return <>فقط ثبت می‌شود؛ اسلیو هنوز فعال نیست و پولی جابه‌جا نمی‌شود.</>
    default:
      return <>فقط به‌عنوان «دیده شد» ثبت می‌شود؛ خود کار (مثلاً واریز) خارج از ربات انجام می‌شود.</>
  }
}

/* ---------------------------------------------------------------- details */

function Details({ meeting }: { meeting: CommitteeMeeting }) {
  const account = meeting.account ?? {}
  const market = meeting.market ?? {}
  const regime = market.btc_regime ?? null
  const performance = meeting.performance
  return (
    <div className="space-y-4">
      {meeting.market_view_fa && (
        <section>
          <h3 className="text-xs font-semibold text-slate-400 mb-1">نگاه به بازار</h3>
          <p className="text-xs text-slate-300 leading-6">{meeting.market_view_fa}</p>
        </section>
      )}
      {meeting.positions_view_fa && (
        <section>
          <h3 className="text-xs font-semibold text-slate-400 mb-1">نگاه به پوزیشن‌ها</h3>
          <p className="text-xs text-slate-300 leading-6">{meeting.positions_view_fa}</p>
        </section>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <KV label="سرمایه" value={`${money(account.equity)} ${quoteLabel()}`} />
        <KV label="سرمایه‌ی پایه" value={`${money(account.capital_base)} ${quoteLabel()}`} />
        <KV label="درگیر در پوزیشن" value={`${money(account.deployed_pct, 1)}%`} />
        <KV label={`${quoteLabel()} آزاد`} value={money(account.idle_usdt)} />
        <KV label="سود/زیان کل" value={pct(account.total_pnl_pct)} valueClass={account.total_pnl_pct >= 0 ? 'text-profit' : 'text-loss'} />
        <KV label="افت سرمایه‌ی بسته" value={`${money(account.closed_drawdown_pct, 1)}% از ${money(meeting.settings?.max_drawdown_pct ?? 20, 0)}%`} />
        <KV label="Simple Earn" value={account.earn_usdt == null ? '—' : `${money(account.earn_usdt)} USDT`} />
        <KV
          label="رژیم بیت‌کوین"
          value={regime ? `${regime.regime_on ? 'روشن' : 'خاموش'} · ${pct(regime.distance_pct, 1)} از EMA200` : '—'}
          valueClass={regime?.regime_on ? 'text-profit' : 'text-loss'}
        />
      </div>

      {meeting.positions.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-slate-400 mb-2">پوزیشن‌ها در لحظه‌ی جلسه</h3>
          <ul className="divide-y divide-ink-800">
            {meeting.positions.map((position) => (
              <li key={position.id} className="flex items-center gap-3 py-2 text-xs">
                <span className="grow min-w-0">
                  <N className="text-slate-100 font-medium">{position.symbol}</N>
                  <span className="text-slate-400 ms-2">{FAMILY_FA[position.family] ?? position.family}</span>
                </span>
                <N className={clsx('font-semibold', (position.unrealized_pct ?? 0) >= 0 ? 'text-profit' : 'text-loss')}>
                  {pct(position.unrealized_pct)}
                </N>
                <N className="text-slate-400 w-24 text-left">استاپ {pct(-(position.stop_distance_pct ?? 0), 1)}</N>
              </li>
            ))}
          </ul>
        </section>
      )}

      {performance && (
        <section>
          <h3 className="text-xs font-semibold text-slate-400 mb-2">عملکرد در برابر انتظار مطالعه</h3>
          <div className="grid sm:grid-cols-3 gap-3">
            <WindowCard label="از ابتدا" window={performance.inception} />
            <WindowCard label="7 روز اخیر" window={performance.last_7d} />
            <WindowCard label="30 روز اخیر" window={performance.last_30d} />
          </div>
          <p className="text-2xs text-slate-400 leading-5 mt-2">
            انتظار مطالعه: نرخ برد <N>35–50%</N>، میانه‌ی برنده حدود <N>+8%</N>، استاپ کامل حدود <N>−1R</N>، توقف
            روزانه در <N>{money(performance.expectations?.daily_pause_pct, 0)}%</N> (این ماه <N>{performance.daily_pauses_30d}</N> بار).
          </p>
        </section>
      )}

      {meeting.stance_history.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-slate-400 mb-2">موضع مشاور در 24 ساعت قبل</h3>
          <ul className="space-y-1">
            {meeting.stance_history.map((row, position) => (
              <li key={position} className="flex items-center gap-2 text-2xs text-slate-400">
                <Badge tone={row.stance === 'risk_on' ? 'profit' : row.stance === 'risk_off' ? 'loss' : 'neutral'}>
                  {STANCE_FA[row.stance] ?? row.stance}
                </Badge>
                <N>×{Number(row.risk_multiplier).toFixed(2)}</N>
                {row.source === 'committee' && <Badge tone="accent">کمیته</Badge>}
                <span className="grow" />
                <N>{datetime(row.at)}</N>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="text-2xs text-slate-500 leading-5">
        {meeting.provider === 'template' ? 'بدون مدل زبانی' : `${meeting.provider}${meeting.model ? ` · ${meeting.model}` : ''}`}
        {meeting.tokens > 0 && (
          <>
            {' · '}
            <N>{meeting.tokens.toLocaleString('en-US')}</N> توکن
          </>
        )}
        {meeting.confidence != null && (
          <>
            {' · '}اطمینان <N>{Math.round(Number(meeting.confidence) * 100)}%</N>
          </>
        )}
        {Object.keys(meeting.delivery ?? {}).length > 0 && (
          <>
            {' · '}ارسال: <N>{Object.entries(meeting.delivery).map(([key, value]) => `${key} ${value}`).join(', ')}</N>
          </>
        )}
      </p>
    </div>
  )
}

function WindowCard({ label, window }: { label: string; window: CommitteeWindow }) {
  return (
    <div className="card-raised p-3">
      <div className="text-2xs text-slate-400 mb-1">{label}</div>
      {window.trades === 0 ? (
        <div className="text-xs text-slate-400">بدون معامله‌ی بسته</div>
      ) : (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <KV label="معامله" value={window.trades} />
          <KV label="نرخ برد" value={window.win_rate == null ? '—' : `${Math.round(window.win_rate * 100)}%`} />
          <KV label="خالص" value={`${money(window.net_pnl)} ${quoteLabel()}`} valueClass={window.net_pnl >= 0 ? 'text-profit' : 'text-loss'} />
          <KV label="میانگین R" value={window.avg_r == null ? '—' : window.avg_r.toFixed(2)} />
          <KV label="میانگین زیان (R)" value={window.avg_loss_r == null ? '—' : window.avg_loss_r.toFixed(2)} />
          <KV label="استاپ کامل" value={window.full_stops} />
        </div>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- skeleton */

function CommitteeSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true">
      <div className="card p-4 sm:p-5 space-y-3">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3 w-3/4" />
        <Skeleton className="h-10 w-40 rounded-xl" />
      </div>
      <div className="card p-4 sm:p-5">
        <SkeletonRows rows={3} />
      </div>
    </div>
  )
}
