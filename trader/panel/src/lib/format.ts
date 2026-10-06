/** Formatting helpers. Persian labels, latin numerals for anything monetary. */

export function money(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function signed(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '—'
  const text = money(Math.abs(value), digits)
  return `${value >= 0 ? '+' : '−'}${text}`
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(digits)}%`
}

export function price(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  const abs = Math.abs(value)
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 5 : 8
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function qty(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  const abs = Math.abs(value)
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6
  return value.toFixed(digits).replace(/\.?0+$/, '')
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const seconds = Math.floor((Date.now() - then) / 1000)
  if (seconds < 5) return 'همین الان'
  if (seconds < 60) return `${seconds} ثانیه پیش`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} دقیقه پیش`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} ساعت پیش`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} روز پیش`
  return new Date(iso).toLocaleDateString(FA_LATIN_DIGITS, { timeZone: DISPLAY_TZ })
}

/** Persian (Jalali) calendar, but latin digits so every number in the app looks the same. */
const FA_LATIN_DIGITS = 'fa-IR-u-nu-latn'
// Every time the panel shows is in the user's zone, whatever device it is opened on.
export const DISPLAY_TZ = 'Asia/Tehran'

/** Short "MM/DD, HH:mm" stamp for list rows. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(FA_LATIN_DIGITS, {
    timeZone: DISPLAY_TZ,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Full date and time, for things like session expiry or a pause deadline. */
export function datetime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(FA_LATIN_DIGITS, {
    timeZone: DISPLAY_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export const SIDE_FA: Record<string, string> = {
  long: 'خرید',
  short: 'فروش',
  flat: 'بی‌طرف',
}

export const REGIME_FA: Record<string, string> = {
  trend_up: 'روند صعودی',
  trend_down: 'روند نزولی',
  range: 'رِنج',
  transition: 'گذار',
  volatile: 'پرنوسان',
  unknown: 'نامشخص',
}

export const REASON_FA: Record<string, string> = {
  stop_loss: 'حد ضرر',
  take_profit: 'حد سود',
  time_stop: 'توقف زمانی',
  signal_exit: 'خروج قانون استراتژی',
  regime_exit: 'خروج رژیم بیت‌کوین',
  end_of_window: 'پایان بازه',
  manual: 'دستی',
  'manual flatten': 'بستن دستی همه',
  exchange_stop: 'استاپ صرافی',
  exchange_fill: 'پرشدن در صرافی',
  closed_outside_engine: 'خارج از ربات بسته شد',
}

export const STANCE_FA: Record<string, string> = {
  risk_on: 'ریسک‌پذیر',
  neutral: 'خنثی',
  risk_off: 'ریسک‌گریز',
}

/** Strategy families of the 4h core (position.meta.strategy / signal contributions.family). */
export const FAMILY_FA: Record<string, string> = {
  momentum: 'مومنتوم مقطعی',
  turtle: 'شکست کانال (لاک‌پشت)',
  ensemble: 'انسمبل ۳۰ دقیقه',
}

export const CORE_FA: Record<string, string> = {
  portfolio4h: 'پرتفوی ۴ساعته',
  ensemble30m: 'انسمبل ۳۰ دقیقه',
}

export const MODE_FA: Record<string, string> = {
  paper: 'آزمایشی (پول فرضی)',
  testnet: 'تست‌نت',
  live: 'واقعی',
}

export const LEVEL_STYLE: Record<string, string> = {
  info: 'bg-slate-500/15 text-slate-300',
  warn: 'bg-warn/15 text-warn',
  error: 'bg-loss/15 text-loss',
  trade: 'bg-accent/15 text-accent',
}

// The account's quote asset (QUOTE_ASSET on the server), set once after login.
let quoteAsset = 'USDT'

export function setQuoteAsset(value: string | null | undefined): void {
  if (value) quoteAsset = value.toUpperCase()
}

/** How the quote asset is written next to amounts: Toman for IRT, else its code. */
export function quoteLabel(): string {
  return quoteAsset === 'IRT' ? 'تومان' : quoteAsset
}

export function quoteIsToman(): boolean {
  return quoteAsset === 'IRT'
}
