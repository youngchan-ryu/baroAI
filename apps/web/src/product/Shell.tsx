import { useQuery } from '@tanstack/react-query'
import { request } from './api'
import type { PlanningCapabilities } from './planningTypes'
import { workerDisplayName } from './workerDisplay'
import { useRuntime } from './runtime'
import { Link, NavLink, Outlet, useLocation, useNavigate, useNavigationType } from 'react-router'
import { useTranslation } from 'react-i18next'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useSession } from './session'
import { ErrorNotice } from './shared'

export function Shell() {
  const runtime = useRuntime()
  const { t } = useTranslation('product')
  const { session, cacheScope, loading, switching, logout, explicitLogout, clearExplicitLogout } = useSession()
  const { pathname, hash } = useLocation()
  const navigationType = useNavigationType()
  const previousPath = useRef(pathname)
  const navigate = useNavigate()
  const [error, setError] = useState<unknown>(null)
  const [offline, setOffline] = useState(navigator.onLine === false)
  useLayoutEffect(() => {
    const changed = previousPath.current !== pathname
    previousPath.current = pathname
    if (changed && !hash && navigationType !== 'POP' && (window.scrollX || window.scrollY)) window.scrollTo(0, 0)
  }, [hash, navigationType, pathname])
  useEffect(() => { if (pathname === '/' && explicitLogout) clearExplicitLogout() }, [pathname, explicitLogout, clearExplicitLogout])
  useEffect(() => { const online = () => setOffline(navigator.onLine === false); window.addEventListener('online', online); window.addEventListener('offline', online); return () => { window.removeEventListener('online', online); window.removeEventListener('offline', online) } }, [])
  const travel = useQuery({ queryKey: ['planning-capabilities', cacheScope], queryFn: () => request<PlanningCapabilities & {travel_data_status?: string}>('/planning/capabilities'), enabled: runtime.mode === 'service' && session?.user.role === 'dispatcher' && !loading && !switching, retry: false, refetchOnWindowFocus: false })
  const showPlanning = travel.data?.travel_data_status !== 'unavailable'
  const worker = pathname === '/w'
  async function leave() {
    setError(null)
    try { await logout(() => navigate('/', { replace: true })) } catch (value) { setError(value) }
  }
  const sessionName = session?.user.role === 'worker'
    ? workerDisplayName(session.user.display_name, runtime.mode)
    : runtime.mode === 'demo' ? session?.user.display_name.replace(/^합성 /, '') : session?.user.display_name
  return <div className={`product-shell ${runtime.mode !== 'demo' ? 'service-shell' : 'demo-shell'}`}><header className="product-header"><Link className="product-brand" to="/"><img className="product-brand-icon" src="/icon.svg" alt="" width={37} height={37} /><span>바로<span className="brand-ai">AI</span></span></Link>{runtime.mode === 'demo' && <span className="demo-badge"><span />시연용 데이터</span>}{!worker && <nav aria-label={t('service')}>{loading || switching ? <span role="status">{t('authCheckingShort')}</span> : session ? <>{session.user.role === 'dispatcher' ? <><NavLink className="staff-queue" to="/admin/queue">{t('queue')}</NavLink>{runtime.mode === 'service' && showPlanning && <NavLink to="/admin/planning">{t('planningTitle')}</NavLink>}<NavLink to="/admin/settings">학교 설정</NavLink>{runtime.mode === 'service' && runtime.staff_password_auth && <NavLink to="/admin/staff-applications">직원 계정 승인</NavLink>}</> : session.user.role === 'worker' ? <><NavLink className="staff-queue" to="/work">{t('myWork')}</NavLink><Link to="/work#my-settings" onClick={() => document.getElementById('my-settings')?.scrollIntoView()}>내 분야·근무 설정</Link></> : <><NavLink to="/me">{t('my')}</NavLink><NavLink to="/report">{t('newReport')}</NavLink></>}<span className="session-controls"><span className="session-name">{sessionName}</span><button className="session-button" aria-label={`${sessionName} 나가기`} disabled={loading || switching} onClick={() => void leave()}>나가기</button></span></> : <Link className="header-login" to={runtime.mode !== 'demo' ? '/login' : '/'}>{t(runtime.mode !== 'demo' ? 'serviceLogin' : 'home')}</Link>}</nav>}</header>{runtime.mode === 'service' && !worker && !loading && !switching && session?.user.role === 'dispatcher' && <nav className="mobile-service-nav" aria-label={t('mobileNavigation')}><NavLink to="/admin/queue">{t('queue')}</NavLink>{showPlanning && <NavLink to="/admin/planning">{t('planningTitle')}</NavLink>}<NavLink to="/admin/settings">학교 설정</NavLink>{runtime.staff_password_auth && <NavLink to="/admin/staff-applications">직원 계정 승인</NavLink>}</nav>}{runtime.mode === 'service' && !worker && !loading && !switching && session && session.user.role !== 'dispatcher' && <nav className="mobile-service-nav" aria-label={t('mobileNavigation')}>{session?.user.role === 'worker' ? <><NavLink to="/work">{t('myWork')}</NavLink><Link to="/work#my-settings" onClick={() => document.getElementById('my-settings')?.scrollIntoView()}>내 분야·근무 설정</Link></> : <><NavLink to="/report">{t('serviceReportNav')}</NavLink><NavLink to="/me">{t('my')}</NavLink></>}</nav>}<main className="product-main">{offline && <div className="notice offline-notice" role="status"><strong>{t('offlineTitle')}</strong><p>{t('offlineHint')}</p></div>}{Boolean(error) && <ErrorNotice error={error} />}<Outlet /></main><footer className="product-footer"><div><strong>바로AI</strong><span>{t('human')}</span></div><p>AI 초안은 담당자가 확인한 뒤 반영돼요. 방문 일정은 이 화면에서 약속하지 않아요.</p>{runtime.mode === 'demo' ? <><Link to="/connection">{t('connection')} ↗</Link><p>{t('demoHint')} {t('syntheticNote')}</p><p>팀 BRIDGE · {t('localOnly')}</p></> : <p>{t(runtime.llm_mode === 'replay' ? 'serviceReplayDisclosure' : runtime.llm_mode === 'unavailable' ? 'aiUnavailable' : 'human')}</p>}</footer></div>
}
