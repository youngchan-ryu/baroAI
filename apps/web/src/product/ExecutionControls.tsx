import { dateTime } from './dateTime'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, persistentKey, request } from './api'
import { ErrorNotice } from './shared'
import { useSession } from './session'
import type { WorkerExecution } from './executionTypes'
function time(value: string) { return dateTime(value, 'full') }
export function ExecutionControls({ assignmentId, execution, onReload, allowRemaining = true, onUpdated, planContext = execution.active_plan_id && execution.active_plan_revision != null ? { id: execution.active_plan_id, revision: execution.active_plan_revision } : undefined }: { planContext?: { id: string; revision: number }; assignmentId: string; execution: WorkerExecution; allowRemaining?: boolean; onUpdated?: (value: WorkerExecution) => void; onReload: () => Promise<WorkerExecution & { current_plan?: { id: string; revision: number } }> }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const [snapshot, setSnapshot] = useState(execution)
  const [snapshotPlan, setSnapshotPlan] = useState(planContext)
  const [remaining, setRemaining] = useState('')
  const [remainingReason, setRemainingReason] = useState('')
  const [endReason, setEndReason] = useState('')
  const [stopReason, setStopReason] = useState('')
  const [stopConfirmed, setStopConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [blocked, setBlocked] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const stale = blocked || snapshot.revision !== execution.revision || snapshotPlan?.id !== planContext?.id || snapshotPlan?.revision !== planContext?.revision
  async function reload() { setBusy(true); setError(null); try { const updated = await onReload(); setSnapshot(updated); setSnapshotPlan(updated.current_plan ?? planContext); setBlocked(false); key.current = null } catch (value) { setError(value) } finally { setBusy(false) } }
  async function act(action: 'remaining' | 'end-visit' | 'stop-visit') {
    if (!session || busy || stale) return
    if (action === 'remaining' && (!/^\d+$/.test(remaining) || Number(remaining) < 1 || Number(remaining) > 480 || !remainingReason.trim())) return
    if (action === 'end-visit' && (!snapshot.can_end_visit || !snapshot.latest_approved_report || !endReason.trim())) return
    if (action === 'stop-visit' && (!snapshotPlan || !snapshot.can_stop_visit || !stopConfirmed || !stopReason.trim())) return
    const body = action === 'stop-visit' ? { expected_execution_revision: snapshot.revision, expected_active_plan_id: snapshotPlan!.id, expected_plan_revision: snapshotPlan!.revision, stop_current_visit: true, reason: stopReason.trim() } : action === 'remaining' ? { expected_execution_revision: snapshot.revision, remaining_min: Number(remaining), reason: remainingReason.trim() } : { expected_execution_revision: snapshot.revision, report_id: snapshot.latest_approved_report!.id, expected_report_revision: snapshot.latest_approved_report!.revision, reason: endReason.trim() }
    key.current = persistentKey(key.current, { action, body }); setBusy(true); setError(null)
    try { const updated = await request<WorkerExecution>(`/assignments/${assignmentId}/${action}`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); setSnapshot(updated); onUpdated?.(updated); await onReload() } catch (value) { if (value instanceof ApiError && value.status === 409) setBlocked(true); setError(value) } finally { setBusy(false) }
  }
  return <section className="execution-controls"><h4>{t('executionTitle')}</h4><p>{t(`executionPhase.${execution.phase}`)}</p>{stale && <p className="notice">{t('executionControlStale')}</p>}<button type="button" className="text-button" disabled={busy} onClick={() => void reload()}>{t('executionReloadControl')}</button>{snapshot.remaining && <div className="notice"><p>{t('remainingPrevious', { minutes: snapshot.remaining.minutes })}</p><p>{t('remainingConfirmedAt')}: {time(snapshot.remaining.confirmed_at)} (KST)</p><p>{t('remainingExpectedEnd')}: {time(snapshot.remaining.expected_end_at)} (KST)</p><p>{t('remainingExpires')}: {time(snapshot.remaining.expires_at)} (KST)</p><p>{t('remainingEvidence')}: {snapshot.remaining.reason}</p></div>}{allowRemaining && ['moving', 'working'].includes(snapshot.phase) && <fieldset disabled={busy}><legend>{t('remainingTitle')}</legend><p>{t('remainingHint')}</p><label>{t('remainingMinutes')}<input type="number" min="1" max="480" value={remaining} onChange={event => setRemaining(event.target.value)} /></label><label>{t('remainingReason')}<textarea rows={2} maxLength={1000} value={remainingReason} onChange={event => setRemainingReason(event.target.value)} /></label><button type="button" className="button secondary" disabled={stale || !/^\d+$/.test(remaining) || Number(remaining) < 1 || Number(remaining) > 480 || !remainingReason.trim()} onClick={() => void act('remaining')}>{t('remainingConfirm')}</button></fieldset>}{snapshot.can_end_visit && snapshot.latest_approved_report && <fieldset disabled={busy}><legend>{t('endVisitTitle')}</legend><p>{t('endVisitHint')}</p><p>{t('endVisitApprovedResult')}: {t(`result.${snapshot.latest_approved_report.result}`)}</p><label>{t('endVisitReason')}<textarea rows={2} maxLength={1000} value={endReason} onChange={event => setEndReason(event.target.value)} /></label><button type="button" className="button secondary" disabled={stale || !endReason.trim()} onClick={() => void act('end-visit')}>{t('endVisitConfirm')}</button></fieldset>}{planContext && snapshot.can_stop_visit && <details><summary>{t('executionStopTitle')}</summary><p className="notice">{t('serviceExecutionStopHint')}</p><label>{t('executionStopReason')}<textarea value={stopReason} maxLength={1000} disabled={busy} onChange={event => setStopReason(event.target.value)} /></label><label className="checkbox-label"><input type="checkbox" checked={stopConfirmed} disabled={busy} onChange={event => setStopConfirmed(event.target.checked)} />{t('serviceExecutionStopConfirm')}</label><button type="button" className="button secondary" disabled={busy || stale || !stopReason.trim() || !stopConfirmed} onClick={() => void act('stop-visit')}>{t('serviceExecutionStopAction')}</button></details>}{execution.phase === 'stopped' && <p className="notice">{t('executionStopped')}</p>}{execution.phase === 'visit_ended' && <p className="notice">{t(execution.latest_approved_report?.result === 'completed' ? 'executionCompletedVisit' : 'executionUnresolvedVisit')}</p>}{Boolean(error) && <ErrorNotice error={error} />}</section>
}
