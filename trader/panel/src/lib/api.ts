/** Typed client for the engine API. One place that knows about auth and errors. */

const TOKEN_KEY = 'gtr.token'

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* private mode - the session just will not persist */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string>) ?? {}),
  }
  if (token) headers.Authorization = `Bearer ${token}`

  const response = await fetch(path, { ...init, headers })

  if (response.status === 401) {
    setToken(null)
    window.dispatchEvent(new CustomEvent('gtr:unauthorized'))
    throw new ApiError(401, 'نشست منقضی شده است')
  }

  if (!response.ok) {
    let detail = `خطای ${response.status}`
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

// ---------------------------------------------------------------- types

export type StrategyCore = 'portfolio4h' | 'ensemble30m'

export interface CoreFamily {
  name: string
  research_name: string
  risk_pct: number
  max_position_pct: number
  atr_stop: number
  cooldown_min: number
}

export interface ExchangeInfo {
  name: string
  title: string
  title_fa: string
  /** "reference" (Binance) or "beta" (written against the public docs) */
  status: string
  website: string
  api_docs: string
  quotes: string[]
  stop_orders: boolean
  kline_stream: boolean
  user_stream: boolean
}

/** NetArz FX: Toman per one unit of `code`. */
export interface TomanRate {
  available: boolean
  code: string
  buy?: number | null
  sell?: number | null
  mid?: number | null
  as_of?: string | null
  plan?: string | null
  delayed_minutes?: number | null
  error?: string
  ip?: string | null
}

export interface StaticSettings {
  mode: string
  exchange: ExchangeInfo
  quote_asset: string
  fx_configured: boolean
  strategy_core: StrategyCore
  timeframe: string
  htf_timeframe: string | null
  universe: string[]
  core_families: CoreFamily[]
  taker_fee_pct: number
  round_trip_fee_pct: number
  llm_model: string
  candle_history: number
}

export interface Overview {
  mode: string
  strategy_core: StrategyCore
  armed: boolean
  kill_switch: boolean
  paused_until: string | null
  equity: number
  free_balance: number
  capital_base: number
  unrealized_pnl: number
  total_pnl: number
  total_pnl_pct: number
  today_pnl: number
  today_pnl_pct: number
  peak_equity: number
  drawdown_pct: number
  closed_equity: number
  closed_peak_equity: number
  closed_drawdown_pct: number
  open_positions: number
  max_positions: number
  trades_total: number
  trades_today: number
  win_rate: number
  profit_factor: number
  avg_r: number
  market_stance: string
  risk_multiplier: number
  exchange_connected: boolean
  stream_connected: boolean
  last_sync: string | null
  workers: Record<string, any>
}

export interface Position {
  id: number
  symbol: string
  side: string
  market: string
  status: string
  qty: number
  entry_price: number
  exit_price: number | null
  mark_price: number | null
  stop_price: number
  target_price: number
  notional: number
  unrealized_pnl: number
  unrealized_pct: number
  realized_pnl: number
  realized_pnl_pct: number
  r_multiple: number
  fees_paid: number
  trail_active: boolean
  pending_exit_reason?: string | null
  stop_missing?: boolean
  /** Bar-close high-water mark (long: highest high, short: lowest low). */
  high_water?: number
  /** Peak open gain in percent of entry. */
  peak_pct?: number
  /** How much of the peak has been given back, in percent points (0 if the peak never went positive). */
  giveback_pct?: number
  exit_reason: string | null
  opened_at: string
  closed_at: string | null
  meta: Record<string, any>
}

export interface Signal {
  id: number
  created_at: string
  symbol: string
  side: string
  score: number
  ml_probability: number | null
  regime: string
  price: number
  expected_move_pct: number
  executed: boolean
  rejection_reason: string | null
  llm_verdict: string | null
  llm_note: string | null
  contributions: Record<string, { score: number; weight: number; reason: string }>
}

export interface EquityPoint {
  t: string
  equity: number
  drawdown_pct: number
  unrealized_pnl: number
}

export interface EventRow {
  id: number
  created_at: string
  level: string
  source: string
  message: string
  data: Record<string, any>
}

export interface CapitalFlow {
  id: number
  occurred_at: string
  kind: string
  amount: number
  asset: string
  source: string
  note: string | null
}

export interface SyncState {
  connected: boolean
  stream_connected: boolean
  last_rest_sync: string | null
  last_stream_event: string | null
  server_time_offset_ms: number
  balances: Record<string, number>
  open_orders: number
  futures_positions: Record<string, any>
  drift: Record<string, any>
  last_error: string | null
  capital_base: number
  mode: string
  /** The address the exchange sees (the API key's allow-list must contain it). */
  public_ip: string | null
  public_ip_seen_at: string | null
}

// ---------------------------------------------------------------- notifications

export type NotifyCategory = 'trade' | 'risk' | 'control' | 'exchange' | 'system' | 'ai'

export interface QuietHours {
  enabled: boolean
  /** "HH:MM" in the engine's TZ. */
  start: string
  end: string
}

export interface NotifyPrefs {
  categories: Record<NotifyCategory, boolean>
  quiet_hours: QuietHours
}

export interface NotifyStatus {
  telegram: {
    configured: boolean
    chat_id: string | null
    chat_title: string | null
    source: 'env' | 'discovered' | null
  }
  push: { devices: number; vapid_ready: boolean }
  preferences: NotifyPrefs
  contact: string
  tz: string
  public_ip: string | null
  public_ip_seen_at: string | null
  public_ip_changed_at: string | null
  public_ip_previous: string | null
}

export interface PushSubscribeIn {
  endpoint: string
  keys: { p256dh: string; auth: string }
  user_agent?: string
}

/** Per channel: "sent", "skipped: ...", "timeout" or "error: ...". */
export type NotifyTestResult = Record<string, string>

export interface BacktestResult {
  symbol: string
  timeframe: string
  candles: number
  trades: number
  wins: number
  losses: number
  win_rate: number
  net_pnl: number
  net_pnl_pct: number
  profit_factor: number
  max_drawdown_pct: number
  avg_r: number
  expectancy: number
  fees_paid: number
  equity_curve: number[]
  trade_log: Array<Record<string, any>>
}

// ---------------------------------------------------------------- watch (the 4h core's live view)

/** Candles stored for one symbol of the 4h universe. */
export interface WatchBars {
  bars: number
  latest_open_time: string | null
  ready: boolean
}

export interface WatchEngine {
  online: boolean
  heartbeat_age_s: number | null
  core: string | null
  core4h: Record<string, any>
}

export interface WatchRegime {
  symbol: string
  close: number | null
  ema200: number | null
  distance_pct: number | null
  regime_on: boolean
  ema_rising: boolean
  regime_bars: number
  regime_since: string | null
  sparkline: Array<number | null>
  ema_sparkline: Array<number | null>
  sparkline_open_times: string[]
}

export interface WatchMomentumRow {
  symbol: string
  /** No bar stored for the evaluated open time yet. */
  stale: boolean
  tradable: boolean
  /** Family holding the symbol right now, if any. */
  held: string | null
  rank: number | null
  universe?: number
  ret_7d_pct?: number | null
  ret_1d_pct?: number | null
  momentum_pct?: number | null
  in_top3?: boolean
  in_top5?: boolean
  signal_score?: number
  close?: number | null
  atr?: number | null
  atr_pct?: number | null
  stop_price?: number | null
  signal_now?: boolean
  exit_now?: boolean
  exit_reason?: string
}

export interface WatchBreakoutRow {
  symbol: string
  stale: boolean
  tradable: boolean
  held: string | null
  /** Live mark (not part of the evaluation; shown next to the channel). */
  mark: number | null
  close?: number | null
  atr?: number | null
  atr_pct?: number | null
  entry_high?: number | null
  exit_low?: number | null
  /** Positive: the close sits this far below the 55-bar high. */
  distance_pct?: number | null
  distance_atr?: number | null
  exit_low_distance_pct?: number | null
  above_ema200?: boolean
  market_on?: boolean
  adx?: number | null
  /** Every entry condition but the close above the channel high. */
  armed?: boolean
  triggered?: boolean
  signal_now?: boolean
  score?: number | null
  stop_price?: number | null
  exit_now?: boolean
  exit_reason?: string
}

export interface WatchCandidate {
  family: string
  symbol: string
  score: number | null
  price: number | null
  stop_price: number | null
}

export interface WatchExitRule {
  rule: 'rank' | 'channel' | 'stop'
  regime_on: boolean
  exit_now: boolean
  exit_reason: string
  rank?: number | null
  universe?: number | null
  hold_n?: number
  at_risk?: boolean
  exit_low?: number | null
  distance_pct?: number | null
}

export interface WatchPosition {
  id: number
  symbol: string
  family: string
  side: string
  qty: number
  entry_price: number
  stop_price: number
  mark: number | null
  notional: number
  unrealized_pnl: number
  unrealized_pct: number
  stop_distance_pct: number | null
  opened_at: string
  exit: WatchExitRule
}

export interface WatchGate {
  code: string
  detail: string
}

export interface WatchSizing {
  family: string
  symbol: string
  notional: number
  min_notional: number
  risk_amount: number
  ok: boolean
}

export interface WatchGates {
  ready: boolean
  blocking: WatchGate[]
  armed: boolean
  mode: string
  kill_switch: boolean
  paused_until: string | null
  session_block: string | null
  skip_weekend_entries: boolean
  skip_entry_hours_utc: number[]
  open_positions: number
  max_positions: number
  equity: number
  free_balance: number
  min_equity: number
  min_notional: number
  min_notional_source: 'exchange' | 'default'
  funds_ok: boolean
  drawdown_pct: number
  max_drawdown_pct: number
  today_pnl_pct: number
  max_daily_loss_pct: number
  /** Turtle cooldown after a loss, minutes left per symbol. */
  cooldowns: Record<string, number>
  sizing: WatchSizing[]
}

export interface WatchSignal extends Signal {
  family: string | null
  timeframe: string
  atr: number
  suggested_stop: number
}

export interface Watch {
  core: StrategyCore
  is_portfolio4h: boolean
  mode: string
  now: string
  next_evaluation_at: string
  seconds_to_next: number
  /** The bar closing next opened at 00:00 UTC: the daily momentum rebalance. */
  is_rebalance_bar: boolean
  next_rebalance_at: string
  last_evaluated_bar: string | null
  last_evaluated_close: string | null
  min_bars: number
  bars_stored: Record<string, WatchBars>
  engine: WatchEngine
  families: Record<string, { risk_pct: number; max_position_pct: number; atr_stop: number; cooldown_min: number }>
  recent: WatchSignal[]
  evaluated_bar: string | null
  evaluated_bar_close?: string | null
  data_lag_bars: number | null
  computed_at?: string | null
  compute_ms?: number | null
  history_ready?: boolean
  ranked?: string[]
  market_regime: WatchRegime | null
  momentum: WatchMomentumRow[]
  breakouts: WatchBreakoutRow[]
  candidates: WatchCandidate[]
  positions: WatchPosition[]
  gates: WatchGates | null
}

// ---------------------------------------------------------------- investment committee

export type CommitteeSeverity = 'low' | 'medium' | 'high'
export type CommitteeAction = 'core4h_risk_scale' | 'add_capital' | 'sleeve_target' | 'pause_bot' | 'other'

export interface CommitteeRisk {
  title_fa: string
  severity: CommitteeSeverity
  detail_fa: string
}

export interface CommitteeDecision {
  kind: 'stance' | 'risk_multiplier' | 'note'
  value: string
  rationale_fa: string
}

export interface CommitteeProposalStatus {
  status: 'approved' | 'declined'
  at: string
  by: string
  action: string
  result: Record<string, any>
}

export interface CommitteeProposal {
  index: number
  title_fa: string
  action: CommitteeAction
  params: Record<string, any>
  rationale_fa: string
  expected_effect_fa: string
  /** null until the user approves or declines. */
  status: CommitteeProposalStatus | null
}

export interface CommitteeCheck {
  code: string
  label_fa: string
  /** null = could not be determined (e.g. the exchange did not answer). */
  ok: boolean | null
  severity: CommitteeSeverity
  detail: string
  data: Record<string, any>
}

export interface CommitteeHealth {
  ok: boolean
  failed: string[]
  unknown?: string[]
  checks: CommitteeCheck[]
}

export interface CommitteeApplied {
  kind: string
  value: any
  applied: boolean
  changed?: boolean
  rationale_fa?: string
  why?: string
}

export interface CommitteeWindow {
  trades: number
  wins: number
  losses: number
  win_rate: number | null
  net_pnl: number
  avg_r: number | null
  avg_loss_r: number | null
  worst_r: number | null
  median_winner_pct: number | null
  full_stops: number
  fees: number
}

export interface CommitteeMeeting {
  id: number
  held_at: string
  reason: 'scheduled' | 'manual' | string
  /** "template" when no model answered and the numeric briefing went out alone. */
  provider: string
  model: string
  tokens: number
  health_ok: boolean
  delivery: Record<string, string>
  summary_fa: string
  market_view_fa: string
  positions_view_fa: string
  risks: CommitteeRisk[]
  decisions: CommitteeDecision[]
  confidence: number | null
  proposals: CommitteeProposal[]
  applied_decisions: CommitteeApplied[]
  health: CommitteeHealth
  account: Record<string, any>
  positions: Array<Record<string, any>>
  market: Record<string, any> | null
  gates: Record<string, any> | null
  performance: {
    since: string | null
    inception: CommitteeWindow
    last_7d: CommitteeWindow
    last_30d: CommitteeWindow
    daily_pauses_30d: number
    expectations: Record<string, any>
  } | null
  stance_history: Array<{ at: string; stance: string; risk_multiplier: number; confidence: number; source: string; reasoning: string }>
  settings: Record<string, any> | null
}

export interface CommitteeIndex {
  enabled: boolean
  hours_utc: number[]
  meeting_minute: number
  tz: string
  last_held_at: string | null
  next_meeting_at: string | null
  scheduler_online: boolean
  meetings: CommitteeMeeting[]
}

// ---------------------------------------------------------------- auth

export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

/** Password accepted, a TOTP (or backup) code is still required. */
export interface TotpChallenge {
  requires_totp: true
  challenge: string
  expires_in: number
}

export type LoginResult = TokenResponse | TotpChallenge

export function isTotpChallenge(result: LoginResult): result is TotpChallenge {
  return (result as TotpChallenge).requires_totp === true
}

export interface Me {
  username: string
  totp_enabled: boolean
  token_expires_at: string
}

export interface TotpSetup {
  secret: string
  otpauth_uri: string
  /** PNG bytes, base64 - use as `data:image/png;base64,${qr_png_base64}` */
  qr_png_base64: string
}

export interface TotpEnabled {
  enabled: true
  /** Shown exactly once. Each code works a single time. */
  backup_codes: string[]
}

export interface TotpDisabled {
  enabled: false
}

/** HTTP 423: too many failed attempts. `retryAfterSeconds` says how long the lock lasts. */
export class LockedError extends ApiError {
  constructor(message: string, public retryAfterSeconds: number) {
    super(423, message)
  }
}

/**
 * Login-flow request: no bearer token, and a 401 here means "wrong
 * credentials", not "session expired" - so it must not clear the stored
 * token or fire `gtr:unauthorized` the way `request()` does.
 */
async function authRequest<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    let detail = `خطای ${response.status}`
    let retryAfter = 0
    try {
      const payload = await response.json()
      detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail)
      retryAfter = Number(payload.retry_after_seconds) || 0
    } catch {
      /* non-JSON error body */
    }
    if (response.status === 423) throw new LockedError(detail, retryAfter)
    throw new ApiError(response.status, detail)
  }

  return response.json() as Promise<T>
}

/** Step 1: username + password. Returns a token, or a challenge when 2FA is on. */
export function loginStart(username: string, password: string): Promise<LoginResult> {
  return authRequest<LoginResult>('/api/auth/login', { username, password })
}

/** Step 2: the challenge from `loginStart` plus a 6-digit TOTP or a backup code. */
export function loginTotp(challenge: string, code: string): Promise<TokenResponse> {
  return authRequest<TokenResponse>('/api/auth/login/totp', { challenge, code })
}

/** Revokes the current token on the server and forgets it locally. */
export async function logout(): Promise<void> {
  try {
    await request<void>('/api/auth/logout', { method: 'POST' })
  } catch (error) {
    // an already-dead session is as logged out as it gets
    if (!(error instanceof ApiError && error.status === 401)) throw error
  } finally {
    setToken(null)
  }
}

/** Revokes every token ever issued (including this one) and forgets it locally. */
export async function logoutAll(): Promise<void> {
  try {
    await request<void>('/api/auth/logout-all', { method: 'POST' })
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) throw error
  } finally {
    setToken(null)
  }
}

export function me(): Promise<Me> {
  return request<Me>('/api/auth/me')
}

/** Creates a pending secret + QR. Nothing is enforced until `totpEnable`. */
export function totpSetup(): Promise<TotpSetup> {
  return request<TotpSetup>('/api/auth/totp/setup', { method: 'POST' })
}

/** Confirms the pending secret with a code from the authenticator app. Wrong code -> 400. */
export function totpEnable(code: string): Promise<TotpEnabled> {
  return request<TotpEnabled>('/api/auth/totp/enable', {
    method: 'POST',
    body: JSON.stringify({ code }),
  })
}

/** Turns 2FA off; accepts a TOTP or a backup code. Wrong code -> 400, locked -> 423. */
export function totpDisable(code: string): Promise<TotpDisabled> {
  return request<TotpDisabled>('/api/auth/totp/disable', {
    method: 'POST',
    body: JSON.stringify({ code }),
  })
}

// ---------------------------------------------------------------- endpoints

export const api = {
  login: (username: string, password: string) =>
    request<{ access_token: string; expires_in: number }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  loginStart,
  loginTotp,
  logout,
  logoutAll,
  me,
  totpSetup,
  totpEnable,
  totpDisable,

  overview: () => request<Overview>('/api/overview'),
  positions: (status = 'open', limit = 100) =>
    request<Position[]>(`/api/positions?status=${status}&limit=${limit}`),
  signals: (limit = 60) => request<Signal[]>(`/api/signals?limit=${limit}`),
  /** What the 4h core is watching: next evaluation, regime, ranks, channels, gates. */
  watch: () => request<Watch>('/api/watch'),
  equity: (hours = 72) => request<EquityPoint[]>(`/api/equity?hours=${hours}`),
  events: (limit = 150, level?: string) =>
    request<EventRow[]>(`/api/events?limit=${limit}${level ? `&level=${level}` : ''}`),
  stats: (days = 30) => request<any>(`/api/stats?days=${days}`),
  orders: (limit = 100) => request<any[]>(`/api/orders?limit=${limit}`),
  llmReviews: (limit = 20) => request<any[]>(`/api/llm/reviews?limit=${limit}`),

  capital: () => request<CapitalFlow[]>('/api/capital'),
  addCapital: (kind: 'deposit' | 'withdrawal', amount: number, note?: string) =>
    request<CapitalFlow>('/api/capital', {
      method: 'POST',
      body: JSON.stringify({ kind, amount, note }),
    }),

  settings: () => request<{ effective: Record<string, any>; static: StaticSettings }>('/api/settings'),
  fx: () => request<TomanRate>('/api/fx'),
  patchSettings: (patch: Record<string, any>) =>
    request<any>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),

  arm: (armed: boolean, confirm?: string) =>
    request<any>('/api/control/arm', {
      method: 'POST',
      body: JSON.stringify({ armed, confirm }),
    }),
  kill: () => request<any>('/api/control/kill', { method: 'POST' }),
  resume: () => request<any>('/api/control/resume', { method: 'POST' }),
  command: (action: string, positionId?: number) =>
    request<any>('/api/control/command', {
      method: 'POST',
      body: JSON.stringify({ action, position_id: positionId }),
    }),

  sync: () => request<SyncState>('/api/exchange/sync'),

  /** Investment committee: the minutes, a manual meeting, the user's verdict on proposals. */
  committee: (limit = 20) => request<CommitteeIndex>(`/api/committee/minutes?limit=${limit}`),
  committeeMeeting: (id: number) => request<CommitteeMeeting>(`/api/committee/minutes/${id}`),
  committeeMeet: () => request<{ queued: boolean }>('/api/committee/meet', { method: 'POST' }),
  committeeVerdict: (meetingId: number, index: number, verb: 'approve' | 'decline') =>
    request<CommitteeMeeting>(`/api/committee/proposals/${meetingId}/${index}/${verb}`, { method: 'POST' }),

  notifyVapidKey: () => request<{ public_key: string }>('/api/notify/vapid-public-key'),
  notifySubscribe: (subscription: PushSubscribeIn) =>
    request<{ ok: boolean; id: number; devices: number }>('/api/notify/subscribe', {
      method: 'POST',
      body: JSON.stringify(subscription),
    }),
  notifyUnsubscribe: (endpoint: string) =>
    request<{ ok: boolean; removed: boolean; devices: number }>('/api/notify/subscribe', {
      method: 'DELETE',
      body: JSON.stringify({ endpoint }),
    }),
  notifyPreferences: () => request<NotifyPrefs>('/api/notify/preferences'),
  notifyPatchPreferences: (patch: Partial<{ categories: Partial<Record<NotifyCategory, boolean>>; quiet_hours: QuietHours }>) =>
    request<NotifyPrefs>('/api/notify/preferences', { method: 'PATCH', body: JSON.stringify(patch) }),
  notifyStatus: () => request<NotifyStatus>('/api/notify/status'),
  notifyTest: () => request<NotifyTestResult>('/api/notify/test', { method: 'POST' }),

  backtest: (symbol: string, candles = 1500, startingCapital = 100, timeframe?: string) =>
    request<BacktestResult>('/api/backtest', {
      method: 'POST',
      body: JSON.stringify({
        symbol,
        candles,
        starting_capital: startingCapital,
        timeframe,
      }),
    }),
}
