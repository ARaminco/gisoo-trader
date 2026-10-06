import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { auth, Me } from '@/lib/auth'
import { datetime } from '@/lib/format'
import { useSession } from '@/lib/session'
import { useInstallPrompt } from '@/hooks/useInstallPrompt'
import { ConfirmDialog, Dot } from '@/components/ui'
import { SECONDARY_NAV } from '@/components/Shell'
import { ChevronIcon, DownloadIcon, LogoutIcon } from '@/components/icons'

// Injected by Vite (vite.config.ts `define`): when this bundle was built and,
// when the build ran inside a git checkout, which commit.
declare const __BUILD_TIME__: string
declare const __BUILD_ID__: string

const DESCRIPTIONS: Record<string, string> = {
  '/committee': 'صورت‌جلسه‌ی دو بار در روز، پیشنهادها و بررسی سلامت',
  '/settings': 'ریسک، ورود و خروج، لایه‌های هوشمند، نمادها',
  '/notifications': 'اعلان روی گوشی، تلگرام، پایش IP',
  '/security': 'تایید دومرحله‌ای و خروج از دستگاه‌ها',
  '/backtest': 'اجرای استراتژی روی داده گذشته',
  '/guide': 'اتصال صرافی قدم به قدم',
}

export default function More() {
  const { signOut, connected } = useSession()
  const install = useInstallPrompt()
  const [me, setMe] = useState<Me | null>(null)
  const [confirmOut, setConfirmOut] = useState(false)
  const [leaving, setLeaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    auth
      .me()
      .then((result) => {
        if (!cancelled) setMe(result)
      })
      .catch(() => {
        /* older backend without /me */
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="space-y-4 max-w-xl mx-auto">
      {/* ---- account ---- */}
      <div className="card p-4 flex items-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-accent/15 text-accent flex items-center justify-center text-lg font-bold">
          {(me?.username ?? 'T').slice(0, 1).toUpperCase()}
        </div>
        <div className="grow min-w-0">
          <div className="text-sm font-semibold text-slate-50 truncate num text-right">
            {me?.username ?? 'مدیر پنل'}
          </div>
          <div className="flex items-center gap-1.5 text-2xs text-slate-400 mt-0.5">
            <Dot ok={connected} />
            {connected ? 'اتصال زنده به ربات' : 'اتصال زنده قطع است'}
            {me && (
              <span className={clsx('ms-2', me.totp_enabled ? 'text-profit' : 'text-warn')}>
                {me.totp_enabled ? 'تایید دومرحله‌ای فعال' : 'بدون تایید دومرحله‌ای'}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* ---- sections ---- */}
      <div className="card overflow-hidden">
        {SECONDARY_NAV.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="flex items-center gap-3 min-h-[60px] px-4 border-b border-ink-800 last:border-b-0 active:bg-ink-800/70 hover:bg-ink-850/60 transition-colors"
          >
            <span className="w-10 h-10 rounded-xl bg-ink-850 border border-ink-700/70 text-slate-200 flex items-center justify-center shrink-0">
              <item.icon className="w-5 h-5" />
            </span>
            <span className="grow min-w-0">
              <span className="block text-sm font-medium text-slate-50">{item.label}</span>
              <span className="block text-2xs text-slate-400 leading-4 truncate">{DESCRIPTIONS[item.to]}</span>
            </span>
            <ChevronIcon className="w-5 h-5 text-slate-500 shrink-0" />
          </Link>
        ))}
      </div>

      {/* ---- install ---- */}
      {!install.installed && (
        <button
          type="button"
          onClick={() => (install.canPrompt ? install.install() : undefined)}
          className="card w-full text-right p-4 flex items-center gap-3 active:bg-ink-800/70"
        >
          <span className="w-10 h-10 rounded-xl bg-accent/15 text-accent flex items-center justify-center shrink-0">
            <DownloadIcon className="w-5 h-5" />
          </span>
          <span className="grow min-w-0">
            <span className="block text-sm font-medium text-slate-50">نصب به‌عنوان اپ</span>
            <span className="block text-2xs text-slate-400 leading-4">
              {install.canPrompt
                ? 'با یک ضربه روی صفحه اصلی گوشی'
                : install.ios
                  ? 'در سافاری: Share ← Add to Home Screen'
                  : 'از منوی مرورگر گزینه «نصب اپ» را بزن'}
            </span>
          </span>
        </button>
      )}

      {/* ---- sign out ---- */}
      <button
        type="button"
        className="btn-danger-soft w-full min-h-[52px]"
        onClick={() => setConfirmOut(true)}
        disabled={leaving}
      >
        <LogoutIcon className="w-5 h-5" />
        خروج از حساب
      </button>

      <p className="text-center text-2xs text-slate-400 leading-5">
        Gisoo Trader · نسخه <span className="num">0.1.0</span>
        <span className="block mt-0.5">
          هسته‌ی متن‌باز؛ مسئولیت استفاده با خود شماست ·{' '}
          <a
            className="text-accent"
            href="https://github.com/ARaminco/gisoo-trader"
            target="_blank"
            rel="noopener noreferrer"
          >
            GitHub
          </a>
        </span>
        <span className="block mt-0.5">
          ساخت <span className="num">{datetime(__BUILD_TIME__)}</span>
          {__BUILD_ID__ && (
            <>
              {' · '}
              <span className="num">{__BUILD_ID__}</span>
            </>
          )}
        </span>
      </p>

      <ConfirmDialog
        open={confirmOut}
        title="خروج از حساب"
        body="از این دستگاه خارج می‌شوی. ربات به کار خودش ادامه می‌دهد."
        confirmText="خروج"
        danger
        onConfirm={async () => {
          setConfirmOut(false)
          setLeaving(true)
          await signOut()
        }}
        onCancel={() => setConfirmOut(false)}
      />
    </div>
  )
}
