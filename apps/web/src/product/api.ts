import type { components } from '../api/schema'
import { dateTime } from './dateTime'
import type { Assigned, Assignment, Catalog, Persona, Photo, Session, Ticket } from './types'

export class ApiError extends Error {
  constructor(public code: string, public status: number, public requestId?: string, public retryAt?: number) { super(code) }
}
export function publicIntakeError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  if (error.code === 'public_intake_paused') return '현재 접수가 중지되어 있어요. 운영자가 재개한 뒤 다시 시도해 주세요.'
  if (error.code === 'public_storage_limit') return '시연 저장 공간이 가득 찼어요. 운영자에게 문의해 주세요.'
  if (['public_session_limit', 'public_ip_limit', 'public_ticket_limit'].includes(error.code)) {
    const retry = error.retryAt && Number.isFinite(error.retryAt) && Math.abs(error.retryAt) <= 8.64e15 ? ` ${dateTime(error.retryAt, 'full')} 이후 다시 시도해 주세요.` : ' 잠시 후 다시 시도해 주세요.'
    return `시연 접수 한도에 도달했어요.${retry}`
  }
  if (error.code === 'public_ip_boundary_unavailable') return '현재 접수 환경을 확인할 수 없어요. 운영자에게 문의해 주세요.'
  return null
}
let authenticationEpoch = 0
export function advanceAuthenticationEpoch() { authenticationEpoch++ }
export type Auth = { csrf?: string; bearer?: string; assignmentId?: string }
export function uploadPath(auth: Auth, id?: string, suffix = '') {
  const base = auth.assignmentId ? `/worker/assignments/${encodeURIComponent(auth.assignmentId)}/uploads` : '/uploads'
  return base + (id ? `/${encodeURIComponent(id)}` : '') + (suffix ? `/${suffix}` : '')
}
export async function imageRequest(path: string, auth: Auth, signal: AbortSignal, isCurrent: () => boolean = () => true) {
  const epoch = authenticationEpoch
  const response = await fetch(`/api/v1${path}`, { cache: 'no-store', credentials: auth.bearer ? 'omit' : 'same-origin', headers: auth.bearer ? { Authorization: `Bearer ${auth.bearer}` } : {}, signal })
  if (!response.ok) {
    if (response.status === 401 && !auth.bearer && epoch === authenticationEpoch && isCurrent()) window.dispatchEvent(new Event('baro:unauthorized'))
    const payload = await response.json().catch(() => null)
    if (auth.assignmentId && epoch === authenticationEpoch && isCurrent() && payload?.error?.code === 'assignment_not_found') window.dispatchEvent(new CustomEvent('baro:assignment-ended', { detail: auth.assignmentId }))
    throw new ApiError(payload?.error?.code ?? 'photo_unavailable', response.status)
  }
  const blob = await response.blob()
  if (epoch !== authenticationEpoch || !isCurrent()) throw new Error('authentication_changed')
  return blob
}
export async function request<T>(path: string, options: { method?: string; body?: unknown; key?: string; signal?: AbortSignal; timeoutMs?: number; photoLocation?: boolean; photoCoordinates?: boolean } = {}, auth: Auth = {}): Promise<T> {
  const requestEpoch = authenticationEpoch
  const headers = new Headers({ Accept: 'application/json' })
  if (auth.bearer) headers.set('Authorization', `Bearer ${auth.bearer}`)
  else if (auth.csrf) headers.set('X-CSRF-Token', auth.csrf)
  if (options.key) headers.set('Idempotency-Key', options.key)
  if (options.photoLocation) headers.set('X-Baro-Photo-Location', 'intake')
  if (options.photoCoordinates) headers.set('X-Baro-Photo-Coordinates', 'consent')
  const isForm = options.body instanceof FormData
  if (options.body !== undefined && !isForm) headers.set('Content-Type', 'application/json')
  const timeout = AbortSignal.timeout(options.timeoutMs ?? 15_000)
  const response = await fetch(`/api/v1${path}`, {
    cache: 'no-store', method: options.method ?? 'GET', headers, credentials: auth.bearer ? 'omit' : 'same-origin',
    body: options.body === undefined ? undefined : isForm ? options.body as FormData : JSON.stringify(options.body),
    signal: options.signal ? AbortSignal.any([options.signal, timeout]) : timeout,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    if (response.status === 401 && requestEpoch === authenticationEpoch && !auth.bearer && (!path.startsWith('/auth/') || path === '/auth/me')) window.dispatchEvent(new Event('baro:unauthorized'))
    if (auth.assignmentId && requestEpoch === authenticationEpoch && payload?.error?.code === 'assignment_not_found') window.dispatchEvent(new CustomEvent('baro:assignment-ended', { detail: auth.assignmentId }))
    const retrySeconds = response.headers.get('Retry-After')
    const retryAt = retrySeconds && /^\d+$/.test(retrySeconds) ? Date.now() + Number(retrySeconds) * 1000 : undefined
    throw new ApiError(payload?.error?.code ?? 'request_failed', response.status, payload?.error?.request_id, retryAt)
  }
  const value = response.status === 204 ? undefined as T : await response.json() as T
  if (path !== '/runtime' && requestEpoch !== authenticationEpoch) throw new Error('authentication_changed')
  return value
}
export const api = {
  startPublicGuest: (csrf?: string) => request<Session>('/auth/public-guest', { method: 'POST', body: { notice_confirmed: true } }, { csrf }),
  exchangePublicInvite: (token: string) => request<Session>('/auth/public-invite/exchange', { method: 'POST', body: { token } }),
  localStaffLogin: (email: string) => request<Session>('/auth/local-staff', { method: 'POST', body: { email } }),
  prototypeLogin: (email: string, password: string) => request<Session>('/auth/prototype-login', { method: 'POST', body: { email, password } }),
  requestOtp: (email: string) => request<components['schemas']['OtpAccepted']>('/auth/otp/request', { method: 'POST', body: { email } }),
  verifyOtp: (email: string, code: string) => request<Session>('/auth/otp/verify', { method: 'POST', body: { email, code } }),
  me: () => request<Session>('/auth/me'),
  login: (persona: Persona) => request<Session>('/auth/demo', { method: 'POST', body: { persona } }),
  logout: (csrf: string) => request<void>('/auth/logout', { method: 'POST' }, { csrf }),
  tickets: (signal?: AbortSignal) => request<{ items: Ticket[] }>('/tickets', { signal }),
  ticket: (id: string, signal?: AbortSignal) => request<Ticket>(`/tickets/${encodeURIComponent(id)}`, { signal }),
  catalog: (signal?: AbortSignal) => request<Catalog>('/catalog', { signal }),
  assignment: (bearer: string, signal?: AbortSignal) => request<Assignment>('/worker/assignment', { signal }, { bearer }),
  assign: (id: string, body: unknown, csrf: string) => request<Assigned>(`/tickets/${encodeURIComponent(id)}/assign`, { method: 'POST', body }, { csrf }),
  upload: (file: File, auth: Auth, key: string, photoLocation = false, photoCoordinates = false) => { const body = new FormData(); body.set('file', file); return request<Photo>(uploadPath(auth), { method: 'POST', body, key, photoLocation, photoCoordinates }, auth) },
}

export function persistentKey(previous: { fingerprint: string; key: string } | null, input: unknown) {
  const fingerprint = JSON.stringify(input)
  return previous?.fingerprint === fingerprint ? previous : { fingerprint, key: crypto.randomUUID() }
}
