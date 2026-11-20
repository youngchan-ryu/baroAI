import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getHealth, getReadiness } from './api/client'

export function ConnectionPage() {
  const { t } = useTranslation()
  const health = useQuery({ queryKey: ['health'], queryFn: ({ signal }) => getHealth(signal) })
  const readiness = useQuery({ queryKey: ['readiness'], queryFn: ({ signal }) => getReadiness(signal) })
  const pending = health.isFetching || readiness.isFetching
  const dbReady = readiness.data?.status === 'ready'
  const mode = health.isError ? undefined : health.data?.llm_mode
  const retry = () => { void health.refetch(); void readiness.refetch() }

  return <div className="page-shell">
    <header className="site-header"><a className="brand" href="/" aria-label={t('brand')}><img className="brand-mark" src="/icon.svg" alt="" width={40} height={40} />{t('brand')}</a><span className="stage">{t('stage')}</span></header>
    <main>
      <section className="intro" aria-labelledby="page-title"><p className="eyebrow">{t('eyebrow')}</p><h1 id="page-title">{t('title')}</h1><p className="lead">{t('intro')}</p></section>
      <section className="connection-panel" aria-label={t('stage')}>
        <div className="panel-heading"><div><span className="small-label">{t('principle')}</span><h2>{t('stage')}</h2></div><button onClick={retry} disabled={pending}>{t(pending ? 'refreshing' : 'retry')}<span aria-hidden="true"> ↗</span></button></div>
        <div className="status-grid" aria-live="polite" aria-busy={pending}>
          <article className="status-card"><div className="card-top"><span className="card-number" aria-hidden="true">01</span><span className={`status ${health.isPending ? 'waiting' : health.isError ? 'error' : 'success'}`}><span className="dot" />{t(health.isPending ? 'checking' : health.isError ? 'failed' : 'connected')}</span></div><h3>{t('api')}</h3><p>{t('apiDescription')}</p>{health.isError && <p className="error-hint">{t('retryHint')}</p>}{health.data && !health.isError && <p className="environment">{t('environment')} · {t(health.data.environment)}</p>}</article>
          <article className="status-card"><div className="card-top"><span className="card-number" aria-hidden="true">02</span><span className={`status ${readiness.isPending ? 'waiting' : readiness.isError || !dbReady ? 'error' : 'success'}`}><span className="dot" />{t(readiness.isPending ? 'checking' : readiness.isError ? 'failed' : dbReady ? 'ready' : 'unavailable')}</span></div><h3>{t('database')}</h3><p>{t('databaseDescription')}</p>{readiness.isError && <p className="error-hint">{t('retryHint')}</p>}{!readiness.isError && readiness.data && !dbReady && <p className="error-hint">{t('dbHint')}</p>}</article>
        </div>
        <aside className="mode-note"><span className="mode-badge">{t(mode === 'replay' ? 'replay' : mode === 'live' ? 'live' : mode === 'unavailable' ? 'aiUnavailable' : 'unknown')}</span><p>{t(mode === 'replay' ? 'replayHint' : mode === 'live' ? 'liveHint' : mode === 'unavailable' ? 'aiUnavailableHint' : 'unknownHint')}</p></aside>
      </section>
      <p className="scope-note">{t('scope')}</p>
    </main>
    <footer>{t('footer')}</footer>
  </div>
}
