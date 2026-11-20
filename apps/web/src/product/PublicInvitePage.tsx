import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { api } from './api'
import { safeReturnPath } from './authReturn'
import { useSession } from './session'

export function PublicInvitePage() {
  const [token, setToken] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const { acceptSession, loading, switching } = useSession()
  const navigate = useNavigate()
  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }, [])
  async function exchange() {
    if (!token || busy || loading || switching) return
    setBusy(true); setError(false)
    try {
      const session = await api.exchangePublicInvite(token)
      setToken('')
      await acceptSession(session)
      navigate(safeReturnPath(null, session.user.role), { replace: true })
    } catch { setError(true) } finally { setBusy(false) }
  }
  return <section className="content-card login-card"><p className="product-eyebrow">공개 시연</p><h1>직원 초대 확인</h1><p>운영자가 전달한 초대로 담당자 또는 작업자 화면에 들어갑니다.</p>{token ? <button className="button primary" disabled={busy || loading || switching} onClick={() => void exchange()}>초대 수락하고 시작</button> : <p className="notice">사용할 초대가 없어요. 운영자에게 새 초대를 요청해 주세요.</p>}{error && <p role="alert" className="field-error">초대를 사용할 수 없어요. 만료되었거나 이미 사용한 경우 운영자에게 새 초대를 요청해 주세요.</p>}</section>
}
