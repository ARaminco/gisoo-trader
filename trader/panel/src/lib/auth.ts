/**
 * Auth endpoints. Kept apart from api.ts because login must not trigger the
 * global "session expired" handling that api.ts applies to every 401, and
 * because a wrong TOTP code is a form error, not a lost session.
 */
import { ApiError, getToken, setToken } from '@/lib/api'

export interface LoginSuccess {
  access_token: string
  token_type?: string
  expires_in: number
}

export interface TotpChallenge {
  requires_totp: true
  challenge: string
  expires_in: number
}

export type LoginResponse = LoginSuccess | TotpChallenge

export interface Me {
  username: string
  totp_enabled: boolean
  token_expires_at: string | null
  /** Passkeys enrolled for every host together (see `Passkey.rp_id`). */
  passkeys_enrolled: number
  /** The password form is refused while this is on and a key exists. */
  passkey_required: boolean
}

/** One enrolled WebAuthn credential (hardware security key or phone passkey). */
export interface Passkey {
  id: number
  name: string
  /** Host the key is bound to: it is only offered when the panel is opened there. */
  rp_id: string
  transports: string[]
  aaguid: string | null
  created_at: string
  last_used_at: string | null
  backup_eligible: boolean
  backed_up: boolean
}

export interface PasskeyListResponse {
  passkeys: Passkey[]
  passkey_required: boolean
}

/** Options for `navigator.credentials.create/get` (binary fields base64url) plus the ceremony handle. */
export interface PasskeyOptions {
  state: string
  options: Record<string, any>
}

export interface TotpSetup {
  secret: string
  otpauth_uri: string
  qr_png_base64: string
}

/** 423: too many failed attempts. `retryAfter` is in seconds. */
export class LockoutError extends ApiError {
  constructor(message: string, public retryAfter: number) {
    super(423, message)
  }
}

export function isTotpChallenge(response: LoginResponse): response is TotpChallenge {
  return (response as TotpChallenge).requires_totp === true
}

type Options = {
  method?: 'POST' | 'PATCH' | 'DELETE'
  /** Attach the bearer token. */
  bearer?: boolean
  /** What a 401 means here: a dead session (log the app out) or a rejected input. */
  unauthorized?: 'expire' | 'reject'
}

async function readDetail(response: Response): Promise<Record<string, any>> {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

async function send<T>(path: string, body?: unknown, options: Options = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (options.bearer !== false) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  let response: Response
  try {
    response = await fetch(path, {
      method: options.method ?? 'POST',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'ارتباط با سرور برقرار نشد')
  }

  if (response.ok) {
    if (response.status === 204) return undefined as T
    return response.json() as Promise<T>
  }

  const payload = await readDetail(response)
  const detail =
    typeof payload.detail === 'string'
      ? payload.detail
      : payload.detail
        ? JSON.stringify(payload.detail)
        : `خطای ${response.status}`

  if (response.status === 423) {
    const retry = Number(payload.retry_after_seconds ?? 60)
    throw new LockoutError(detail, Number.isFinite(retry) ? retry : 60)
  }

  if (response.status === 401 && options.unauthorized === 'expire') {
    setToken(null)
    window.dispatchEvent(new CustomEvent('gtr:unauthorized'))
    throw new ApiError(401, 'نشست منقضی شده است')
  }

  throw new ApiError(response.status, detail)
}

async function get<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(path, { headers })
  } catch {
    throw new ApiError(0, 'ارتباط با سرور برقرار نشد')
  }

  if (response.status === 401) {
    setToken(null)
    window.dispatchEvent(new CustomEvent('gtr:unauthorized'))
    throw new ApiError(401, 'نشست منقضی شده است')
  }
  if (!response.ok) {
    const payload = await readDetail(response)
    throw new ApiError(
      response.status,
      typeof payload.detail === 'string' ? payload.detail : `خطای ${response.status}`,
    )
  }
  return response.json() as Promise<T>
}

export const auth = {
  login: (username: string, password: string) =>
    send<LoginResponse>('/api/auth/login', { username, password }, { bearer: false, unauthorized: 'reject' }),

  loginTotp: (challenge: string, code: string) =>
    send<LoginSuccess>('/api/auth/login/totp', { challenge, code }, { bearer: false, unauthorized: 'reject' }),

  /** Server-side logout. Callers clear the local token afterwards regardless of the result. */
  logout: () => send<void>('/api/auth/logout', undefined, { unauthorized: 'reject' }),

  logoutAll: () => send<void>('/api/auth/logout-all', undefined, { unauthorized: 'reject' }),

  me: () => get<Me>('/api/auth/me'),

  totpSetup: () => send<TotpSetup>('/api/auth/totp/setup', undefined, { unauthorized: 'expire' }),

  totpEnable: (code: string) =>
    send<{ enabled: boolean; backup_codes: string[] }>('/api/auth/totp/enable', { code }, { unauthorized: 'reject' }),

  totpDisable: (code: string) =>
    send<{ enabled: boolean }>('/api/auth/totp/disable', { code }, { unauthorized: 'reject' }),

  // ---- passkeys (the ceremonies themselves live in lib/passkey.ts)

  /** Whether `username` has a key for this host. The server answers after a fixed delay. */
  passkeyAvailable: (username: string) =>
    get<{ available: boolean }>(`/api/auth/passkey/available?username=${encodeURIComponent(username)}`),

  passkeyLoginOptions: (username: string) =>
    send<PasskeyOptions>('/api/auth/passkey/login/options', { username }, { bearer: false, unauthorized: 'reject' }),

  passkeyLoginVerify: (state: string, credential: unknown) =>
    send<LoginSuccess>('/api/auth/passkey/login/verify', { state, credential }, { bearer: false, unauthorized: 'reject' }),

  passkeyRegisterOptions: () =>
    send<PasskeyOptions>('/api/auth/passkey/register/options', undefined, { unauthorized: 'expire' }),

  passkeyRegisterVerify: (state: string, credential: unknown, name: string) =>
    send<PasskeyListResponse>('/api/auth/passkey/register/verify', { state, credential, name }, { unauthorized: 'expire' }),

  passkeys: () => get<PasskeyListResponse>('/api/auth/passkeys'),

  deletePasskey: (id: number) =>
    send<PasskeyListResponse>(`/api/auth/passkeys/${id}`, undefined, { method: 'DELETE', unauthorized: 'expire' }),

  /** Passkey-only login. The server refuses to turn it on with no key enrolled. */
  setPasskeyRequired: (enabled: boolean) =>
    send<PasskeyListResponse>('/api/auth/passkey/required', { enabled }, { method: 'PATCH', unauthorized: 'expire' }),
}

/** Log out of this device: tell the server, then forget the token no matter what. */
export async function signOut(everywhere = false) {
  try {
    if (everywhere) await auth.logoutAll()
    else await auth.logout()
  } catch {
    /* an old backend or a dead network must not trap the user in the app */
  } finally {
    setToken(null)
  }
}
