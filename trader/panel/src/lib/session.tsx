import { createContext, useContext } from 'react'

export interface Session {
  /** Sign out of this device (or every device) and return to the login screen. */
  signOut: (everywhere?: boolean) => Promise<void>
  /** Live engine socket state, shown in the header. */
  connected: boolean
}

export const SessionContext = createContext<Session>({
  signOut: async () => undefined,
  connected: false,
})

export function useSession() {
  return useContext(SessionContext)
}
