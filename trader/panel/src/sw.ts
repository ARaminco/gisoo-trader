/// <reference lib="webworker" />
/**
 * The panel's service worker (built by vite-plugin-pwa, injectManifest).
 *
 * Caching is exactly what the generated worker used to do: the app shell is
 * precached, `/api` and `/ws` are never cached (stale money is worse than
 * no money), the Persian font is cached so the shell opens offline. On top
 * of that it receives Web Push messages from the engine and shows them.
 */
import { clientsClaim } from 'workbox-core'
import { cleanupOutdatedCaches, createHandlerBoundToURL, precacheAndRoute } from 'workbox-precaching'
import { NavigationRoute, registerRoute } from 'workbox-routing'
import { CacheFirst, NetworkOnly, StaleWhileRevalidate } from 'workbox-strategies'
import { ExpirationPlugin } from 'workbox-expiration'
import { CacheableResponsePlugin } from 'workbox-cacheable-response'

declare let self: ServiceWorkerGlobalScope

// autoUpdate: a new build takes over open tabs at once instead of waiting.
self.skipWaiting()
clientsClaim()

cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST)

// SPA navigation falls back to the shell, except for the engine's own paths.
registerRoute(
  new NavigationRoute(createHandlerBoundToURL('index.html'), {
    denylist: [/^\/api/, /^\/ws/],
  }),
)

// Trading data must never come from a cache.
registerRoute(({ url }) => /^\/(api|ws)(\/|$)/.test(url.pathname), new NetworkOnly())

// The Persian face keeps working when the app is opened offline.
registerRoute(
  /^https:\/\/fonts\.googleapis\.com\/.*/i,
  new StaleWhileRevalidate({ cacheName: 'google-fonts-stylesheets' }),
)
registerRoute(
  /^https:\/\/fonts\.gstatic\.com\/.*/i,
  new CacheFirst({
    cacheName: 'google-fonts-webfonts',
    plugins: [
      new CacheableResponsePlugin({ statuses: [0, 200] }),
      new ExpirationPlugin({ maxEntries: 20, maxAgeSeconds: 60 * 60 * 24 * 365 }),
    ],
  }),
)

/* ---------------------------------------------------------------- push */

interface PushPayload {
  title?: string
  body?: string
  category?: string
  level?: string
  url?: string
  tag?: string
  silent?: boolean
}

function parsePayload(event: PushEvent): PushPayload {
  if (!event.data) return {}
  try {
    return event.data.json() as PushPayload
  } catch {
    return { body: event.data.text() }
  }
}

self.addEventListener('push', (event: PushEvent) => {
  const payload = parsePayload(event)
  const title = payload.title || 'Gisoo Trader'
  const options: NotificationOptions = {
    body: payload.body || '',
    icon: '/icon-192.png',
    // Android renders the badge as a monochrome silhouette; a colour icon
    // there turns into a grey blob.
    badge: '/badge-96.png',
    dir: 'rtl',
    lang: 'fa',
    // Same tag = the same alert again; it replaces instead of piling up.
    tag: payload.tag || `gtr-${payload.category || 'system'}`,
    silent: payload.silent === true,
    // A risk or exchange error stays on the lock screen until the user sees it.
    requireInteraction: payload.level === 'error',
    data: { url: payload.url || '/', category: payload.category, level: payload.level },
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event: NotificationEvent) => {
  event.notification.close()
  const target = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin)
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      // Prefer a tab that is already open: focus it and steer it to the page.
      for (const client of windows) {
        if (new URL(client.url).origin !== self.location.origin) continue
        if ('navigate' in client) {
          try {
            await (client as WindowClient).navigate(target.href)
          } catch {
            /* a cross-document navigate may be refused; focusing is still right */
          }
        }
        return (client as WindowClient).focus()
      }
      return self.clients.openWindow(target.href)
    })(),
  )
})
