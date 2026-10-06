/**
 * Passkey / FIDO2 (WebAuthn) ceremonies: a hardware security key or the
 * phone's own passkey. The server builds the options and
 * verifies the answers; this file only moves bytes between the JSON the API
 * speaks (base64url) and the ArrayBuffers `navigator.credentials` wants, and
 * turns the browser's DOMExceptions into Persian.
 */
import { LoginSuccess, PasskeyListResponse, auth } from '@/lib/auth'

export type PasskeyErrorCode = 'unsupported' | 'cancelled' | 'duplicate' | 'security' | 'failed'

export class PasskeyError extends Error {
  constructor(
    public code: PasskeyErrorCode,
    message: string,
  ) {
    super(message)
  }
}

const UNSUPPORTED = 'این مرورگر یا دستگاه از کلید امنیتی پشتیبانی نمی‌کند (HTTPS یا localhost لازم است)'
const NO_ANSWER = 'کلید امنیتی پاسخی نداد'
const RETRY_HINT = 'کلید امنیتی را وصل کن، آماده نگه دار و دوباره تلاش کن'

/** WebAuthn only exists in secure contexts: HTTPS, or localhost. */
export function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext &&
    'PublicKeyCredential' in window &&
    typeof navigator.credentials?.create === 'function' &&
    typeof navigator.credentials?.get === 'function'
  )
}

/* ------------------------------------------------------------------ base64url */

export function toBase64Url(data: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    data instanceof ArrayBuffer
      ? new Uint8Array(data)
      : new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
  let binary = ''
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function fromBase64Url(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/')
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
  const binary = atob(padded)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

/* ------------------------------------------------------------------ options */

type JsonDescriptor = { id: string; type: string; transports?: string[] }

function descriptors(list: JsonDescriptor[] | undefined): PublicKeyCredentialDescriptor[] | undefined {
  if (!list?.length) return undefined
  return list.map((entry) => ({
    id: fromBase64Url(entry.id),
    type: 'public-key',
    transports: entry.transports as AuthenticatorTransport[] | undefined,
  }))
}

function creationOptions(json: Record<string, any>): PublicKeyCredentialCreationOptions {
  return {
    rp: json.rp,
    user: {
      id: fromBase64Url(json.user.id),
      name: json.user.name,
      displayName: json.user.displayName ?? json.user.name,
    },
    challenge: fromBase64Url(json.challenge),
    pubKeyCredParams: json.pubKeyCredParams,
    timeout: json.timeout,
    excludeCredentials: descriptors(json.excludeCredentials),
    authenticatorSelection: json.authenticatorSelection,
    attestation: json.attestation,
  }
}

function requestOptions(json: Record<string, any>): PublicKeyCredentialRequestOptions {
  return {
    challenge: fromBase64Url(json.challenge),
    timeout: json.timeout,
    rpId: json.rpId,
    allowCredentials: descriptors(json.allowCredentials),
    userVerification: json.userVerification,
  }
}

/* ------------------------------------------------------------------ answers */

/** The `create()` result as the JSON the API verifies (same shape as `PublicKeyCredential.toJSON()`). */
function serializeAttestation(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAttestationResponse
  const transports = typeof response.getTransports === 'function' ? response.getTransports() : []
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      attestationObject: toBase64Url(response.attestationObject),
      transports,
    },
  }
}

function serializeAssertion(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAssertionResponse
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      authenticatorData: toBase64Url(response.authenticatorData),
      signature: toBase64Url(response.signature),
      userHandle: response.userHandle ? toBase64Url(response.userHandle) : null,
    },
  }
}

/* ------------------------------------------------------------------ errors */

function translate(err: unknown, phase: 'register' | 'login'): PasskeyError {
  if (err instanceof PasskeyError) return err
  const name = (err as { name?: string } | null)?.name
  switch (name) {
    case 'NotAllowedError':
      // The browser says the same thing for "cancelled", "timed out" and
      // "this authenticator has none of the allowed credentials".
      return new PasskeyError(
        'cancelled',
        phase === 'login'
          ? `ورود لغو شد، مهلت تمام شد یا این کلید برای این حساب ثبت نشده است. ${RETRY_HINT}`
          : `ثبت لغو شد یا مهلت آن تمام شد. ${RETRY_HINT}`,
      )
    case 'InvalidStateError':
      return new PasskeyError('duplicate', 'این کلید قبلاً برای این حساب ثبت شده است')
    case 'NotSupportedError':
      return new PasskeyError('unsupported', UNSUPPORTED)
    case 'SecurityError':
      return new PasskeyError('security', 'نشانی فعلی با تنظیمات کلید امنیتی سازگار نیست (RP ID)')
    case 'AbortError':
      return new PasskeyError('cancelled', 'درخواست لغو شد')
    default:
      return new PasskeyError(
        'failed',
        err instanceof Error && err.message ? err.message : NO_ANSWER,
      )
  }
}

/* ------------------------------------------------------------------ flows */

/**
 * Enrol the authenticator that answers the browser prompt (signed in).
 * Resolves with the refreshed key list.
 */
export async function register(name: string): Promise<PasskeyListResponse> {
  if (!isSupported()) throw new PasskeyError('unsupported', UNSUPPORTED)
  const { state, options } = await auth.passkeyRegisterOptions()
  let credential: Credential | null
  try {
    credential = await navigator.credentials.create({ publicKey: creationOptions(options) })
  } catch (err) {
    throw translate(err, 'register')
  }
  if (!(credential instanceof PublicKeyCredential)) throw new PasskeyError('failed', NO_ANSWER)
  return auth.passkeyRegisterVerify(state, serializeAttestation(credential), name)
}

/**
 * Username-first login: the server lists that user's credential ids for this
 * host, the authenticator signs the challenge, the server hands out the
 * normal access token. Counts as both factors - no TOTP step afterwards.
 */
export async function login(username: string): Promise<LoginSuccess> {
  if (!isSupported()) throw new PasskeyError('unsupported', UNSUPPORTED)
  const { state, options } = await auth.passkeyLoginOptions(username)
  let credential: Credential | null
  try {
    credential = await navigator.credentials.get({ publicKey: requestOptions(options) })
  } catch (err) {
    throw translate(err, 'login')
  }
  if (!(credential instanceof PublicKeyCredential)) throw new PasskeyError('failed', NO_ANSWER)
  return auth.passkeyLoginVerify(state, serializeAssertion(credential))
}
