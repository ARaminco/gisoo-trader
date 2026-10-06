import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getToken } from '@/lib/api'
import { reportReachability } from '@/lib/net'

/** Poll an async function on an interval, pausing when the tab is hidden. */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs = 5000,
  deps: unknown[] = [],
) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const mounted = useRef(true)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const refresh = useCallback(async () => {
    try {
      const result = await fetcherRef.current()
      if (!mounted.current) return
      setData(result)
      setError(null)
      setUpdatedAt(Date.now())
      reportReachability(true)
    } catch (err) {
      if (!mounted.current) return
      // A structured error means the server answered; anything else is the network.
      if (err instanceof ApiError && err.status > 0) reportReachability(true)
      else reportReachability(false)
      setError(err instanceof Error ? err.message : 'خطای ناشناخته')
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    refresh()

    let timer: number | undefined
    const tick = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    timer = window.setInterval(tick, intervalMs)

    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onVisible)

    return () => {
      mounted.current = false
      if (timer) window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps])

  return { data, error, loading, refresh, updatedAt }
}

export type LiveEvent = { channel: string; data: any }

/** Live event feed from the engine. Reconnects with backoff. */
export function useLiveFeed(onEvent?: (event: LiveEvent) => void) {
  const [connected, setConnected] = useState(false)
  const [marks, setMarks] = useState<Record<string, number>>({})
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    let socket: WebSocket | null = null
    let retry: number | undefined
    let attempt = 0
    let closed = false

    const connect = () => {
      const token = getToken()
      if (!token || closed) return

      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${location.host}/ws?token=${encodeURIComponent(token)}`)

      socket.onopen = () => {
        attempt = 0
        setConnected(true)
      }

      socket.onmessage = (message) => {
        try {
          const parsed: LiveEvent = JSON.parse(message.data)
          if (parsed.channel === 'gtr:ticks' && parsed.data?.symbol) {
            setMarks((current) => ({ ...current, [parsed.data.symbol]: parsed.data.price }))
          }
          handlerRef.current?.(parsed)
        } catch {
          /* ignore malformed frames */
        }
      }

      socket.onclose = () => {
        setConnected(false)
        if (closed) return
        attempt += 1
        retry = window.setTimeout(connect, Math.min(1000 * 2 ** attempt, 20000))
      }

      socket.onerror = () => socket?.close()
    }

    connect()

    // The heartbeat keeps intermediaries from dropping an idle socket.
    const ping = window.setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send('ping')
    }, 25000)

    return () => {
      closed = true
      window.clearInterval(ping)
      if (retry) window.clearTimeout(retry)
      socket?.close()
    }
  }, [])

  return { connected, marks }
}
