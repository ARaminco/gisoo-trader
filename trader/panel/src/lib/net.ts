/**
 * Reachability of the engine API, fed by every poll. Lets the shell show one
 * honest "offline" banner instead of an error in every card.
 */
import { useEffect, useState } from 'react'

type Listener = (reachable: boolean) => void

let reachable = true
let lastOkAt = Date.now()
const listeners = new Set<Listener>()

export function reportReachability(ok: boolean) {
  if (ok) lastOkAt = Date.now()
  if (ok === reachable) return
  reachable = ok
  listeners.forEach((listener) => listener(ok))
}

export function useConnectivity() {
  const [apiReachable, setApiReachable] = useState(reachable)
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)

  useEffect(() => {
    listeners.add(setApiReachable)
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    return () => {
      listeners.delete(setApiReachable)
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
    }
  }, [])

  return { online, apiReachable, lastOkAt }
}
