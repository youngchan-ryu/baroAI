import type { components } from '../api/schema'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { request } from './api'

export type Runtime = components['schemas']['RuntimeView']
const Context = createContext<Runtime | null>(null)
export function RuntimeProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation('product')
  const [runtime, setRuntime] = useState<Runtime | null>(null)
  const [error, setError] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    request<Runtime>('/runtime').then(value => {
      const expected = import.meta.env.VITE_APP_MODE
      if (!['demo', 'service', 'prototype', 'public_demo'].includes(value.mode) || !['replay', 'live', 'unavailable'].includes(value.llm_mode) || !['local', 'smtp', 'gmail', 'disabled'].includes(value.mail_delivery) || value.auth_method !== (value.mode === 'service' ? 'email_otp' : value.mode === 'prototype' ? 'prototype_admin' : value.mode === 'public_demo' ? 'public_session' : 'demo') || (['prototype', 'public_demo'].includes(value.mode) && (value.mail_delivery !== 'disabled' || value.llm_mode !== 'replay')) || (expected && expected !== value.mode)) throw new Error('runtime_mismatch')
      if (active) { setRuntime(value); setError(false) }
    }).catch(() => { if (active) setError(true) })
    return () => { active = false }
  }, [revision])
  if (error) return <main className="runtime-state"><h1>{t('runtimeUnavailable')}</h1><p>{t('runtimeUnavailableHint')}</p><div className="form-actions"><button className="button primary" onClick={() => { setError(false); setRevision(value => value + 1) }}>{t('retry')}</button><a className="button secondary" href="/">처음 화면으로</a></div></main>
  if (!runtime) return <main className="runtime-state" role="status">{t('loading')}</main>
  return <Context.Provider value={runtime}>{children}</Context.Provider>
}
// eslint-disable-next-line react-refresh/only-export-components
export function useRuntime() { const value = useContext(Context); if (!value) throw new Error('RuntimeProvider required'); return value }
// Components also used in isolated photo/analysis previews may omit the app provider.
// eslint-disable-next-line react-refresh/only-export-components
export function useOptionalRuntime() { return useContext(Context) }
