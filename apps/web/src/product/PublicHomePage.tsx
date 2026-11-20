import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api, publicIntakeError } from './api'
import { useRuntime } from './runtime'
import { useSession } from './session'
import { ConnectionError } from './shared'

export function PublicHomePage() {
  const { session, loading, switching, error: sessionError, reload, acceptSession } = useSession()
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const limits = useRuntime().public_limits
  const navigate = useNavigate()
  async function start() {
    if (!confirmed || busy || loading || switching) return
    setBusy(true); setError(null)
    try {
      const guest = await api.startPublicGuest(session?.csrf_token)
      await acceptSession(guest)
      navigate('/report', { replace: true })
    } catch (value) { setError(value) } finally { setBusy(false) }
  }
  if (loading || switching) return <section className="empty-state" role="status">접속 상태를 확인하고 있어요.</section>
  if (sessionError) return <ConnectionError title="연결 상태를 확인하지 못했어요" error={sessionError} retry={reload} />
  if (session && session.user.role !== 'reporter') return <section className="content-card"><h1>직원으로 접속 중이에요</h1><p>신고를 체험하려면 다른 브라우저를 사용해 주세요. 현재 직원 접속은 유지됩니다.</p><Link className="button primary" to={session.user.role === 'worker' ? '/work' : '/admin/queue'}>내 업무로 이동</Link></section>
  return <section className="content-card"><p className="product-eyebrow">바로AI 공개 시연</p><h1>시설 불편 신고 체험</h1><p>실제 학교 시설 민원 접수는 아니에요.</p><p>신고 내용은 시연 담당자의 공동 업무 화면에 표시됩니다. 이름, 연락처 등 개인정보는 입력하지 말아 주세요.</p><p>입력 내용은 접수 후 7일이 지나면 볼 수 없고 삭제 대기 상태가 됩니다. 삭제는 운영 정리 시 이루어지며 지연될 수 있으니 민감한 내용은 입력하지 마세요.</p>{limits && <p>시연 한도: 신고자별 시간당 {limits.ticket_hourly}건, 동시 신고자 {limits.active_sessions}명, 전체 저장 {limits.ticket_storage}건. 같은 네트워크의 신규 접속은 시간당 {limits.ip_hourly}회까지예요.</p>}<label className="checkbox-label"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />실제 민원 접수가 아닌 체험임을 확인했어요.</label><button className="button primary" disabled={!confirmed || busy} onClick={() => void start()}>{session ? '신고 이어가기' : '신고 시작하기'}</button>{session && <Link className="button secondary" to="/me">내 신고 보기</Link>}{Boolean(error) && <p className="field-error" role="alert">{publicIntakeError(error) ?? "접속을 시작하지 못했어요. 연결을 확인하고 다시 시도해 주세요."}</p>}</section>
}
