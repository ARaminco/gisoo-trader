import { FormEvent, useEffect, useRef, useState } from 'react'
import { ApiError, setToken } from '@/lib/api'
import { LockoutError, auth, isTotpChallenge } from '@/lib/auth'
import * as passkey from '@/lib/passkey'
import { CodeInput, Spinner } from '@/components/ui'
import { EyeIcon, EyeOffIcon, LockIcon, PasskeyIcon } from '@/components/icons'

type Step = { kind: 'password' } | { kind: 'totp'; challenge: string; expiresAt: number }

function formatCountdown(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/**
 * Seconds left until `until`, ticking once a second. Derived during render so
 * a freshly set deadline is never reported as expired for one frame.
 */
function useCountdown(until: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!until) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [until])
  if (!until) return 0
  return Math.max(0, Math.ceil((until - now) / 1000))
}

export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [step, setStep] = useState<Step>({ kind: 'password' })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lockedUntil, setLockedUntil] = useState<number | null>(null)
  const [passkeyBusy, setPasskeyBusy] = useState(false)
  const [passkeyKnown, setPasskeyKnown] = useState(false)
  const probedFor = useRef<string | null>(null)
  const passkeySupported = passkey.isSupported()

  const lockLeft = useCountdown(lockedUntil)
  const locked = lockLeft > 0
  const challengeLeft = useCountdown(step.kind === 'totp' ? step.expiresAt : null)

  useEffect(() => {
    if (!locked && lockedUntil) setLockedUntil(null)
  }, [locked, lockedUntil])

  // A challenge that ran out sends the user back to the password step.
  useEffect(() => {
    if (step.kind === 'totp' && challengeLeft === 0) {
      setStep({ kind: 'password' })
      setCode('')
      setError('مهلت وارد کردن کد تمام شد. دوباره وارد شو')
    }
  }, [step, challengeLeft])

  function fail(err: unknown) {
    if (err instanceof LockoutError) {
      setLockedUntil(Date.now() + err.retryAfter * 1000)
      setError(err.message || 'به‌خاطر تلاش‌های ناموفق، ورود موقتاً قفل شد')
      return
    }
    if (err instanceof ApiError && err.status === 401) {
      setError(err.message || 'نام کاربری یا رمز درست نیست')
      return
    }
    setError(err instanceof Error ? err.message : 'ورود انجام نشد')
  }

  async function submitPassword(event: FormEvent) {
    event.preventDefault()
    if (locked) return
    setBusy(true)
    setError(null)
    try {
      const response = await auth.login(username.trim(), password)
      if (isTotpChallenge(response)) {
        setStep({
          kind: 'totp',
          challenge: response.challenge,
          expiresAt: Date.now() + Math.max(15, response.expires_in) * 1000,
        })
        setCode('')
      } else {
        setToken(response.access_token)
        onSuccess()
      }
    } catch (err) {
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  async function submitCode(value: string) {
    if (step.kind !== 'totp' || busy || locked) return
    setBusy(true)
    setError(null)
    try {
      const response = await auth.loginTotp(step.challenge, value)
      setToken(response.access_token)
      onSuccess()
    } catch (err) {
      setCode('')
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  /** Once the username is typed: does this account have a key on this host? */
  async function probePasskey() {
    const name = username.trim()
    if (!passkeySupported || !name || name === probedFor.current) return
    probedFor.current = name
    try {
      const { available } = await auth.passkeyAvailable(name)
      setPasskeyKnown(available)
    } catch {
      setPasskeyKnown(false)
    }
  }

  /** Username-first WebAuthn login (hardware security key or phone passkey). */
  async function submitPasskey() {
    const name = username.trim()
    if (!name || busy || passkeyBusy || locked) return
    setPasskeyBusy(true)
    setError(null)
    try {
      const response = await passkey.login(name)
      setToken(response.access_token)
      onSuccess()
    } catch (err) {
      fail(err)
    } finally {
      setPasskeyBusy(false)
    }
  }

  return (
    <div className="min-h-[100dvh] flex flex-col items-center justify-center px-5 pt-[calc(2.5rem_+_var(--safe-top))] pb-[calc(2.5rem_+_var(--safe-bottom))]">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <img src="/brand/mark.svg" alt="" width={72} height={72} className="w-[4.5rem] h-[4.5rem] mb-4" />
          <h1 className="text-xl font-bold text-slate-50">Gisoo Trader</h1>
          <p className="text-xs text-slate-400 mt-1">پنل کنترل ربات معامله‌گر</p>
        </div>

        {step.kind === 'password' ? (
          <form onSubmit={submitPassword} className="card p-5 sm:p-6 space-y-4" noValidate>
            <div>
              <label className="label" htmlFor="login-username">
                نام کاربری
              </label>
              <input
                id="login-username"
                className="input"
                dir="ltr"
                autoComplete="username"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                onBlur={probePasskey}
                autoFocus
                disabled={locked}
              />
            </div>

            <div>
              <label className="label" htmlFor="login-password">
                رمز عبور
              </label>
              <div className="relative">
                <input
                  id="login-password"
                  className="input pe-12"
                  type={showPassword ? 'text' : 'password'}
                  dir="ltr"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={locked}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((value) => !value)}
                  className="absolute inset-y-0 end-0 w-12 flex items-center justify-center text-slate-400 hover:text-slate-100"
                  aria-label={showPassword ? 'پنهان کردن رمز' : 'نمایش رمز'}
                  tabIndex={-1}
                >
                  {showPassword ? <EyeOffIcon className="w-5 h-5" /> : <EyeIcon className="w-5 h-5" />}
                </button>
              </div>
            </div>

            <Feedback error={error} locked={locked} lockLeft={lockLeft} />

            <button
              className="btn-primary w-full min-h-[48px] text-base"
              disabled={busy || locked || !username || !password}
            >
              {busy ? <Spinner className="w-5 h-5" /> : 'ورود'}
            </button>

            {passkeySupported && (
              <>
                <div className="flex items-center gap-3 text-2xs text-slate-500" aria-hidden="true">
                  <span className="h-px grow bg-ink-700" />
                  یا
                  <span className="h-px grow bg-ink-700" />
                </div>
                <button
                  type="button"
                  className="btn-ghost w-full min-h-[48px] text-sm"
                  onClick={submitPasskey}
                  disabled={busy || passkeyBusy || locked || !username.trim()}
                >
                  {passkeyBusy ? (
                    <Spinner className="w-5 h-5" />
                  ) : (
                    <>
                      <PasskeyIcon className="w-5 h-5" />
                      ورود با کلید امنیتی (Passkey)
                    </>
                  )}
                </button>
                {passkeyKnown && (
                  <p className="text-2xs text-slate-400 leading-5 text-center">
                    برای این حساب کلید امنیتی ثبت شده است. کلید را وصل کن و آماده نگه دار.
                  </p>
                )}
              </>
            )}
          </form>
        ) : (
          <div className="card p-5 sm:p-6 space-y-5">
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-xl bg-accent/15 text-accent flex items-center justify-center shrink-0">
                <LockIcon className="w-5 h-5" />
              </div>
              <div>
                <h2 className="text-base font-semibold text-slate-50">کد تایید دومرحله‌ای</h2>
                <p className="text-xs text-slate-400 leading-5 mt-0.5">
                  کد شش‌رقمی اپ احراز هویت (مثل Google Authenticator) را وارد کن. با کامل شدن کد، خودکار وارد می‌شوی.
                </p>
              </div>
            </div>

            <CodeInput
              value={code}
              onChange={setCode}
              onComplete={submitCode}
              disabled={busy || locked}
              autoFocus
              error={Boolean(error) && !locked}
            />

            <Feedback error={error} locked={locked} lockLeft={lockLeft} />

            <div className="flex items-center justify-between text-xs">
              <span className="text-slate-400">
                اعتبار کد:{' '}
                <span className="num text-slate-200">{formatCountdown(challengeLeft)}</span>
              </span>
              <button
                type="button"
                className="text-accent min-h-[36px] px-2 -me-2"
                onClick={() => {
                  setStep({ kind: 'password' })
                  setCode('')
                  setError(null)
                }}
                disabled={busy}
              >
                برگشت به رمز عبور
              </button>
            </div>

            <button
              className="btn-primary w-full min-h-[48px] text-base"
              disabled={busy || locked || code.length < 6}
              onClick={() => submitCode(code)}
            >
              {busy ? <Spinner className="w-5 h-5" /> : 'تایید'}
            </button>
          </div>
        )}

        <p className="text-2xs text-slate-400 text-center mt-6 leading-5">
          نام کاربری و رمز در فایل <span className="num">.env</span> تعریف می‌شوند
          <span className="num"> (PANEL_USERNAME / PANEL_PASSWORD)</span>
        </p>
      </div>
    </div>
  )
}

function Feedback({ error, locked, lockLeft }: { error: string | null; locked: boolean; lockLeft: number }) {
  if (locked) {
    return (
      <div className="rounded-xl bg-warn/10 border border-warn/30 px-4 py-3 text-sm text-warn leading-6" role="alert">
        ورود موقتاً قفل شده است. تلاش دوباره تا{' '}
        <span className="num font-semibold">{formatCountdown(lockLeft)}</span> دیگر
      </div>
    )
  }
  if (!error) return null
  return (
    <div className="rounded-xl bg-loss/10 border border-loss/30 px-4 py-3 text-sm text-loss leading-6" role="alert">
      {error}
    </div>
  )
}
