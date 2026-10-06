import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { toast } from '@/components/ui'
import App from './App'
import './index.css'

/**
 * Service worker registration and the update flow.
 *
 * src/sw.ts calls skipWaiting() + clientsClaim(), so a new build takes
 * control the moment it is installed (the "autoUpdate" behaviour); the
 * JavaScript already running in the page stays the old one until a reload.
 * Two things the default registration does not do, and an installed iOS app
 * needs:
 *
 *  - look for a new build whenever the app comes back to the foreground
 *    (a home-screen app can stay alive for days without a navigation, and
 *    the browser's own check only runs on navigation / every 24 h);
 *  - once a new build controls the page, reload at once if nobody is
 *    looking, otherwise offer the reload in a toast so a half-typed form or
 *    an open confirmation is not thrown away underneath the user.
 */
const UPDATE_CHECK_MS = 60 * 60 * 1000

function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || !import.meta.env.PROD) return
  const hadController = Boolean(navigator.serviceWorker.controller)
  let notified = false

  navigator.serviceWorker.addEventListener('controllerchange', () => {
    // The very first install also fires this (clientsClaim): nothing to replace.
    if (!hadController || notified) return
    notified = true
    if (document.visibilityState !== 'visible') {
      window.location.reload()
      return
    }
    toast('نسخه‌ی جدید آماده است', 'ok', {
      id: 'sw-update',
      sticky: true,
      action: { label: 'به‌روزرسانی', onClick: () => window.location.reload() },
    })
  })

  navigator.serviceWorker
    .register('/sw.js', { scope: '/' })
    .then((registration) => {
      const check = () => {
        if (document.visibilityState === 'visible') registration.update().catch(() => undefined)
      }
      window.setInterval(check, UPDATE_CHECK_MS)
      document.addEventListener('visibilitychange', check)

      registration.addEventListener('updatefound', () => {
        const worker = registration.installing
        if (!worker) return
        worker.addEventListener('statechange', () => {
          if (worker.state === 'installed' && !navigator.serviceWorker.controller) {
            toast('اپ برای کار آفلاین آماده شد')
          }
        })
      })
    })
    .catch(() => {
      /* a failed registration must never take the panel down */
    })
}

registerServiceWorker()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
