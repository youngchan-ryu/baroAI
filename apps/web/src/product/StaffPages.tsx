import { dateTime } from './dateTime'
import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { ApiError, request } from './api'
import { safeReturnPath } from './authReturn'
import { useSession } from './session'
import type { Session } from './types'

function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === 'staff_approval_pending') return '담당자 승인 후 로그인할 수 있어요.'
    if (error.code === 'authentication_failed') return '아이디 또는 비밀번호를 확인해 주세요.'
    if (error.code === 'origin_rejected') return '접속 주소가 서비스 설정과 달라요. 서비스 주소를 확인해 주세요.'
    if (error.code === 'local_staff_unavailable') return '이 가상 직원 계정은 노트북의 127.0.0.1:5174에서 로그인해 주세요.'
    if (error.code === 'authentication_retry_required') return '로그인 상태를 저장하지 못했어요. 다시 로그인해 주세요.'
    if (error.code === 'staff_username_unavailable') return '이미 사용 중인 아이디예요.'
    if (error.code === 'auth_rate_limited') return '요청이 많아요. 잠시 후 다시 시도해 주세요.'
    if (error.code === 'staff_auth_unavailable') return '직원 계정 서비스를 준비 중이에요. 잠시 후 다시 시도해 주세요.'
    if (error.code === 'staff_self_revoke_forbidden') return '본인 계정은 해지할 수 없어요.'
    if (error.code === 'staff_last_dispatcher_forbidden') return '마지막 담당자는 해지할 수 없어요.'
    if (error.code === 'staff_open_assignments_confirmation_required') return '미종료 배정을 확인한 뒤 다시 시도해 주세요.'
    if (error.code === 'staff_application_decided') return '이미 처리된 신청이에요. 목록을 다시 확인해 주세요.'
    if (error.code === 'validation_error') return '입력한 내용을 다시 확인해 주세요.'
  }
  return '요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.'
}

export function StaffLoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { acceptSession } = useSession()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true); setError('')
    try {
      const result = await request<Session>('/auth/staff/login', { method: 'POST', body: { username, password } })
      setPassword(''); await acceptSession(result)
      navigate(safeReturnPath(params.get('next'), result.user.role), { replace: true })
    } catch (value) { setPassword(''); setError(errorMessage(value)) } finally { setBusy(false) }
  }
  return <section className="content-card login-card"><h1>직원 로그인</h1><p>승인된 직원 계정으로 로그인해 주세요.</p>
    {window.location.origin === 'http://localhost:5174' && <p className="notice">노트북 가상 직원 계정은 <a className="text-button" href="http://127.0.0.1:5174/staff/login">127.0.0.1에서 직원 로그인 열기</a>를 이용해 주세요.</p>}
    <form onSubmit={submit}><label htmlFor="staff-username">아이디</label><input id="staff-username" autoComplete="username" required minLength={3} maxLength={64} value={username} onChange={event => setUsername(event.target.value)} /><label htmlFor="staff-password">비밀번호</label><input id="staff-password" type="password" autoComplete="current-password" required minLength={8} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} /><button className="button primary" disabled={busy}>{busy ? '확인 중' : '로그인'}</button></form>{error && <p className="field-error" role="alert">{error}</p>}<div className="staff-auth-links"><Link className="button secondary" to="/staff/register">직원 계정 신청</Link><Link className="button secondary" to="/login">신고자 로그인</Link></div></section>
}

export function StaffRegisterPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [role, setRole] = useState<'dispatcher' | 'worker'>('worker')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    if (password !== confirmation) { setError('비밀번호가 서로 같지 않아요.'); return }
    if (!displayName.trim()) { setError('이름을 입력해 주세요.'); return }
    setBusy(true); setError('')
    try {
      await request<{ status: 'pending' }>('/auth/staff/register', { method: 'POST', body: { username, password, display_name: displayName, role } })
      setPassword(''); setConfirmation(''); setDone(true)
    } catch (value) { setPassword(''); setConfirmation(''); setError(errorMessage(value)) } finally { setBusy(false) }
  }
  if (done) return <section className="content-card login-card"><h1>신청을 받았어요</h1><p>승인되면 직원 로그인에서 로그인할 수 있어요.</p><Link className="button primary" to="/staff/login">직원 로그인</Link></section>
  return <section className="content-card login-card"><h1>직원 계정 신청</h1><p>신청한 역할은 담당자가 확인한 뒤 승인해요.</p><form onSubmit={submit}><label htmlFor="staff-new-username">아이디</label><input id="staff-new-username" autoComplete="username" required minLength={3} maxLength={64} pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,63}" value={username} onChange={event => setUsername(event.target.value)} /><p className="field-hint">영문, 숫자, 마침표, 밑줄, 하이픈을 사용할 수 있어요.</p><label htmlFor="staff-new-password">비밀번호</label><input id="staff-new-password" type="password" autoComplete="new-password" required minLength={8} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} /><p className="field-hint">8자 이상 128자 이하로 입력해 주세요.</p><label htmlFor="staff-confirm-password">비밀번호 확인</label><input id="staff-confirm-password" type="password" autoComplete="new-password" required minLength={8} maxLength={128} value={confirmation} onChange={event => setConfirmation(event.target.value)} /><label htmlFor="staff-name">이름</label><input id="staff-name" autoComplete="name" required maxLength={100} value={displayName} onChange={event => setDisplayName(event.target.value)} /><label htmlFor="staff-role">신청 역할</label><select id="staff-role" value={role} onChange={event => setRole(event.target.value as 'dispatcher' | 'worker')}><option value="worker">작업자</option><option value="dispatcher">담당자</option></select><button className="button primary" disabled={busy}>{busy ? '신청 중' : '신청하기'}</button></form>{error && <p className="field-error" role="alert">{error}</p>}<div className="staff-auth-links"><Link className="button secondary" to="/staff/login">직원 로그인으로 돌아가기</Link></div></section>
}

type Application = { id: string; username: string; display_name: string; role: 'worker' | 'dispatcher'; created_at: string }
export function StaffApplicationsPage() {
  const { session } = useSession()
  const [items, setItems] = useState<Application[]>([])
  const [reason, setReason] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  async function load() {
    try { const result = await request<{ items: Application[] }>('/admin/staff-applications'); setItems(result.items); setError('') } catch (value) { setError(errorMessage(value)) }
  }
  useEffect(() => {
    let current = true
    request<{ items: Application[] }>('/admin/staff-applications').then(result => {
      if (current) { setItems(result.items); setLoading(false) }
    }).catch(value => { if (current) { setError(errorMessage(value)); setLoading(false) } })
    return () => { current = false }
  }, [])
  async function decide(item: Application, decision: 'approve' | 'reject') {
    if (!session || busy || !reason[item.id]?.trim()) return
    setBusy(true); setError(''); setNotice('')
    try {
      await request(`/admin/staff-applications/${encodeURIComponent(item.id)}/decision`, { method: 'POST', body: { decision, reason: reason[item.id].trim() } }, { csrf: session.csrf_token })
      setNotice(decision === 'approve' ? '신청을 승인했어요.' : '신청을 반려했어요.'); await load()
    } catch (value) { setError(errorMessage(value)) } finally { setBusy(false) }
  }
  return <><section className="content-card staff-applications"><div className="staff-applications-heading"><div><h1>직원 계정 승인</h1><p>신청자의 역할을 확인하고 승인 또는 반려해 주세요.</p></div><button type="button" className="button secondary" disabled={busy || loading} onClick={() => void load()}>목록 새로고침</button></div>{notice && <p role="status">{notice}</p>}{error && <p className="field-error" role="alert">{error}</p>}{loading && <p role="status">신청 목록을 불러오고 있어요.</p>}{!loading && items.length === 0 && !error && <p>대기 중인 신청이 없어요.</p>}<div className="staff-application-list">{items.map(item => <section className="staff-application" key={item.id}><div className="staff-application-title"><h2>{item.display_name}</h2><span>{item.role === 'worker' ? '작업자' : '담당자'}</span></div><p className="staff-application-meta">아이디: {item.username} · 신청 시각: <time dateTime={item.created_at}>{dateTime(item.created_at, 'full')}</time></p><label htmlFor={`reason-${item.id}`}>처리 사유</label><textarea id={`reason-${item.id}`} rows={2} maxLength={1000} value={reason[item.id] ?? ''} onChange={event => setReason({ ...reason, [item.id]: event.target.value })} /><div className="staff-application-actions"><button type="button" className="button primary" disabled={busy || !reason[item.id]?.trim()} onClick={() => void decide(item, 'approve')}>승인</button><button type="button" className="button secondary" disabled={busy || !reason[item.id]?.trim()} onClick={() => void decide(item, 'reject')}>반려</button></div></section>)}</div></section><StaffAccountManagement /></>
}


type StaffAccount = { id: string; user_id: string; display_name: string; role: 'worker' | 'dispatcher'; open_assignments: number }
type StaffEvent = { action: string; created_at: string; target_name: string; actor_name: string | null; reason: string | null }
function StaffAccountManagement() {
  const { session } = useSession()
  const [accounts, setAccounts] = useState<StaffAccount[]>([])
  const [events, setEvents] = useState<StaffEvent[]>([])
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function load() {
    try {
      const [a, e] = await Promise.all([request<{ items: StaffAccount[] }>('/admin/staff-accounts'), request<{ items: StaffEvent[] }>('/admin/staff-account-events')])
      setAccounts(a.items); setEvents(e.items); setError('')
    } catch (value) { setError(errorMessage(value)) }
  }
  useEffect(() => {
    let current = true
    Promise.all([request<{ items: StaffAccount[] }>('/admin/staff-accounts'), request<{ items: StaffEvent[] }>('/admin/staff-account-events')]).then(([a, e]) => {
      if (current) { setAccounts(a.items); setEvents(e.items); setError('') }
    }).catch(value => { if (current) setError(errorMessage(value)) })
    return () => { current = false }
  }, [])
  async function revoke(account: StaffAccount) {
    if (!session || busy || !reasons[account.id]?.trim() || (account.open_assignments > 0 && !confirmed[account.id])) return
    setBusy(true); setError('')
    try {
      await request(`/admin/staff-accounts/${encodeURIComponent(account.id)}/revoke`, { method: 'POST', body: { reason: reasons[account.id].trim(), confirm_open_assignments: Boolean(confirmed[account.id]) } }, { csrf: session.csrf_token })
      await load()
    } catch (value) { setError(errorMessage(value)) } finally { setBusy(false) }
  }
  return <section className="content-card"><h2>승인된 직원 계정</h2>{error && <p className="field-error" role="alert">{error}</p>}{accounts.map(account => <article key={account.id}><h3>{account.display_name} · {account.role === 'worker' ? '작업자' : '담당자'}</h3>{account.open_assignments > 0 && <p className="notice">미종료 배정 {account.open_assignments}건이 있어요. 해지 후 재배정이 필요해요.</p>}{account.user_id === session?.user.id ? <p>본인 계정은 해지할 수 없어요.</p> : <><label>해지 사유<textarea value={reasons[account.id] ?? ''} maxLength={1000} onChange={event => setReasons({ ...reasons, [account.id]: event.target.value })} /></label>{account.open_assignments > 0 && <label className="checkbox-label"><input type="checkbox" checked={Boolean(confirmed[account.id])} onChange={event => setConfirmed({ ...confirmed, [account.id]: event.target.checked })} />미종료 배정의 재배정 필요를 확인했어요.</label>}<button type="button" className="button secondary" disabled={busy || !reasons[account.id]?.trim() || (account.open_assignments > 0 && !confirmed[account.id])} onClick={() => void revoke(account)}>계정 해지</button></>}</article>)}<h2>최근 계정 이력</h2>{events.length === 0 ? <p>기록이 없어요.</p> : <ul>{events.map((event, index) => <li key={`${event.created_at}-${index}`}>{dateTime(event.created_at, 'full')} · {event.target_name} · {({applied:'신청',approved:'승인',rejected:'반려',revoked:'해지'} as Record<string, string>)[event.action] ?? event.action} · {event.actor_name ?? '신청자'}{event.reason && ` · ${event.reason}`}</li>)}</ul>}</section>
}
