import { ReactNode, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import { useSession } from '@/lib/session'
import { useConnectivity } from '@/lib/net'
import { useInstallPrompt } from '@/hooks/useInstallPrompt'
import { Dot, Toaster } from '@/components/ui'
import {
  BackIcon,
  BellIcon,
  BookIcon,
  CloseIcon,
  CommitteeIcon,
  DownloadIcon,
  FlaskIcon,
  GearIcon,
  HomeIcon,
  IconProps,
  LogoutIcon,
  MoreIcon,
  ShareIcon,
  ShieldIcon,
  SignalIcon,
  TradesIcon,
  WalletIcon,
  WifiOffIcon,
} from '@/components/icons'

type NavItem = {
  to: string
  label: string
  icon: (props: IconProps) => JSX.Element
  end?: boolean
}

/** The five thumb tabs on a phone. */
export const PRIMARY_NAV: NavItem[] = [
  { to: '/', label: 'داشبورد', icon: HomeIcon, end: true },
  { to: '/trades', label: 'معاملات', icon: TradesIcon },
  { to: '/signals', label: 'سیگنال‌ها', icon: SignalIcon },
  { to: '/capital', label: 'سرمایه', icon: WalletIcon },
  { to: '/more', label: 'بیشتر', icon: MoreIcon },
]

/** Reached through "بیشتر" on a phone, listed directly on wider screens. */
export const SECONDARY_NAV: NavItem[] = [
  { to: '/committee', label: 'کمیته', icon: CommitteeIcon },
  { to: '/settings', label: 'تنظیمات', icon: GearIcon },
  { to: '/notifications', label: 'اعلان‌ها', icon: BellIcon },
  { to: '/security', label: 'امنیت', icon: ShieldIcon },
  { to: '/backtest', label: 'بک‌تست', icon: FlaskIcon },
  { to: '/guide', label: 'راهنما', icon: BookIcon },
]

function titleFor(pathname: string): { title: string; secondary: boolean } {
  const all = [...PRIMARY_NAV, ...SECONDARY_NAV]
  const item = all.find((entry) =>
    entry.end ? pathname === entry.to : pathname.startsWith(entry.to),
  )
  return {
    title: item?.label ?? 'Gisoo Trader',
    secondary: SECONDARY_NAV.some((entry) => pathname.startsWith(entry.to)),
  }
}

export default function Shell({ children }: { children: ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const { connected, signOut } = useSession()
  const { title, secondary } = titleFor(location.pathname)

  return (
    <div className="app-shell flex">
      {/* ---- desktop sidebar (lg+) ---- */}
      <aside className="hidden lg:flex lg:flex-col w-60 shrink-0 border-e border-ink-800 bg-ink-900 p-4 h-full">
        <Brand connected={connected} />
        <nav className="space-y-1 grow mt-6">
          {PRIMARY_NAV.filter((item) => item.to !== '/more').map((item) => (
            <SideLink key={item.to} item={item} />
          ))}
          <div className="h-px bg-ink-800 my-3" />
          {SECONDARY_NAV.map((item) => (
            <SideLink key={item.to} item={item} />
          ))}
        </nav>
        <button
          onClick={() => signOut()}
          className="flex items-center gap-3 px-3 min-h-[44px] rounded-xl text-sm text-slate-400 hover:bg-ink-800 hover:text-loss transition-colors"
        >
          <LogoutIcon className="w-5 h-5" />
          خروج
        </button>
      </aside>

      {/* ---- tablet rail (md to lg) ---- */}
      <aside className="hidden md:flex lg:hidden flex-col items-center w-[4.75rem] shrink-0 border-e border-ink-800 bg-ink-900 py-3 gap-1 h-full">
        <img src="/brand/mark.svg" alt="" width={40} height={40} className="w-10 h-10 mb-3" />
        {[...PRIMARY_NAV.filter((item) => item.to !== '/more'), ...SECONDARY_NAV].map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              clsx(
                'w-16 flex flex-col items-center gap-1 py-2 rounded-xl text-2xs transition-colors',
                isActive ? 'bg-accent/15 text-accent' : 'text-slate-400 hover:bg-ink-800 hover:text-slate-100',
              )
            }
          >
            <item.icon className="w-5 h-5" />
            {item.label}
          </NavLink>
        ))}
        <span className="grow" />
        <button
          onClick={() => signOut()}
          className="w-16 flex flex-col items-center gap-1 py-2 rounded-xl text-2xs text-slate-400 hover:text-loss"
          aria-label="خروج"
        >
          <LogoutIcon className="w-5 h-5" />
          خروج
        </button>
      </aside>

      <div className="grow min-w-0 flex flex-col min-h-0">
        {/* ---- phone / tablet header: a flex child, not sticky - only <main> scrolls ---- */}
        <header className="lg:hidden shrink-0 z-30 bg-ink-950 border-b border-ink-800 safe-t">
          <div className="h-header px-3 flex items-center gap-2">
            {secondary ? (
              <button
                onClick={() => navigate('/more')}
                className="md:hidden w-11 h-11 -ms-1 flex items-center justify-center rounded-xl text-slate-200 active:bg-ink-800"
                aria-label="بازگشت"
              >
                <BackIcon className="w-6 h-6" />
              </button>
            ) : (
              <img src="/brand/mark.svg" alt="" width={32} height={32} className="md:hidden w-8 h-8 ms-1" />
            )}
            <h1 className="text-base font-semibold text-slate-50 grow truncate md:ms-1">{title}</h1>
            <span
              className={clsx(
                'flex items-center gap-1.5 text-2xs px-2.5 h-7 rounded-full border',
                connected
                  ? 'text-profit border-profit/30 bg-profit/10'
                  : 'text-slate-300 border-ink-700 bg-ink-850',
              )}
              title={connected ? 'اتصال زنده به ربات برقرار است' : 'اتصال زنده قطع است؛ اعداد با فاصله چند ثانیه به‌روز می‌شوند'}
            >
              <Dot ok={connected} live />
              {connected ? 'زنده' : 'قطع'}
            </span>
          </div>
        </header>

        {/* The only scroll container. The shell is viewport-sized, so the header
            and the tab bar are ordinary flex children: iOS Safari detaches
            position:fixed bars mid-page while scrolling, which is exactly what a
            bottom bar must never do. */}
        <main className="app-main grow min-h-0 w-full px-4 pt-4 pb-6 sm:px-6 sm:pt-6 md:pb-8">
          <div className="max-w-[1400px] mx-auto">
            <OfflineBanner />
            {location.pathname !== '/more' && <InstallBanner />}
            {children}
          </div>
        </main>

        {/* ---- phone tab bar ---- */}
        <nav
          className="md:hidden shrink-0 z-30 bg-ink-950 border-t border-ink-800 safe-b"
          aria-label="ناوبری اصلی"
        >
        <div className="flex h-tabbar">
          {PRIMARY_NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                clsx(
                  'flex-1 flex flex-col items-center justify-center gap-1 text-2xs font-medium transition-colors',
                  isActive || (item.to === '/more' && secondary) ? 'text-accent' : 'text-slate-400',
                )
              }
            >
              {({ isActive }) => {
                const active = isActive || (item.to === '/more' && secondary)
                return (
                  <>
                    <span
                      className={clsx(
                        'w-14 h-7 rounded-full flex items-center justify-center transition-colors',
                        active && 'bg-accent/15',
                      )}
                    >
                      <item.icon className="w-[22px] h-[22px]" strokeWidth={active ? 2.2 : 1.9} />
                    </span>
                    {item.label}
                  </>
                )
              }}
            </NavLink>
          ))}
          </div>
        </nav>
      </div>

      <Toaster />
    </div>
  )
}

function Brand({ connected }: { connected: boolean }) {
  return (
    <div className="flex items-center gap-3 px-2">
      <img src="/brand/mark.svg" alt="" width={40} height={40} className="w-10 h-10" />
      <div>
        <div className="text-sm font-semibold text-slate-50">Gisoo Trader</div>
        <div className="flex items-center gap-1.5 text-2xs text-slate-400">
          <Dot ok={connected} live />
          {connected ? 'اتصال زنده' : 'اتصال قطع است'}
        </div>
      </div>
    </div>
  )
}

function SideLink({ item }: { item: NavItem }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) =>
        clsx(
          'flex items-center gap-3 px-3 min-h-[44px] rounded-xl text-sm transition-colors',
          isActive ? 'bg-accent/15 text-accent font-medium' : 'text-slate-300 hover:bg-ink-800 hover:text-slate-50',
        )
      }
    >
      <item.icon className="w-5 h-5 shrink-0" />
      {item.label}
    </NavLink>
  )
}

/* ---------------------------------------------------------------- banners */

function OfflineBanner() {
  const { online, apiReachable } = useConnectivity()
  if (online && apiReachable) return null
  return (
    <div className="mb-4 rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 flex items-center gap-3 text-sm text-warn" role="status">
      <WifiOffIcon className="w-5 h-5 shrink-0" />
      <div className="leading-6">
        {!online
          ? 'اینترنت قطع است. اعداد این صفحه تا برقراری اتصال به‌روز نمی‌شوند.'
          : 'سرور ربات در دسترس نیست. اعداد ممکن است قدیمی باشند؛ به‌محض اتصال دوباره به‌روز می‌شوند.'}
      </div>
    </div>
  )
}

function InstallBanner() {
  const install = useInstallPrompt()
  const [showIosSteps, setShowIosSteps] = useState(false)
  if (!install.visible) return null

  return (
    <div className="mb-3 rounded-xl border border-accent/25 bg-accent/10 ps-3 pe-1 py-1 flex items-center gap-2 text-xs">
      <DownloadIcon className="w-4 h-4 text-accent shrink-0" />
      <div className="grow min-w-0 text-slate-100 leading-5">
        {showIosSteps ? (
          <>
            در سافاری <ShareIcon className="inline w-3.5 h-3.5 align-middle text-accent" /> را بزن و «Add to Home Screen» را انتخاب کن
          </>
        ) : (
          <>
            نصب به‌عنوان اپ
            <span className="text-slate-400 hidden sm:inline"> — تمام‌صفحه و با آیکون روی صفحه اصلی</span>
          </>
        )}
      </div>
      {install.canPrompt ? (
        <button className="btn-primary btn-sm !min-h-[32px] shrink-0" onClick={() => install.install()}>
          نصب
        </button>
      ) : (
        !showIosSteps && (
          <button className="btn-ghost btn-sm !min-h-[32px] shrink-0" onClick={() => setShowIosSteps(true)}>
            چطور؟
          </button>
        )
      )}
      <button
        className="w-9 h-9 flex items-center justify-center rounded-lg text-slate-400 hover:bg-ink-800 shrink-0"
        onClick={install.dismiss}
        aria-label="بستن"
      >
        <CloseIcon className="w-4 h-4" />
      </button>
    </div>
  )
}
