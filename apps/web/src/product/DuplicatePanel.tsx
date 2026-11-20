import { safetyUrgencies, urgencyLabel } from './safetyDisplay'
import type { OrphanTicket } from './OrphanedReports'
import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { useTranslation } from 'react-i18next'
import { ApiError, api, persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice, Loading, StatusBadge } from './shared'
import { displayLocation } from './locationDisplay'
import type { DuplicateCandidate, DuplicateCandidates, MergeHistory, MergeTicket } from './duplicateTypes'
import type { Ticket } from './types'

export function DuplicatePanel({ ticket, onUpdate }: { ticket: Ticket; onUpdate: () => void }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const current = ticket as MergeTicket
  if (session?.user.role !== 'dispatcher' || !current.merge_version) return null
  const active = current.merge_history?.find(item => item.active && item.source_id === ticket.id)
  const related = current.merge_history?.filter(item => item.active && item.target_id === ticket.id) ?? []
  return <section className="content-card duplicate-panel"><h2>{t('duplicateTitle')}</h2><p className="field-hint">{t('duplicateRulesHint')}</p>{active ? <ActiveMerge key={active.id} ticket={current} relation={active} onUpdate={onUpdate} /> : ['received', 'confirmed', 'assigned', 'in_progress'].includes(ticket.status) && <CandidateLoader ticket={current} onUpdate={onUpdate} />}{(current.merge_history?.some(item => !item.active && item.source_id === ticket.id && item.reassignment_required)) && <p className="notice">{t('duplicateReassign')}</p>}{related.length > 0 && <div><h3>{t('duplicateLinkedSources')}</h3><ul>{related.map(item => <li key={item.id}><Link to={`/admin/t/${item.source_id}`}>{t('duplicateSourceLink')}</Link><p>{t('duplicateLinkedFloor', { level: item.linked_floor === 3 ? '적중 없음' : urgencyLabel(item.linked_floor) })}</p><details><summary>{t('processingDetails')}</summary><p>{t('duplicateLinkedSourceAuto')}: {urgencyLabel(item.source_automatic_urgency)}</p><p>{t('duplicateLinkedSourceEffective')}: {urgencyLabel(item.source_effective_urgency)}</p><p>{t('rulesVersion')}: {item.source_rules_version ?? t('unknown')}</p></details></li>)}</ul></div>}</section>
}
function CandidateLoader({ ticket, onUpdate }: { ticket: MergeTicket; onUpdate: () => void }) {
  const { cacheScope } = useSession()
  const query = useQuery({ queryKey: ['duplicates', cacheScope, ticket.id], queryFn: () => request<DuplicateCandidates>(`/tickets/${ticket.id}/duplicates`), refetchOnWindowFocus: false, retry: false })
  if (query.isPending) return <Loading />
  if (query.isError) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  return <CandidateReview key={ticket.id} ticket={ticket} initial={query.data} onUpdate={onUpdate} />
}
function CompareTicket({ ticket, title }: { ticket: MergeTicket; title: string }) {
  const { t } = useTranslation('product')
  const values = ticket.safety ? safetyUrgencies(ticket.safety) : null
  return <article className="duplicate-comparison"><h3>{title} · {ticket.public_no}</h3><StatusBadge status={ticket.status} /><p className="ticket-body">{ticket.text}</p><dl><dt>{t('location')}</dt><dd>{displayLocation(ticket) || t('locationUnknown')}</dd><dt>{t('category')}</dt><dd>{ticket.category_label || t('unknown')}</dd><dt>{t('automatic')}</dt><dd>{urgencyLabel(values?.ownAutomatic)}</dd><dt>{t('effective')}</dt><dd>{urgencyLabel(values?.current)}</dd></dl><p>{t(ticket.assignment_id ? 'duplicateAssigned' : 'duplicateNotAssigned')}</p><p>{t((ticket.latest_report && !ticket.latest_report.reviewed) || ((ticket as OrphanTicket).orphaned_reports ?? []).some(report => report.analysis_status !== 'dismissed') ? 'duplicatePendingReport' : 'duplicateNoPendingReport')}</p><Link to={`/admin/t/${ticket.id}`}>{t('duplicateDetailLink')}</Link></article>
}
function CandidateReview({ ticket, initial, onUpdate }: { ticket: MergeTicket; initial: DuplicateCandidates; onUpdate: () => void }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const [snapshot, setSnapshot] = useState(ticket)
  const [candidates, setCandidates] = useState(initial)
  const [selected, setSelected] = useState<DuplicateCandidate | null>(null)
  const [target, setTarget] = useState<MergeTicket | null>(null)
  const [reason, setReason] = useState('')
  const [stopWork, setStopWork] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [conflict, setConflict] = useState(false)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const reportPending = (value: MergeTicket) => Boolean(value.latest_report && !value.latest_report.reviewed) || ((value as OrphanTicket).orphaned_reports ?? []).some(report => report.analysis_status !== 'dismissed')
  const stale = candidates.source_version !== snapshot.merge_version || candidates.source_version !== ticket.merge_version || reportPending(snapshot) !== reportPending(ticket) || Boolean(target && selected && target.merge_version !== selected.merge_version)
  const working = ['assigned', 'in_progress'].includes(snapshot.status)
  const hasOrphans = ((snapshot as OrphanTicket).orphaned_reports ?? []).some(report => report.analysis_status !== 'dismissed')
  const pending = Boolean(snapshot.latest_report && !snapshot.latest_report.reviewed) || hasOrphans
  async function select(candidate: DuplicateCandidate) { setBusy(true); setError(null); try { const latest = await api.ticket(candidate.target_id); setSelected(candidate); setTarget(latest as MergeTicket); setStopWork(false) } catch (value) { setError(value) } finally { setBusy(false) } }
  async function reload() { setBusy(true); setError(null); try { const latest = await api.ticket(ticket.id); const list = await request<DuplicateCandidates>(`/tickets/${ticket.id}/duplicates`); setSnapshot(latest as MergeTicket); setCandidates(list); setTarget(null); setSelected(null); setStopWork(false); setConflict(false); key.current = null; onUpdate() } catch (value) { setError(value) } finally { setBusy(false) } }
  async function act(action: 'merge' | 'duplicate-dismiss') {
    if (!session || !selected || !target || stale || conflict || busy || !reason.trim()) return
    if (action === 'merge' && (pending || (working && !stopWork))) return
    const body = { target_id: selected.target_id, expected_source_version: candidates.source_version, expected_target_version: selected.merge_version, ...(action === 'merge' ? { stop_source_work: stopWork } : {}), reason: reason.trim() }
    key.current = persistentKey(key.current, { action, body }); setBusy(true); setError(null)
    try { await request<Ticket>(`/tickets/${ticket.id}/${action}`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); await reload(); onUpdate() } catch (value) { setError(value); if (value instanceof ApiError && value.status === 409 && ['merge_version_conflict', 'merge_candidate_changed'].includes(value.code)) setConflict(true) } finally { setBusy(false) }
  }
  return <><p>{t('duplicateSourceHint')}</p>{(stale || conflict) && <p className="notice" role="status">{t('duplicateStale')}</p>}{(stale || conflict) && <button className="text-button" type="button" disabled={busy} onClick={() => void reload()}>{t('duplicateReload')}</button>}{!candidates.candidates.length ? <p>{t('duplicateNone')}</p> : <div className="location-candidates">{candidates.candidates.map(candidate => <button type="button" className="button secondary" key={candidate.target_id} disabled={busy} onClick={() => void select(candidate)}>{candidate.public_no} · {candidate.location_label}<small>{t(`duplicateEvidence.${candidate.evidence}`, { defaultValue: t('duplicateEvidenceUnknown') })}</small></button>)}</div>}{target && selected && <><div className="duplicate-compare-grid"><CompareTicket ticket={snapshot} title={t('duplicateSource')} /><CompareTicket ticket={target} title={t('duplicateTarget')} /></div><fieldset disabled={busy}><label>{t('duplicateReason')}<textarea rows={3} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label>{working && <label className="checkbox-label"><input type="checkbox" checked={stopWork} onChange={event => setStopWork(event.target.checked)} />{t('duplicateStopWork')}</label>}{pending && <div className="notice"><p>{t('duplicatePendingBlock')}</p>{hasOrphans ? <a href="#orphaned-reports">{t('orphanResolveLink')}</a> : <Link to={`/admin/t/${ticket.id}`}>{t('duplicateResolveReport')}</Link>}</div>}<div className="stack-buttons"><button className="button primary" type="button" disabled={stale || conflict || !reason.trim() || pending || (working && !stopWork)} onClick={() => void act('merge')}>{t('duplicateApprove')}</button><button className="button secondary" type="button" disabled={stale || conflict || !reason.trim()} onClick={() => void act('duplicate-dismiss')}>{t('duplicateDismiss')}</button></div></fieldset></>}{Boolean(error) && <ErrorNotice error={error} />}</>
}
function ActiveMerge({ ticket, relation, onUpdate }: { ticket: MergeTicket; relation: MergeHistory; onUpdate: () => void }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const [snapshot, setSnapshot] = useState(ticket)
  const [target, setTarget] = useState<MergeTicket | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const stale = ticket.merge_version !== snapshot.merge_version
  async function reload() { setBusy(true); setError(null); try { const source = await api.ticket(ticket.id); const destination = await api.ticket(relation.target_id); setSnapshot(source as MergeTicket); setTarget(destination as MergeTicket); key.current = null; onUpdate() } catch (value) { setError(value) } finally { setBusy(false) } }
  async function unmerge() { if (!session || !target || stale || busy || !reason.trim()) return; const body = { expected_source_version: snapshot.merge_version, expected_target_version: target.merge_version, reason: reason.trim() }; key.current = persistentKey(key.current, body); setBusy(true); setError(null); try { await request<Ticket>(`/tickets/${ticket.id}/unmerge`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); onUpdate() } catch (value) { setError(value) } finally { setBusy(false) } }
  return <><p>{t('duplicateActive')}</p><Link to={`/admin/t/${relation.target_id}`}>{t('duplicateTargetLink')}</Link><p className="field-hint">{t('duplicateUnmergeHint')}</p>{stale && <p className="notice">{t('duplicateStale')}</p>}<button type="button" className="text-button" disabled={busy} onClick={() => void reload()}>{t('duplicateLoadUnmerge')}</button>{target && <><p>{t('duplicateTargetState')}: <StatusBadge status={target.status} /></p><label>{t('duplicateUnmergeReason')}<textarea rows={2} maxLength={1000} disabled={busy} value={reason} onChange={event => setReason(event.target.value)} /></label><button type="button" className="button secondary" disabled={busy || stale || !reason.trim()} onClick={() => void unmerge()}>{t('duplicateUnmerge')}</button></>}{Boolean(error) && <ErrorNotice error={error} />}</>
}
