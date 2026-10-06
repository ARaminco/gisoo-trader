import { useCallback, useEffect, useMemo, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api, getToken } from '@/lib/api'
import { quoteLabel, setQuoteAsset } from '@/lib/format'
import { signOut as serverSignOut } from '@/lib/auth'
import { SessionContext } from '@/lib/session'
import { useLiveFeed } from '@/hooks/usePolling'
import { Toaster, toast } from '@/components/ui'
import Shell from '@/components/Shell'
import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import Signals from '@/pages/Signals'
import Trades from '@/pages/Trades'
import Capital from '@/pages/Capital'
import Backtest from '@/pages/Backtest'
import Settings from '@/pages/Settings'
import Security from '@/pages/Security'
import Notifications from '@/pages/Notifications'
import More from '@/pages/More'
import Guide from '@/pages/Guide'
import Committee from '@/pages/Committee'

export default function App() {
  const [authed, setAuthed] = useState(() => Boolean(getToken()))
  // Re-render once the server's quote asset is known (USDT or IRT/Toman).
  const [, setQuoteReady] = useState(false)

  useEffect(() => {
    if (!authed) return
    let alive = true
    api
      .settings()
      .then((res) => {
        if (!alive) return
        setQuoteAsset(res.static.quote_asset)
        setQuoteReady(true)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [authed])

  useEffect(() => {
    const onUnauthorized = () => {
      setAuthed(false)
      toast('نشست تمام شد. دوباره وارد شو', 'error')
    }
    window.addEventListener('gtr:unauthorized', onUnauthorized)
    return () => window.removeEventListener('gtr:unauthorized', onUnauthorized)
  }, [])

  const onEvent = useCallback((event: { channel: string; data: any }) => {
    if (event.channel !== 'gtr:events') return
    const payload = event.data
    if (payload.type === 'position_opened') {
      toast(`پوزیشن ${payload.side === 'long' ? 'خرید' : 'فروش'} ${payload.symbol} باز شد`)
    } else if (payload.type === 'position_closed') {
      const pnl = Number(payload.pnl ?? 0)
      toast(
        `${payload.symbol} بسته شد — ${pnl >= 0 ? 'سود' : 'ضرر'} ${Math.abs(pnl).toFixed(3)} ${quoteLabel()}`,
        pnl >= 0 ? 'ok' : 'error',
      )
    } else if (payload.type === 'kill_switch') {
      toast('توقف اضطراری فعال شد', 'error')
    } else if (payload.type === 'committee') {
      toast(payload.health_ok ? 'صورت‌جلسه‌ی کمیته آماده شد' : 'صورت‌جلسه‌ی کمیته: بررسی سلامت مشکل دارد', payload.health_ok ? 'ok' : 'error')
    }
  }, [])

  const { connected, marks } = useLiveFeed(authed ? onEvent : undefined)

  const signOut = useCallback(async (everywhere = false) => {
    await serverSignOut(everywhere)
    setAuthed(false)
  }, [])

  const session = useMemo(() => ({ signOut, connected }), [signOut, connected])

  if (!authed) {
    return (
      <>
        <Login onSuccess={() => setAuthed(true)} />
        <Toaster />
      </>
    )
  }

  return (
    <SessionContext.Provider value={session}>
      <Shell>
        <Routes>
          <Route path="/" element={<Dashboard marks={marks} />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/history" element={<Navigate to="/trades" replace />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/capital" element={<Capital />} />
          <Route path="/more" element={<More />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/security" element={<Security />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/guide" element={<Guide />} />
          <Route path="/committee" element={<Committee />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </SessionContext.Provider>
  )
}
