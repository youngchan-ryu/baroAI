import './homeFlow.css'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useTranslation } from 'react-i18next'
import { useSession } from './session'
import { useOptionalRuntime } from './runtime'
import { ErrorNotice } from './shared'
import { request } from './api'
import type { Persona } from './types'
import type { components } from '../api/schema'
import { safeReturnPath } from './authReturn'

const OPTIONAL_WORKERS = ['worker_c', 'worker_d', 'worker_e', 'worker_f', 'worker_g'] as const satisfies readonly Persona[]

export function HomePage() {
  const { t } = useTranslation('product')
  const runtime = useOptionalRuntime()
  const { session, login, switching, error, loading } = useSession()
  const [selected, setSelected] = useState<Persona | null>(null)
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [extraWorkers, setExtraWorkers] = useState<Persona[]>([])
  useEffect(() => {
    let active = true
    if (runtime?.mode === 'demo' && !loading && !switching) {
      request<components['schemas']['DemoWorkers']>('/auth/demo-workers').then(result => {
        if (!active) return
        const values = Array.isArray(result.personas) ? result.personas : []
        setExtraWorkers(OPTIONAL_WORKERS.filter(persona => values.includes(persona)))
      }).catch(() => { if (active) setExtraWorkers([]) })
    }
    return () => { active = false }
  }, [runtime?.mode, loading, switching])
  const primaryPersonas: Persona[] = ['reporter_a', 'dispatcher', 'worker_a']
  const extraPersonas: Persona[] = ['reporter_b', 'worker_b', ...(runtime?.mode === 'demo' && !loading && !switching ? extraWorkers : [])]
  const campusNames: Record<string, string> = { worker_a: '전기 담당 1', worker_b: '설비 담당 1', worker_c: '건축 담당 1', worker_d: '토목 담당 1', worker_e: '전기 담당 2', worker_f: '설비 담당 2', worker_g: '시설관리 담당 1' }
  const campusWorkers = runtime?.mode === 'demo' && extraWorkers.length > 0
  async function enter(persona: Persona) {
    setSelected(persona)
    const role = persona === 'dispatcher' ? 'dispatcher' : persona.startsWith('worker_') ? 'worker' : 'reporter'
    const next = params.get('next')
    try {
      await login(persona)
      navigate(next || role !== 'reporter' ? safeReturnPath(next, role) : '/me')
    } catch { /* Render safe API error from session. */ }
  }
  const roleCard = (persona: Persona, step?: string) => <button key={persona} className={`role-card ${persona === 'dispatcher' ? 'role-admin' : ''}`} disabled={switching || loading} onClick={() => void enter(persona)}><span className="role-icon" aria-hidden="true">{persona === 'dispatcher' ? '▤' : persona.slice(-1).toUpperCase()}</span>{step && <span className="role-step">{step}</span>}<strong>{campusWorkers && campusNames[persona] ? campusNames[persona] : t(persona === 'reporter_a' ? 'reporterA' : persona === 'reporter_b' ? 'reporterB' : persona === 'dispatcher' ? 'dispatcher' : persona === 'worker_a' ? 'workerA' : persona === 'worker_b' ? 'workerB' : persona === 'worker_c' ? 'workerC' : 'workerD')}</strong><span className="role-description">{t(persona === 'dispatcher' ? 'dispatcherDesc' : persona.startsWith('worker_') ? 'workerDesc' : 'reporterDesc')}</span><span className="role-action">{t(switching && selected === persona ? 'loginLoading' : 'roleEnter')} <span aria-hidden="true">↗</span></span></button>
  return <>
    <section className="product-hero"><div className="hero-copy"><p className="product-eyebrow">{t('heroEyebrow')}</p><h1>{t('heroTitle')}</h1><p className="hero-lead">{t('heroLead')}</p></div><div className="hero-art" aria-hidden="true"><div className="art-orbit" /><div className="art-building"><div className="building-cap" /><div className="building-windows">{Array.from({ length: 9 }, (_, index) => <span key={index} />)}</div><div className="building-door" /></div><div className="art-check">✓</div><div className="art-note"><span /><span /><span /></div></div></section>
    <section className="role-section"><div className="section-title"><div><p className="product-eyebrow">{t('localOnly')}</p><h2>{t('demoTitle')}</h2></div>{session && <Link className="text-link" to={session.user.role === 'dispatcher' ? '/admin/queue' : session.user.role === 'worker' ? '/work' : '/me'}>{t('continue')} <span aria-hidden="true">→</span></Link>}</div>{Boolean(error) && <ErrorNotice error={error} />}<div className="role-grid">{primaryPersonas.map((persona, index) => roleCard(persona, `${index + 1} · ${['신고', '담당자 확인·배정', '작업 보고'][index]}`))}</div><details className="role-extra"><summary>다른 시연 역할 보기</summary><div className="role-grid">{extraPersonas.filter(persona => !persona.startsWith('worker_')).map(persona => roleCard(persona))}</div><details className="role-extra"><summary>다른 작업자로 보기</summary><div className="role-grid">{extraPersonas.filter(persona => persona.startsWith('worker_')).map(persona => roleCard(persona))}</div></details></details></section>

  </>
}
