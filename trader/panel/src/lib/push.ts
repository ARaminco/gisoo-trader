/**
 * Web Push on this device: permission, the browser subscription, and its
 * registration with the engine. The engine signs pushes with a VAPID key it
 * hands out through the API; the service worker (src/sw.ts) shows them.
 *
 * iOS: Safari only delivers Web Push to a PWA installed on the home screen
 * (iOS 16.4+), never to a tab - `iosNeedsInstall()` tells the page to say so.
 */
import { api } from '@/lib/api'

export type PermissionState = NotificationPermission | 'unsupported'

const READY_TIMEOUT_MS = 8000

export function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  )
}

export function isIos(): boolean {
  const ua = navigator.userAgent
  const iPadOs = navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
  return /iPhone|iPad|iPod/i.test(ua) || iPadOs
}

export function isStandalone(): boolean {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches ||
    (navigator as any).standalone === true
  )
}

/** True when this is iOS Safari in a tab: push cannot work until installed. */
export function iosNeedsInstall(): boolean {
  return isIos() && !isStandalone()
}

export function permissionState(): PermissionState {
  if (!('Notification' in window)) return 'unsupported'
  return Notification.permission
}

/** The active registration, or a clear error instead of hanging forever (dev mode has no worker). */
async function registration(): Promise<ServiceWorkerRegistration> {
  if (!('serviceWorker' in navigator)) throw new Error('این مرورگر سرویس‌ورکر ندارد')
  const ready = navigator.serviceWorker.ready
  const timeout = new Promise<never>((_, reject) =>
    window.setTimeout(() => reject(new Error('سرویس‌ورکر اپ آماده نشد؛ صفحه را دوباره باز کن')), READY_TIMEOUT_MS),
  )
  return Promise.race([ready, timeout])
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!isSupported()) return null
  try {
    const reg = await registration()
    return await reg.pushManager.getSubscription()
  } catch {
    return null
  }
}

/** Backed by a plain ArrayBuffer: `subscribe()` refuses a SharedArrayBuffer-typed view. */
function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalised = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(normalised)
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i)
  return bytes
}

/** Ask for permission, subscribe with the engine's VAPID key, register the device. */
export async function subscribe(): Promise<PushSubscription> {
  if (!isSupported()) throw new Error('این مرورگر از اعلان پوش پشتیبانی نمی‌کند')
  if (iosNeedsInstall()) throw new Error('روی آیفون اول اپ را به صفحه اصلی اضافه کن و از آنجا باز کن')

  const permission = await Notification.requestPermission()
  if (permission !== 'granted') {
    throw new Error(
      permission === 'denied'
        ? 'اجازه اعلان رد شده است؛ از تنظیمات مرورگر یا سیستم برای این سایت روشنش کن'
        : 'اجازه اعلان داده نشد',
    )
  }

  const { public_key } = await api.notifyVapidKey()
  const reg = await registration()
  let subscription = await reg.pushManager.getSubscription()
  if (subscription) {
    // A subscription made with a previous key cannot be used with this one.
    const existing = subscription.options.applicationServerKey
    const wanted = urlBase64ToUint8Array(public_key)
    const same =
      existing != null &&
      existing.byteLength === wanted.byteLength &&
      new Uint8Array(existing).every((byte, index) => byte === wanted[index])
    if (!same) {
      await subscription.unsubscribe()
      subscription = null
    }
  }
  if (!subscription) {
    subscription = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    })
  }

  const json = subscription.toJSON()
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error('مرورگر اشتراک ناقصی برگرداند')
  }
  await api.notifySubscribe({
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
    user_agent: navigator.userAgent.slice(0, 255),
  })
  return subscription
}

/** Forget this device on the engine, then in the browser. */
export async function unsubscribe(): Promise<void> {
  const subscription = await currentSubscription()
  if (!subscription) return
  try {
    await api.notifyUnsubscribe(subscription.endpoint)
  } catch {
    /* the engine may be unreachable; the browser side still goes */
  }
  await subscription.unsubscribe()
}
