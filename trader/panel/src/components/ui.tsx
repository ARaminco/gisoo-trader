import clsx from 'clsx'
import {
  ClipboardEvent,
  KeyboardEvent,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import { pct, signed } from '@/lib/format'
import { CheckIcon, CloseIcon, CopyIcon } from '@/components/icons'

/* ------------------------------------------------------------------ surfaces */

export function Card({
  children,
  className,
  title,
  action,
  padded = true,
}: {
  children: ReactNode
  className?: string
  title?: ReactNode
  action?: ReactNode
  /** Turn off for lists that want to run edge to edge inside the card. */
  padded?: boolean
}) {
  return (
    <section className={clsx('card', padded && 'p-4 sm:p-5', className)}>
      {(title || action) && (
        <header
          className={clsx(
            'flex items-center justify-between gap-3',
            padded ? 'mb-4' : 'px-4 sm:px-5 pt-4 sm:pt-5 pb-3',
          )}
        >
          {title && <h2 className="text-sm font-semibold text-slate-200">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

export function Stat({
  label,
  value,
  sub,
  tone = 'neutral',
  className,
  size = 'md',
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: 'neutral' | 'profit' | 'loss' | 'warn'
  className?: string
  size?: 'sm' | 'md'
}) {
  const toneClass = {
    neutral: 'text-slate-100',
    profit: 'text-profit',
    loss: 'text-loss',
    warn: 'text-warn',
  }[tone]

  return (
    <div className={clsx('card', size === 'sm' ? 'p-3.5' : 'p-4', className)}>
      <div className="text-2xs font-medium text-slate-400 mb-1">{label}</div>
      <div
        className={clsx(
          'num font-bold leading-tight',
          size === 'sm' ? 'text-lg' : 'text-xl sm:text-2xl',
          toneClass,
        )}
      >
        {value}
      </div>
      {sub && <div className="text-2xs text-slate-400 mt-1.5 leading-4">{sub}</div>}
    </div>
  )
}

export function PnL({
  value,
  percent,
  digits = 2,
  className,
}: {
  value: number
  percent?: number
  digits?: number
  className?: string
}) {
  const tone = value > 0 ? 'text-profit' : value < 0 ? 'text-loss' : 'text-slate-300'
  return (
    <span className={clsx('num font-semibold', tone, className)}>
      {signed(value, digits)}
      {percent != null && (
        <span className="opacity-75 text-[0.8em] font-medium ms-1">({pct(percent)})</span>
      )}
    </span>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode
  tone?: 'neutral' | 'profit' | 'loss' | 'warn' | 'accent'
  className?: string
}) {
  const tones = {
    neutral: 'bg-slate-400/15 text-slate-200',
    profit: 'bg-profit/15 text-profit',
    loss: 'bg-loss/15 text-loss',
    warn: 'bg-warn/15 text-warn',
    accent: 'bg-accent/15 text-accent',
  }
  return <span className={clsx('badge', tones[tone], className)}>{children}</span>
}

export function Dot({
  ok,
  className,
  live = false,
}: {
  ok: boolean
  className?: string
  /** Slow breathing ring: the only ambient motion in the app, meaning "alive". */
  live?: boolean
}) {
  return (
    <span
      className={clsx(
        'inline-block w-2 h-2 rounded-full shrink-0',
        ok ? 'bg-profit' : 'bg-loss',
        ok && live && 'animate-breathe',
        className,
      )}
    />
  )
}

export function Empty({
  children,
  icon,
  action,
}: {
  children: ReactNode
  icon?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center text-center py-10 px-4 gap-3">
      {icon && (
        <div className="w-12 h-12 rounded-2xl bg-ink-850 border border-ink-700/70 flex items-center justify-center text-slate-400">
          {icon}
        </div>
      )}
      <div className="text-sm text-slate-400 leading-6 max-w-[22rem]">{children}</div>
      {action}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <div
      className={clsx(
        'w-5 h-5 rounded-full border-2 border-ink-600 border-t-accent animate-spin',
        className,
      )}
      role="status"
      aria-label="در حال بارگذاری"
    />
  )
}

export function ErrorBanner({ message, className }: { message: string | null; className?: string }) {
  if (!message) return null
  return (
    <div
      className={clsx(
        'rounded-xl bg-loss/10 border border-loss/30 text-loss px-4 py-3 text-sm leading-6',
        className,
      )}
      role="alert"
    >
      {message}
    </div>
  )
}

export function Notice({
  tone = 'info',
  children,
  className,
}: {
  tone?: 'info' | 'warn' | 'danger' | 'ok'
  children: ReactNode
  className?: string
}) {
  const tones = {
    info: 'bg-accent/10 border-accent/25 text-slate-200',
    warn: 'bg-warn/10 border-warn/30 text-warn',
    danger: 'bg-loss/10 border-loss/30 text-loss',
    ok: 'bg-profit/10 border-profit/30 text-profit',
  }
  return (
    <div className={clsx('rounded-xl border px-4 py-3 text-xs leading-6', tones[tone], className)}>
      {children}
    </div>
  )
}

/* ------------------------------------------------------------------ skeletons */

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx('skeleton', className)} aria-hidden="true" />
}

export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={clsx('space-y-3', className)} aria-busy="true">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex items-center gap-3">
          <Skeleton className="w-9 h-9 rounded-xl shrink-0" />
          <div className="grow space-y-2">
            <Skeleton className="h-3.5 w-2/5" />
            <Skeleton className="h-3 w-3/5" />
          </div>
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  )
}

export function SkeletonStats({ count = 4 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3" aria-busy="true">
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="card p-4 space-y-2.5">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-6 w-28" />
          <Skeleton className="h-3 w-24" />
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ lists */

/** One row of a card list: a leading block, a body, and a trailing number. */
export function ListRow({
  children,
  onClick,
  className,
  as: Tag = 'div',
}: {
  children: ReactNode
  onClick?: () => void
  className?: string
  as?: 'div' | 'li' | 'button'
}) {
  const interactive = Boolean(onClick)
  const Element = (interactive ? 'button' : Tag) as any
  return (
    <Element
      onClick={onClick}
      type={interactive ? 'button' : undefined}
      className={clsx(
        'w-full text-right flex items-center gap-3 min-h-[56px] px-4 sm:px-5 py-3',
        'border-b border-ink-800 last:border-b-0',
        interactive && 'transition-colors active:bg-ink-800/70 hover:bg-ink-850/60',
        className,
      )}
    >
      {children}
    </Element>
  )
}

/** Small label above a value; used to lay out secondary numbers in a row. */
export function KV({
  label,
  value,
  className,
  valueClass,
}: {
  label: string
  value: ReactNode
  className?: string
  valueClass?: string
}) {
  return (
    <div className={clsx('min-w-0', className)}>
      <div className="text-2xs text-slate-400 leading-4">{label}</div>
      <div className={clsx('num text-sm text-slate-200 leading-5 truncate', valueClass)}>{value}</div>
    </div>
  )
}

/** Label on the right, value on the left (RTL) with a hairline between rows. */
export function Row({ label, value }: { label: ReactNode; value: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5 border-b border-ink-800 last:border-b-0 text-sm">
      <dt className="text-slate-400">{label}</dt>
      <dd className="text-slate-100 text-left">{value}</dd>
    </div>
  )
}

/* ------------------------------------------------------------------ controls */

export function Segmented<T extends string | number>({
  value,
  onChange,
  options,
  className,
  size = 'sm',
}: {
  value: T
  onChange: (value: T) => void
  options: Array<{ value: T; label: ReactNode }>
  className?: string
  size?: 'sm' | 'md'
}) {
  return (
    <div
      role="tablist"
      className={clsx('inline-flex bg-ink-850 border border-ink-700/70 rounded-xl p-1 gap-0.5', className)}
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={String(option.value)}
            role="tab"
            aria-selected={active}
            type="button"
            onClick={() => onChange(option.value)}
            className={clsx(
              'rounded-lg font-medium transition-colors whitespace-nowrap',
              size === 'sm' ? 'px-3 min-h-[32px] text-xs' : 'px-4 min-h-[40px] text-sm grow',
              active ? 'bg-ink-700 text-slate-50' : 'text-slate-400 hover:text-slate-200',
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  label: ReactNode
  hint?: ReactNode
  disabled?: boolean
}) {
  return (
    <label
      className={clsx(
        'flex items-center justify-between gap-4 min-h-[52px] py-2 cursor-pointer select-none',
        disabled && 'opacity-50 cursor-not-allowed',
      )}
    >
      <span className="min-w-0">
        <span className="block text-sm text-slate-100">{label}</span>
        {hint && <span className="block text-2xs text-slate-400 mt-0.5 leading-4">{hint}</span>}
      </span>
      <span className="relative shrink-0">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="block w-12 h-7 rounded-full bg-ink-700 peer-checked:bg-profit transition-colors peer-focus-visible:ring-2 peer-focus-visible:ring-accent/60" />
        <span className="absolute top-1 right-1 w-5 h-5 rounded-full bg-white shadow transition-transform peer-checked:-translate-x-5" />
      </span>
    </label>
  )
}

export function CopyButton({
  text,
  label = 'کپی',
  className,
}: {
  text: string
  label?: string
  className?: string
}) {
  const [done, setDone] = useState(false)
  async function copy() {
    const ok = await copyToClipboard(text)
    if (ok) {
      setDone(true)
      window.setTimeout(() => setDone(false), 1800)
    } else {
      toast('کپی نشد. متن را دستی انتخاب کن', 'error')
    }
  }
  return (
    <button type="button" className={clsx('btn-ghost btn-sm', className)} onClick={copy}>
      {done ? <CheckIcon className="w-4 h-4 text-profit" /> : <CopyIcon className="w-4 h-4" />}
      {done ? 'کپی شد' : label}
    </button>
  )
}

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(area)
      return ok
    } catch {
      return false
    }
  }
}

/**
 * Six-digit code entry. One real input (so paste, autofill and the numeric
 * keyboard all work) drawn as six boxes.
 */
export function CodeInput({
  value,
  onChange,
  onComplete,
  length = 6,
  disabled,
  autoFocus,
  error,
}: {
  value: string
  onChange: (value: string) => void
  onComplete?: (value: string) => void
  length?: number
  disabled?: boolean
  autoFocus?: boolean
  error?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [focused, setFocused] = useState(false)
  const completedFor = useRef<string | null>(null)

  const apply = useCallback(
    (raw: string) => {
      const digits = raw.replace(/\D/g, '').slice(0, length)
      onChange(digits)
      if (digits.length === length && completedFor.current !== digits) {
        completedFor.current = digits
        onComplete?.(digits)
      }
      if (digits.length < length) completedFor.current = null
    },
    [length, onChange, onComplete],
  )

  function onPaste(event: ClipboardEvent<HTMLInputElement>) {
    event.preventDefault()
    apply(event.clipboardData.getData('text'))
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter' && value.length === length) {
      event.preventDefault()
      onComplete?.(value)
    }
  }

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus()
  }, [autoFocus])

  const active = Math.min(value.length, length - 1)

  return (
    <div className="relative" dir="ltr" onClick={() => inputRef.current?.focus()}>
      <div className="grid grid-cols-6 gap-2">
        {Array.from({ length }).map((_, index) => {
          const char = value[index] ?? ''
          const isActive = focused && index === active && value.length < length
          return (
            <div
              key={index}
              className={clsx(
                'h-14 rounded-xl border bg-ink-850 flex items-center justify-center num text-2xl font-bold text-slate-50 transition-colors',
                error
                  ? 'border-loss/70'
                  : isActive
                    ? 'border-accent ring-2 ring-accent/30'
                    : char
                      ? 'border-ink-600'
                      : 'border-ink-700',
              )}
            >
              {char || (isActive ? <span className="w-px h-6 bg-accent animate-pulse" /> : '')}
            </div>
          )
        })}
      </div>
      <input
        ref={inputRef}
        className="absolute inset-0 w-full h-full opacity-0 cursor-text"
        inputMode="numeric"
        pattern="[0-9]*"
        autoComplete="one-time-code"
        maxLength={length}
        value={value}
        disabled={disabled}
        onChange={(event) => apply(event.target.value)}
        onPaste={onPaste}
        onKeyDown={onKeyDown}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        aria-label="کد شش رقمی"
      />
    </div>
  )
}

/* ------------------------------------------------------------------ toasts */

type ToastTone = 'ok' | 'error'

export type ToastOptions = {
  /** Stays until dismissed or acted on instead of fading after 4 s. */
  sticky?: boolean
  /** One inline action, e.g. "به‌روزرسانی" on the new-version toast. */
  action?: { label: string; onClick: () => void }
  /** A toast with the same id replaces the earlier one instead of stacking. */
  id?: string
}

type ToastItem = {
  id: string
  message: string
  tone: ToastTone
  sticky: boolean
  action?: ToastOptions['action']
}

let pushToast: ((item: ToastItem) => void) | null = null
/* Raised before <Toaster> mounted (the service-worker code in main.tsx can
   fire during boot); replayed as soon as it does. */
const pending: ToastItem[] = []

export function toast(message: string, tone: ToastTone = 'ok', options: ToastOptions = {}) {
  const item: ToastItem = {
    id: options.id ?? `toast-${Date.now()}-${Math.random()}`,
    message,
    tone,
    sticky: Boolean(options.sticky),
    action: options.action,
  }
  if (pushToast) pushToast(item)
  else pending.push(item)
}

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([])
  const timers = useRef(new Map<string, number>())

  useEffect(() => {
    pushToast = (item) => {
      setItems((current) => [...current.filter((existing) => existing.id !== item.id).slice(-2), item])
      const previous = timers.current.get(item.id)
      if (previous) window.clearTimeout(previous)
      if (!item.sticky) {
        timers.current.set(
          item.id,
          window.setTimeout(() => {
            setItems((current) => current.filter((existing) => existing.id !== item.id))
          }, 4200),
        )
      }
    }
    pending.splice(0).forEach((item) => pushToast?.(item))
    return () => {
      pushToast = null
    }
  }, [])

  const dismiss = (id: string) => setItems((current) => current.filter((item) => item.id !== id))

  return (
    <div
      className="fixed left-1/2 -translate-x-1/2 z-[60] flex flex-col gap-2 w-[min(92vw,26rem)] pointer-events-none above-tabbar"
      aria-live="polite"
    >
      {items.map((item) => (
        <div
          key={item.id}
          className={clsx(
            'rounded-xl ps-4 pe-2 py-2 min-h-[48px] text-sm border backdrop-blur animate-pop-in pointer-events-auto',
            'flex items-center gap-2',
            item.tone === 'error'
              ? 'bg-loss-deep/90 border-loss text-white'
              : 'bg-ink-800/95 border-ink-600 text-slate-50',
          )}
        >
          <span className="grow leading-6 py-1">{item.message}</span>
          {item.action && (
            <button
              type="button"
              className="btn-primary btn-sm !min-h-[32px] shrink-0"
              onClick={item.action.onClick}
            >
              {item.action.label}
            </button>
          )}
          {item.sticky && (
            <button
              type="button"
              className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-100 hover:bg-ink-700 shrink-0"
              onClick={() => dismiss(item.id)}
              aria-label="بستن"
            >
              <CloseIcon className="w-4 h-4" />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ sheets */

function useBodyLock(active: boolean) {
  useEffect(() => {
    if (!active) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [active])
}

/**
 * Bottom sheet on phones, centred dialog from `sm` up. Every confirmation
 * and secondary form in the app goes through this so they all feel the same.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean
  onClose: () => void
  title?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  useBodyLock(open)

  useEffect(() => {
    if (!open) return
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm animate-fade-in" onClick={onClose} />
      <div
        className={clsx(
          'relative w-full sm:max-w-md bg-ink-900 border border-ink-700/70',
          'rounded-t-3xl sm:rounded-2.5xl shadow-sheet sm:shadow-modal',
          'animate-sheet-up sm:animate-pop-in max-h-[92dvh] flex flex-col',
        )}
      >
        <div className="sm:hidden pt-2.5 flex justify-center">
          <span className="w-10 h-1.5 rounded-full bg-ink-600" />
        </div>
        <div className="flex items-center justify-between gap-3 px-5 pt-3 sm:pt-5">
          {title && <h3 className="text-base font-semibold text-slate-50">{title}</h3>}
          <button
            type="button"
            onClick={onClose}
            className="hidden sm:flex w-9 h-9 -me-2 items-center justify-center rounded-lg text-slate-400 hover:bg-ink-800"
            aria-label="بستن"
          >
            <CloseIcon className="w-5 h-5" />
          </button>
        </div>
        {/* The bottom edge of a phone sheet must clear the home indicator; the
            padding carries the inset itself because a `.safe-b` class would
            lose to the pb-* utility. */}
        <div className={clsx('px-5 pt-4 overflow-y-auto grow', footer ? 'pb-4' : 'pb-[calc(1rem_+_var(--safe-bottom))]')}>
          {children}
        </div>
        {footer && (
          <div className="px-5 pt-1 pb-[calc(1rem_+_var(--safe-bottom))] flex flex-col sm:flex-row-reverse gap-2">{footer}</div>
        )}
      </div>
    </div>
  )
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmText = 'تایید',
  danger = false,
  requirePhrase,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  body: ReactNode
  confirmText?: string
  danger?: boolean
  requirePhrase?: string
  onConfirm: () => void
  onCancel: () => void
}) {
  const [typed, setTyped] = useState('')
  useEffect(() => {
    if (open) setTyped('')
  }, [open])

  const ready = !requirePhrase || typed.trim().toUpperCase() === requirePhrase.toUpperCase()

  return (
    <Sheet
      open={open}
      onClose={onCancel}
      title={title}
      footer={
        <>
          <button
            type="button"
            className={clsx(danger ? 'btn-danger' : 'btn-primary', 'sm:min-w-[9rem]')}
            disabled={!ready}
            onClick={onConfirm}
          >
            {confirmText}
          </button>
          <button type="button" className="btn-ghost" onClick={onCancel}>
            انصراف
          </button>
        </>
      }
    >
      <div className="text-sm text-slate-300 leading-7">{body}</div>
      {requirePhrase && (
        <div className="mt-4">
          <label className="label">
            برای تایید عبارت <span className="num text-accent font-semibold">{requirePhrase}</span> را بنویس
          </label>
          <input
            className="input num"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoFocus
            dir="ltr"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
        </div>
      )}
    </Sheet>
  )
}
