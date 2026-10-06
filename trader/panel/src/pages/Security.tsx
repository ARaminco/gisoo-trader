import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@/lib/api'
import { Me, Passkey, PasskeyListResponse, TotpSetup, auth } from '@/lib/auth'
import * as passkey from '@/lib/passkey'
import { datetime, timeAgo } from '@/lib/format'
import { useSession } from '@/lib/session'
import {
  Badge,
  Card,
  CodeInput,
  ConfirmDialog,
  CopyButton,
  Notice,
  Sheet,
  Skeleton,
  Spinner,
  Toggle,
  copyToClipboard,
  toast,
} from '@/components/ui'
import { KeyIcon, LogoutIcon, PasskeyIcon, ShieldIcon, TrashIcon } from '@/components/icons'

type Flow =
  | { kind: 'idle' }
  | { kind: 'setup'; setup: TotpSetup; code: string; busy: boolean; error: string | null }
  | { kind: 'backup'; codes: string[] }
  | { kind: 'disable'; code: string; busy: boolean; error: string | null }

type AddKey = { name: string; busy: boolean; error: string | null }

/** How the authenticator reported it talks to the browser. */
const TRANSPORT_FA: Record<string, string> = {
  usb: 'USB',
  nfc: 'NFC',
  ble: 'بلوتوث',
  hybrid: 'گوشی',
  cable: 'گوشی',
  internal: 'داخلی',
  'smart-card': 'کارت هوشمند',
}

export default function Security() {
  const { signOut } = useSession()
  const [me, setMe] = useState<Me | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [unsupported, setUnsupported] = useState(false)
  const [flow, setFlow] = useState<Flow>({ kind: 'idle' })
  const [starting, setStarting] = useState(false)
  const [confirm, setConfirm] = useState<null | 'all' | 'one'>(null)
  const [keys, setKeys] = useState<PasskeyListResponse | null>(null)
  const [keysError, setKeysError] = useState<string | null>(null)
  const [adding, setAdding] = useState<AddKey | null>(null)
  const [removing, setRemoving] = useState<Passkey | null>(null)
  const [switching, setSwitching] = useState(false)
  const passkeySupported = passkey.isSupported()

  const load = useCallback(async () => {
    try {
      setMe(await auth.me())
      setLoadError(null)
      setUnsupported(false)
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) setUnsupported(true)
      else setLoadError(err instanceof Error ? err.message : 'خطا')
    }
  }, [])

  const loadKeys = useCallback(async () => {
    try {
      setKeys(await auth.passkeys())
      setKeysError(null)
    } catch (err) {
      // an API from before passkeys: the card just stays empty
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) setKeys(null)
      else setKeysError(err instanceof Error ? err.message : 'خطا')
    }
  }, [])

  useEffect(() => {
    load()
    loadKeys()
  }, [load, loadKeys])

  async function startSetup() {
    setStarting(true)
    try {
      const setup = await auth.totpSetup()
      setFlow({ kind: 'setup', setup, code: '', busy: false, error: null })
    } catch (err) {
      toast(err instanceof Error ? err.message : 'خطا', 'error')
    } finally {
      setStarting(false)
    }
  }

  async function confirmEnable(code: string) {
    if (flow.kind !== 'setup' || flow.busy) return
    setFlow({ ...flow, busy: true, error: null })
    try {
      const result = await auth.totpEnable(code)
      setFlow({ kind: 'backup', codes: result.backup_codes ?? [] })
      toast('تایید دومرحله‌ای فعال شد')
      load()
    } catch (err) {
      setFlow({ ...flow, code: '', busy: false, error: err instanceof Error ? err.message : 'کد پذیرفته نشد' })
    }
  }

  async function confirmDisable(code: string) {
    if (flow.kind !== 'disable' || flow.busy) return
    setFlow({ ...flow, busy: true, error: null })
    try {
      await auth.totpDisable(code)
      setFlow({ kind: 'idle' })
      toast('تایید دومرحله‌ای غیرفعال شد')
      load()
    } catch (err) {
      setFlow({ ...flow, code: '', busy: false, error: err instanceof Error ? err.message : 'کد پذیرفته نشد' })
    }
  }

  async function confirmAdd() {
    if (!adding || adding.busy) return
    setAdding({ ...adding, busy: true, error: null })
    try {
      setKeys(await passkey.register(adding.name.trim() || 'کلید امنیتی'))
      setAdding(null)
      toast('کلید امنیتی ثبت شد')
      load()
    } catch (err) {
      setAdding({ ...adding, busy: false, error: err instanceof Error ? err.message : 'ثبت انجام نشد' })
    }
  }

  async function confirmRemove() {
    if (!removing) return
    const key = removing
    setRemoving(null)
    try {
      setKeys(await auth.deletePasskey(key.id))
      toast(`کلید «${key.name}» حذف شد`)
      load()
    } catch (err) {
      toast(err instanceof Error ? err.message : 'حذف انجام نشد', 'error')
    }
  }

  async function toggleRequired(enabled: boolean) {
    setSwitching(true)
    try {
      setKeys(await auth.setPasskeyRequired(enabled))
      toast(enabled ? 'از این پس فقط با کلید امنیتی می‌توان وارد شد' : 'ورود با رمز عبور دوباره فعال شد')
      load()
    } catch (err) {
      toast(err instanceof Error ? err.message : 'تغییر انجام نشد', 'error')
    } finally {
      setSwitching(false)
    }
  }

  const enabled = me?.totp_enabled ?? false
  const keyCount = keys?.passkeys.length ?? 0

  return (
    <div className="space-y-4 max-w-2xl mx-auto">
      {unsupported && (
        <Notice tone="warn">
          سرور هنوز به نسخه‌ای با احراز هویت دومرحله‌ای به‌روز نشده است. بعد از به‌روزرسانی، این صفحه فعال می‌شود.
        </Notice>
      )}
      {loadError && <Notice tone="danger">{loadError}</Notice>}

      {/* ---- 2FA ---- */}
      <Card>
        <div className="flex items-start gap-3">
          <div
            className={`w-11 h-11 rounded-xl flex items-center justify-center shrink-0 ${
              enabled ? 'bg-profit/15 text-profit' : 'bg-ink-850 border border-ink-700/70 text-slate-300'
            }`}
          >
            <ShieldIcon className="w-6 h-6" />
          </div>
          <div className="grow min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-base font-semibold text-slate-50">تایید دومرحله‌ای</h2>
              {me ? (
                <Badge tone={enabled ? 'profit' : 'warn'}>{enabled ? 'فعال' : 'غیرفعال'}</Badge>
              ) : (
                !unsupported && <Skeleton className="h-5 w-14" />
              )}
            </div>
            <p className="text-xs text-slate-400 leading-6 mt-1">
              {enabled
                ? 'برای ورود، علاوه بر رمز، کد شش‌رقمی اپ احراز هویت لازم است. اگر رمزت لو برود، باز هم کسی وارد نمی‌شود.'
                : 'با فعال کردن، ورود به پنل به کد شش‌رقمی از اپ احراز هویت (Google Authenticator، Aegis، 1Password) هم نیاز پیدا می‌کند. برای پنلی که پول واقعی را کنترل می‌کند، توصیه‌ی جدی است.'}
            </p>
          </div>
        </div>

        <div className="mt-5">
          {enabled ? (
            <button
              className="btn-ghost w-full sm:w-auto"
              onClick={() => setFlow({ kind: 'disable', code: '', busy: false, error: null })}
              disabled={unsupported}
            >
              غیرفعال کردن
            </button>
          ) : (
            <button className="btn-primary w-full sm:w-auto" onClick={startSetup} disabled={starting || unsupported || !me}>
              {starting ? <Spinner className="w-4 h-4 border-ink-950/40 border-t-ink-950" /> : 'فعال‌سازی'}
            </button>
          )}
        </div>
      </Card>

      {/* ---- passkeys ---- */}
      <Card>
        <div className="flex items-start gap-3">
          <div
            className={`w-11 h-11 rounded-xl flex items-center justify-center shrink-0 ${
              keyCount > 0 ? 'bg-profit/15 text-profit' : 'bg-ink-850 border border-ink-700/70 text-slate-300'
            }`}
          >
            <PasskeyIcon className="w-6 h-6" />
          </div>
          <div className="grow min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-base font-semibold text-slate-50">کلیدهای امنیتی (Passkey)</h2>
              {keys ? (
                <Badge tone={keyCount > 0 ? 'profit' : 'neutral'}>
                  {keyCount > 0 ? `${keyCount} کلید` : 'ثبت نشده'}
                </Badge>
              ) : (
                !keysError && !unsupported && <Skeleton className="h-5 w-14" />
              )}
            </div>
            <p className="text-xs text-slate-400 leading-6 mt-1">
              ورود بدون رمز با یک کلید امنیتی سخت‌افزاری (FIDO2) یا کلید داخلی گوشی. ورود با کلید، جای رمز و کد
              دومرحله‌ای را با هم می‌گیرد. هر کلید فقط برای نشانی‌ای که روی آن ثبت شده کار می‌کند؛ برای{' '}
              <span className="num">localhost</span> و نشانی عمومی جداگانه ثبت کن.
            </p>
          </div>
        </div>

        {!passkeySupported && (
          <Notice tone="warn" className="mt-4">
            این مرورگر از کلید امنیتی پشتیبانی نمی‌کند، یا صفحه روی HTTPS / localhost باز نشده است.
          </Notice>
        )}
        {keysError && (
          <Notice tone="danger" className="mt-4">
            {keysError}
          </Notice>
        )}

        {keys && keys.passkeys.length > 0 && (
          <ul className="mt-4 card-raised divide-y divide-ink-700/60 overflow-hidden">
            {keys.passkeys.map((key) => (
              <li key={key.id} className="flex items-center gap-3 px-4 py-3">
                <div className="grow min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-slate-100 truncate">{key.name}</span>
                    <Badge tone="accent" className="num">
                      {key.rp_id}
                    </Badge>
                    {key.transports.map((transport) => (
                      <Badge key={transport}>{TRANSPORT_FA[transport] ?? transport}</Badge>
                    ))}
                  </div>
                  <div className="text-2xs text-slate-400 leading-5 mt-1">
                    ثبت: <span className="num">{datetime(key.created_at)}</span>
                    {' · '}
                    آخرین ورود: {key.last_used_at ? timeAgo(key.last_used_at) : 'هنوز استفاده نشده'}
                  </div>
                </div>
                <button
                  type="button"
                  className="w-10 h-10 -me-2 rounded-lg flex items-center justify-center shrink-0 text-slate-400 hover:text-loss hover:bg-loss/10"
                  aria-label={`حذف کلید ${key.name}`}
                  onClick={() => setRemoving(key)}
                >
                  <TrashIcon className="w-5 h-5" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="mt-5">
          <button
            className="btn-primary w-full sm:w-auto"
            onClick={() => setAdding({ name: 'کلید امنیتی', busy: false, error: null })}
            disabled={!passkeySupported || !keys}
          >
            <PasskeyIcon className="w-4 h-4" />
            افزودن کلید
          </button>
        </div>

        <div className="mt-3 border-t border-ink-800">
          <Toggle
            checked={keys?.passkey_required ?? false}
            onChange={toggleRequired}
            disabled={switching || !keys || (!keys.passkey_required && keyCount === 0)}
            label="فقط ورود با کلید امنیتی"
            hint="ورود با رمز عبور روی همهٔ دستگاه‌ها بسته می‌شود و برای خاموش کردنش باید با کلید وارد شوی. راه بازیابی بدون کلید در README آمده است."
          />
        </div>

        <details className="mt-1">
          <summary className="text-xs text-accent cursor-pointer min-h-[36px] flex items-center">
            راهنمای ثبت کلید امنیتی
          </summary>
          <ol className="text-xs text-slate-300 leading-6 space-y-1 list-decimal ps-5 mt-1">
            <li>
              کلید امنیتی (FIDO2) را آماده کن؛ اگر اپ یا قفل جداگانه دارد، آن را باز نگه دار.
            </li>
            <li>روی گوشی: بلوتوث یا NFC را روشن کن و کلید را نزدیک نگه دار؛ روی کامپیوتر: با کابل USB وصل کن.</li>
            <li>
              «افزودن کلید» را بزن، نامی بده و «ثبت کلید» را بزن. مرورگر می‌پرسد کلید کجاست: گزینهٔ «کلید امنیتی» /
              «USB» / «دستگاه دیگر» را انتخاب کن، نه iCloud یا Google Password Manager.
            </li>
            <li>روی کلید تأیید کن (لمس یا دکمه). کلید فقط برای همین نشانی ثبت می‌شود.</li>
            <li>برای ورود: نام کاربری را بنویس و «ورود با کلید امنیتی» را بزن؛ کلید دوباره تأیید می‌خواهد.</li>
          </ol>
        </details>
      </Card>

      {/* ---- sessions ---- */}
      <Card title="نشست‌ها و دستگاه‌ها">
        <dl className="text-sm mb-4">
          <div className="flex items-center justify-between gap-4 py-2.5 border-b border-ink-800">
            <dt className="text-slate-400">کاربر</dt>
            <dd className="num text-slate-100">{me?.username ?? '—'}</dd>
          </div>
          <div className="flex items-center justify-between gap-4 py-2.5">
            <dt className="text-slate-400">پایان اعتبار این نشست</dt>
            <dd className="num text-slate-100">{datetime(me?.token_expires_at)}</dd>
          </div>
        </dl>
        <div className="grid sm:grid-cols-2 gap-2">
          <button className="btn-ghost" onClick={() => setConfirm('one')}>
            <LogoutIcon className="w-4 h-4" />
            خروج از این دستگاه
          </button>
          <button className="btn-danger-soft" onClick={() => setConfirm('all')} disabled={unsupported}>
            <KeyIcon className="w-4 h-4" />
            خروج از همه دستگاه‌ها
          </button>
        </div>
        <p className="text-2xs text-slate-400 leading-5 mt-3">
          «خروج از همه دستگاه‌ها» هر نشست بازی را باطل می‌کند؛ اگر فکر می‌کنی رمزت جایی مانده، این را بزن و بعد رمز را در{' '}
          <span className="num">.env</span> عوض کن.
        </p>
      </Card>

      {/* ---- setup sheet ---- */}
      <Sheet
        open={flow.kind === 'setup'}
        onClose={() => setFlow({ kind: 'idle' })}
        title="فعال‌سازی تایید دومرحله‌ای"
      >
        {flow.kind === 'setup' && (
          <div className="space-y-5">
            <ol className="text-xs text-slate-300 leading-6 space-y-1 list-decimal ps-5">
              <li>اپ احراز هویت را باز کن و این کد QR را اسکن کن.</li>
              <li>کد شش‌رقمی که اپ نشان می‌دهد را پایین وارد کن.</li>
            </ol>

            <div className="flex flex-col items-center gap-3">
              <div className="p-3 bg-white rounded-2xl">
                <img
                  src={`data:image/png;base64,${flow.setup.qr_png_base64}`}
                  alt="QR فعال‌سازی"
                  className="w-44 h-44 sm:w-48 sm:h-48 [image-rendering:pixelated]"
                />
              </div>
              <details className="w-full">
                <summary className="text-xs text-accent cursor-pointer text-center min-h-[36px] flex items-center justify-center">
                  نمی‌توانی اسکن کنی؟ کلید را دستی وارد کن
                </summary>
                <div className="mt-2 card-raised p-3 flex items-center gap-2">
                  <code className="num text-sm text-slate-50 grow leading-6 tracking-wide" dir="ltr">
                    {chunk(flow.setup.secret)}
                  </code>
                  <CopyButton text={flow.setup.secret} />
                </div>
              </details>
            </div>

            <div>
              <div className="label">کد تایید</div>
              <CodeInput
                value={flow.code}
                onChange={(code) => setFlow({ ...flow, code, error: null })}
                onComplete={confirmEnable}
                disabled={flow.busy}
                error={Boolean(flow.error)}
                autoFocus
              />
              {flow.error && <p className="text-xs text-loss mt-2 leading-5">{flow.error}</p>}
            </div>

            <button
              className="btn-primary w-full min-h-[48px]"
              disabled={flow.busy || flow.code.length < 6}
              onClick={() => confirmEnable(flow.code)}
            >
              {flow.busy ? <Spinner className="w-5 h-5 border-ink-950/40 border-t-ink-950" /> : 'فعال کن'}
            </button>
          </div>
        )}
      </Sheet>

      {/* ---- backup codes (shown once) ---- */}
      <Sheet
        open={flow.kind === 'backup'}
        onClose={() => setFlow({ kind: 'idle' })}
        title="کدهای پشتیبان"
        footer={
          <button className="btn-primary w-full" onClick={() => setFlow({ kind: 'idle' })}>
            ذخیره کردم
          </button>
        }
      >
        {flow.kind === 'backup' && (
          <div className="space-y-4">
            <Notice tone="warn">
              این کدها فقط همین یک بار نمایش داده می‌شوند. اگر گوشی را گم کنی، هر کدام یک بار به‌جای کد اپ کار می‌کند. جایی امن نگهشان دار.
            </Notice>
            {flow.codes.length === 0 ? (
              <p className="text-sm text-slate-400">سرور کد پشتیبانی برنگرداند.</p>
            ) : (
              <div className="grid grid-cols-2 gap-2" dir="ltr">
                {flow.codes.map((code) => (
                  <code
                    key={code}
                    className="num text-sm text-slate-50 bg-ink-850 border border-ink-700 rounded-lg px-3 py-2.5 text-center tracking-wider"
                  >
                    {code}
                  </code>
                ))}
              </div>
            )}
            <button
              className="btn-ghost w-full"
              onClick={async () => {
                const ok = await copyToClipboard(flow.codes.join('\n'))
                toast(ok ? 'همه کدها کپی شد' : 'کپی نشد', ok ? 'ok' : 'error')
              }}
              disabled={flow.codes.length === 0}
            >
              کپی همه کدها
            </button>
          </div>
        )}
      </Sheet>

      {/* ---- disable sheet ---- */}
      <Sheet
        open={flow.kind === 'disable'}
        onClose={() => setFlow({ kind: 'idle' })}
        title="غیرفعال کردن تایید دومرحله‌ای"
      >
        {flow.kind === 'disable' && (
          <div className="space-y-5">
            <p className="text-sm text-slate-300 leading-7">
              برای تایید، کد فعلی اپ احراز هویت را وارد کن. بعد از این، ورود فقط با رمز انجام می‌شود.
            </p>
            <CodeInput
              value={flow.code}
              onChange={(code) => setFlow({ ...flow, code, error: null })}
              onComplete={confirmDisable}
              disabled={flow.busy}
              error={Boolean(flow.error)}
              autoFocus
            />
            {flow.error && <p className="text-xs text-loss leading-5">{flow.error}</p>}
            <button
              className="btn-danger w-full min-h-[48px]"
              disabled={flow.busy || flow.code.length < 6}
              onClick={() => confirmDisable(flow.code)}
            >
              {flow.busy ? <Spinner className="w-5 h-5" /> : 'غیرفعال کن'}
            </button>
          </div>
        )}
      </Sheet>

      {/* ---- add passkey ---- */}
      <Sheet
        open={adding !== null}
        onClose={() => {
          if (!adding?.busy) setAdding(null)
        }}
        title="افزودن کلید امنیتی"
      >
        {adding && (
          <div className="space-y-5">
            <p className="text-sm text-slate-300 leading-7">
              کلید امنیتی را وصل کن (USB روی کامپیوتر، بلوتوث یا NFC روی گوشی) و «ثبت کلید» را بزن. بعد از انتخاب
              کلید در مرورگر، روی خود کلید تأیید کن.
            </p>
            <div>
              <label className="label" htmlFor="passkey-name">
                نام کلید
              </label>
              <input
                id="passkey-name"
                className="input"
                value={adding.name}
                maxLength={64}
                onChange={(event) => setAdding({ ...adding, name: event.target.value, error: null })}
                disabled={adding.busy}
                autoFocus
              />
            </div>
            {adding.error && <p className="text-xs text-loss leading-5">{adding.error}</p>}
            <button className="btn-primary w-full min-h-[48px]" disabled={adding.busy} onClick={confirmAdd}>
              {adding.busy ? <Spinner className="w-5 h-5 border-ink-950/40 border-t-ink-950" /> : 'ثبت کلید'}
            </button>
          </div>
        )}
      </Sheet>

      <ConfirmDialog
        open={removing !== null}
        title="حذف کلید امنیتی"
        danger
        confirmText="حذف"
        body={
          <>
            کلید «{removing?.name}» حذف می‌شود و دیگر با آن نمی‌توان وارد شد. روی خود کلید چیزی تغییر نمی‌کند.
          </>
        }
        onConfirm={confirmRemove}
        onCancel={() => setRemoving(null)}
      />

      <ConfirmDialog
        open={confirm === 'all'}
        title="خروج از همه دستگاه‌ها"
        danger
        confirmText="همه را خارج کن"
        body="هر نشست باز روی هر دستگاهی، از جمله همین یکی، باطل می‌شود و باید دوباره وارد شوی. ربات به کارش ادامه می‌دهد."
        onConfirm={async () => {
          setConfirm(null)
          await signOut(true)
        }}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm === 'one'}
        title="خروج از این دستگاه"
        confirmText="خروج"
        danger
        body="از این دستگاه خارج می‌شوی. ربات به کار خودش ادامه می‌دهد."
        onConfirm={async () => {
          setConfirm(null)
          await signOut()
        }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  )
}

/** Group a base32 secret in fours so it can be typed from a screen. */
function chunk(secret: string): string {
  return secret.replace(/\s+/g, '').replace(/(.{4})/g, '$1 ').trim()
}
