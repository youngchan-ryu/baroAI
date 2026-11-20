import './serviceHome.css'
import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router'
import { useTranslation } from 'react-i18next'
import { api, ApiError } from './api'
import { useSession } from './session'
import { useRuntime } from './runtime'
import { safeReturnPath } from './authReturn'
import { ConnectionError } from './shared'

export function ServiceHomePage() {
  const { t } = useTranslation('product')
  const { session, loading, switching, error, reload } = useSession()
  const runtime = useRuntime()
  if (loading || switching) return <section className="empty-state" role="status">{t('authChecking')}</section>
  if (error) return <ConnectionError title={t('authCheckFailed')} error={null} retry={reload} />
  if (session) return <Navigate to={safeReturnPath(null, session.user.role)} replace />
  return <div className="service-home">
    <section className="product-hero service-home-hero">
      <div className="hero-copy"><p className="product-eyebrow">바로AI · 시설 업무 지원</p><h1>{t('serviceWelcomeTitle')}</h1><p className="hero-lead">{t('serviceWelcomeLead')}</p>
        {runtime.mode === 'service' && (runtime as typeof runtime & { photo_ai_capability?: string }).photo_ai_capability !== 'available' && <p className="field-hint">사진은 담당자가 직접 확인해요.</p>}
        <div className="hero-tags"><span>AI 초안 + 사람 확인</span></div>
        <div className="service-home-actions"><Link className="button primary" to="/login">{t('serviceLogin')}</Link><Link className="button secondary" to="/report">{t('serviceWelcomeReport')}</Link></div>
        {runtime.mode === 'service' && runtime.staff_password_auth && <Link className="service-staff-link" to="/staff/login">직원 로그인</Link>}
      </div>
      <div className="hero-art" aria-hidden="true"><div className="art-orbit" /><div className="art-building"><div className="building-cap" /><div className="building-windows">{Array.from({ length: 9 }, (_, index) => <span key={index} />)}</div><div className="building-door" /></div><div className="art-check">✓</div><div className="art-note"><span /><span /><span /></div></div>
    </section>
    <section className="service-home-steps" aria-labelledby="service-flow-title"><div><p className="product-eyebrow">이용 순서</p><h2 id="service-flow-title">이렇게 진행돼요</h2></div><ol><li><span>01</span><strong>신고</strong><p>시설의 불편한 점을 알려주세요.</p></li><li><span>02</span><strong>담당자 확인·배정</strong><p>담당자가 내용을 확인하고 작업을 배정해요.</p></li><li><span>03</span><strong>작업 결과 회신</strong><p>확인된 작업 결과를 살펴볼 수 있어요.</p></li></ol></section>
  </div>
}
function emailError(value: string) {
  const stripped = value.trim()
  if (!/^[\x20-\x7e]+$/.test(stripped)) return 'email_invalid'
  const normalized = (stripped.includes('@') ? stripped : `${stripped}@snu.ac.kr`).toLowerCase()
  if (!/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9.-]+$/.test(normalized)) return 'email_invalid'
  return normalized.split('@')[1] === 'snu.ac.kr' ? '' : 'email_domain_rejected'
}
function displayId(value: string) {
  const trimmed = value.trim()
  return /^[\x20-\x7e]+$/.test(trimmed) && trimmed.toLowerCase().endsWith('@snu.ac.kr') ? trimmed.slice(0, -'@snu.ac.kr'.length) : value
}
export function LoginPage() {
  const { t } = useTranslation('product')
  const runtime = useRuntime()
  const { acceptSession } = useSession()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const normalizedEmail = () => { const value = email.trim().toLowerCase(); return value.includes('@') ? value : `${value}@snu.ac.kr` }
  const [requestedEmail, setRequestedEmail] = useState('')
  const [deadline, setDeadline] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const secondsLeft = deadline === null ? 0 : Math.max(0, Math.ceil((deadline - now) / 1000))
  const timerText = `${secondsLeft >= 60 ? Math.floor(secondsLeft / 60) : '00'}:${String(secondsLeft % 60).padStart(2, '0')}`
  useEffect(() => {
    if (deadline === null) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [deadline])
  const [delivery, setDelivery] = useState(runtime.mail_delivery)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function requestCode(event: FormEvent) {
    event.preventDefault(); if (busy) return
    const invalid = emailError(email); if (invalid) { setCode(''); setRequestedEmail(''); setError(invalid); return }
    if (['baroai-manager@snu.ac.kr', 'baroai-worker@snu.ac.kr'].includes(normalizedEmail())) { setError('reserved_staff'); return }
    setBusy(true); setError('')
    try { const result = await api.requestOtp(normalizedEmail()); setRequestedEmail(normalizedEmail()); setCode(''); setDelivery(result.delivery); const receivedAt = Date.now(); setNow(receivedAt); setDeadline(receivedAt + result.expires_in_seconds * 1000) } catch (value) { setError(value instanceof ApiError && ['auth_rate_limited', 'mail_unavailable', 'email_domain_rejected', 'email_invalid'].includes(value.code) ? value.code : 'auth_request_failed') } finally { setBusy(false) }
  }
  async function verify(event: FormEvent) {
    event.preventDefault(); if (busy || deadline === null || Date.now() >= deadline || !/^\d{6}$/.test(code) || requestedEmail !== normalizedEmail()) return
    const invalid = emailError(email); if (invalid) { setCode(''); setRequestedEmail(''); setError(invalid); return }
    setBusy(true); setError('')
    try { const session = await api.verifyOtp(requestedEmail, code); await acceptSession(session); setCode(''); navigate(safeReturnPath(params.get('next'), session.user.role), { replace: true }) } catch (value) {
      if (value instanceof ApiError && value.code === 'authentication_retry_required') {
        setCode(''); setRequestedEmail(''); setError('authentication_retry_required')
      } else if (value instanceof ApiError && ['email_domain_rejected', 'email_invalid'].includes(value.code)) {
        setCode(''); setRequestedEmail(''); setError(value.code)
      } else { setCode(''); setError(value instanceof ApiError && value.code === 'auth_rate_limited' ? 'auth_rate_limited' : 'otp_invalid') }
    } finally { setBusy(false) }
  }
  async function prototypeLogin(event: FormEvent) {
    event.preventDefault(); if (busy) return
    const invalid = emailError(email)
    if (invalid || normalizedEmail() !== 'baroai-admin@snu.ac.kr') { setPassword(''); setError(invalid || 'prototype_invalid'); return }
    setBusy(true); setError('')
    try { const session = await api.prototypeLogin(normalizedEmail(), password); setPassword(''); await acceptSession(session); navigate(safeReturnPath(params.get('next'), session.user.role), { replace: true }) }
    catch { setPassword(''); setError('prototype_invalid') } finally { setBusy(false) }
  }
  if (runtime.mode === 'prototype') return <section className="content-card login-card"><p className="product-eyebrow">합성 프로토타입</p><h1>담당자 시연 로그인</h1><p>일반 사용자 로그인은 학교 이메일 인증을 제공하는 서비스에서 이용해 주세요.</p><form onSubmit={event => void prototypeLogin(event)}><label htmlFor="prototype-email">학교 아이디</label><input id="prototype-email" type="text" autoComplete="username" required maxLength={254} value={email} disabled={busy} onBlur={event => setEmail(displayId(event.target.value))} onChange={event => { setEmail(event.target.value); setError('') }} />{!email.includes('@') && <span>@snu.ac.kr</span>}<label htmlFor="prototype-password">시연 암호</label><input id="prototype-password" type="password" autoComplete="current-password" required value={password} disabled={busy} onChange={event => setPassword(event.target.value)} /><button className="button primary" disabled={busy}>로그인</button></form>{error && <p role="alert" className="field-error">{t(`authError.${error}`)}</p>}</section>
  return <section className="content-card login-card"><p className="product-eyebrow">{t('service')}</p><h1>{t('serviceLogin')}</h1><p>{t('loginEmailHint')}</p>{delivery === 'local' && <p className="notice">{t('loginLocalMail')}</p>}{delivery === 'disabled' && <p className="notice">{t('loginMailDisabled')}</p>}<form onSubmit={event => void requestCode(event)}><label htmlFor="login-email">{t('emailLabel')}</label><div className="login-email-row"><input id="login-email" type="text" autoComplete="email" inputMode="email" required maxLength={254} value={email} disabled={busy} onBlur={event => setEmail(displayId(event.target.value))} onChange={event => { setEmail(event.target.value); setCode(''); setRequestedEmail(''); setDeadline(null); setError('') }} />{!email.includes('@') && <span>@snu.ac.kr</span>}</div><button className="button secondary" disabled={busy || delivery === 'disabled'}>{t(requestedEmail ? 'requestCodeAgain' : 'requestCode')}</button></form>{requestedEmail && <form onSubmit={event => void verify(event)}><p role="status">{t(delivery === 'local' ? 'loginLocalRequested' : 'loginCodeRequested')}</p><p className="field-hint">{t('loginLatestCodeOnly')}</p><p role="timer" aria-live="off">{t('loginTimeRemaining')} {timerText}</p>{secondsLeft === 0 && <p role="status">{t('loginCodeExpired')}</p>}<label htmlFor="login-code">{t('codeLabel')}</label><input id="login-code" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required value={code} disabled={busy} onChange={event => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} /><button className="button primary" disabled={busy || secondsLeft === 0 || code.length !== 6}>{t('verifyLogin')}</button></form>}{error && <p role="alert" className="field-error">{error === 'reserved_staff' ? '이 주소는 사용할 수 없어요. 직원은 직원 로그인으로 이동해 주세요.' : t(`authError.${error}`)}</p>}<p className="field-hint">{t('loginMemoryOnly')}</p>{runtime.mode === 'service' && runtime.staff_password_auth && <div className="staff-auth-links"><Link className="button secondary" to={`/staff/login${params.get('next') ? `?next=${encodeURIComponent(params.get('next') ?? '')}` : ''}`}>직원 로그인</Link></div>}</section>
}
