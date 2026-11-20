import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import { ReportSnapshot } from './ReportWorkflow'
import type { Report } from './reportTypes'
import type { Ticket } from './types'

export type OrphanTicket = Ticket
export function OrphanedReports({ ticket, onUpdate, onLoadLatest }: { ticket: Ticket; onUpdate: () => void; onLoadLatest: () => Promise<Ticket> }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const reports = (ticket as OrphanTicket).orphaned_reports ?? []
  if (session?.user.role !== 'dispatcher' || !reports.length) return null
  return <section id="orphaned-reports" className="orphaned-reports"><h2>{t('orphanReportsTitle')}</h2><p>{t('orphanReportsHint')}</p>{reports.map(report => <OrphanedReport key={report.id} report={report} ticketId={ticket.id} csrf={session.csrf_token} onUpdate={onUpdate} onLoadLatest={onLoadLatest} />)}</section>
}
function OrphanedReport({ report, ticketId, csrf, onUpdate, onLoadLatest }: { report: Report; ticketId: string; csrf: string; onUpdate: () => void; onLoadLatest: () => Promise<Ticket> }) {
  const { t } = useTranslation('product')
  const [snapshot, setSnapshot] = useState(report)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const stale = snapshot.revision !== report.revision
  async function reload() { setBusy(true); setError(null); try { const latest = await onLoadLatest() as OrphanTicket; const found = latest.orphaned_reports?.find(item => item.id === report.id); if (!found) throw new Error('report_unavailable'); setSnapshot(found); key.current = null; onUpdate() } catch (value) { setError(value) } finally { setBusy(false) } }
  async function dismiss() {
    if (busy || stale || !reason.trim() || report.analysis_status === 'dismissed') return
    const body = { expected_report_revision: snapshot.revision, reason: reason.trim() }
    key.current = persistentKey(key.current, body); setBusy(true); setError(null)
    try { await request<Ticket>(`/tickets/${ticketId}/reports/${report.id}/dismiss`, { method: 'POST', body, key: key.current.key }, { csrf }); onUpdate() } catch (value) { setError(value) } finally { setBusy(false) }
  }
  return <article className="orphaned-report"><ReportSnapshot report={report.analysis_status === 'dismissed' ? report : snapshot} /><div className="content-card"><h3>{t(report.analysis_status === 'dismissed' ? 'orphanClosed' : 'orphanReviewTitle')}</h3><p className="field-hint">{t('orphanNotApproved')}</p>{report.analysis_status !== 'dismissed' && <>{stale && <p className="notice">{t('reportStale')}</p>}<button type="button" className="text-button" disabled={busy} onClick={() => void reload()}>{t('orphanReload')}</button><label>{t('orphanReason')}<textarea rows={3} value={reason} maxLength={1000} disabled={busy} onChange={event => setReason(event.target.value)} /></label><button type="button" className="button secondary" disabled={busy || stale || !reason.trim()} onClick={() => void dismiss()}>{t('orphanDismiss')}</button></>}{Boolean(error) && <ErrorNotice error={error} />}</div></article>
}
