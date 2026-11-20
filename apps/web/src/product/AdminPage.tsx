import { AIFollowup } from './AIFollowup'
import { InformationRequests } from './InformationRequests'
import { AssignmentPlacePanel } from './AssignmentPlacePanel'
import { locationDraftScope, type LocationDraftControl, type PlaceDraft } from './assignmentLocation'
import './adminDetail.css'
import { AdminAssignment } from './AdminAssignment'
import { queueAction } from './AdminQueue'
import { needsSafetyWarning } from './safetyDisplay'
import { AiDraftCard } from './AiDraftCard'
import { ForegroundRefreshStatus, useForegroundRefresh } from './useForegroundRefresh'
import { MailRecovery } from './MailRecovery'
import { StaffExecution, StaffVisitSummary } from './StaffExecution'
import { useRuntime } from './runtime'
import { OrphanedReports } from './OrphanedReports'
import { DuplicatePanel } from './DuplicatePanel'
import { LinkedSafetyReview } from './LinkedSafety'
import { ReportApproval } from './ReportWorkflow'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { LocationPanel } from './LocationPanel'
import { ServiceLocation } from './ServiceLocation'
import { SafetyPanel } from './SafetyPanel'
import { ApiError, api, persistentKey, request } from './api'
import type { Assigned, Catalog, Ticket } from './types'
import { legacyCategories } from './ticketCategories'
import type { PlanningCapabilities } from './planningTypes'
import { useSession } from './session'
import { ErrorNotice, Loading, StatusBadge, TicketContent, TicketDates, TicketReceiptReference } from './shared'

export function adminTicketTitle(ticket: Ticket): string {
  return (!ticket.ai_draft?.stale && ticket.ai_draft?.summary?.trim()) || ticket.text
}

export function OtherActions({ attention, detailsRef, children }: { attention: boolean; detailsRef: React.RefObject<HTMLDetailsElement | null>; children: React.ReactNode }) {
  useEffect(() => { if (attention && detailsRef.current) detailsRef.current.open = true }, [attention, detailsRef])
  return <details ref={detailsRef} onToggle={event => { if (attention && !event.currentTarget.open) event.currentTarget.open = true }}><summary>다른 작업</summary>{children}</details>
}

export function AdminPage() {
  const { cacheScope } = useSession()
  const { id = '' } = useParams()
  return <ScopedAdminPage key={`${cacheScope}:${id}`} />
}
function ScopedAdminPage() {
  const runtime = useRuntime()
  const { id = '' } = useParams()
  const { state } = useLocation()
  const queueSearch = typeof state?.queueSearch === 'string' && state.queueSearch.startsWith('?') ? state.queueSearch : ''
  const { t } = useTranslation('product')
  const { session, cacheScope, loading, switching, manualAssignments } = useSession()
  useSyncExternalStore(manualAssignments.subscribe, manualAssignments.snapshot)
  const pendingManual = Boolean(manualAssignments.get(id))
  const client = useQueryClient()
  const [link, setLink] = useState('')
  const [workerAccountNotice, setWorkerAccountNotice] = useState(false)
  const [message, setMessage] = useState('')
  const [pendingAtomic, setPendingAtomic] = useState(false)
  const [placeDraft, setPlaceDraft] = useState<PlaceDraft | null>(null)
  const [reasonDraft, setReasonDraft] = useState<{ scope: string; value: string } | null>(null)
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const prepared = Boolean(id && session && !loading && !switching)
  const query = useQuery({ queryKey: ['ticket', cacheScope, id], queryFn: ({ signal }) => api.ticket(id, signal), enabled: prepared, retry: false, refetchOnWindowFocus: false })
  const refresh = useForegroundRefresh(query, { enabled: prepared })
  const catalog = useQuery({ queryKey: ['catalog', cacheScope], queryFn: ({ signal }) => api.catalog(signal) })
  const planning = useQuery({ queryKey: ['planning-capabilities', cacheScope], queryFn: ({ signal }) => request<PlanningCapabilities>('/planning/capabilities', { signal }), retry: false })
  if (query.isPending || catalog.isPending) return <Loading />
  if (query.isError && !query.data) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if (catalog.isError) return <ErrorNotice error={catalog.error} retry={() => void catalog.refetch()} />
  async function refreshed(messageKey: string, assigned?: Assigned, alreadyRefreshed = false) {
    if (!active.current) return
    setMessage(messageKey)
    if (assigned) {
      setLink(assigned.worker_token ? `/w#token=${encodeURIComponent(assigned.worker_token)}` : '')
      setWorkerAccountNotice(!assigned.worker_token && messageKey !== 'assignmentRecovered')
    }
    if (alreadyRefreshed) return
    await client.invalidateQueries({ queryKey: ['tickets', cacheScope] })
    await client.invalidateQueries({ queryKey: ['staff-execution', cacheScope] })
    if (active.current) await query.refetch()
  }
  const hasReview = pendingAtomic || pendingManual || ['confirmed', 'assigned', 'in_progress'].includes(query.data.status) || (query.data.status === 'received' && runtime.mode !== 'prototype')
  const activeAssignmentId = ['assigned', 'in_progress'].includes(query.data.status) ? query.data.assignment_id : null
  const latestTicketEvent = query.data.timeline?.at(-1)?.id
  const reporterIdentity = query.data.reporter_identity
  const actionGroup = queueAction(query.data).group
  const leadReport = !pendingAtomic && actionGroup === 'report'
  const leadExecution = !pendingAtomic && actionGroup === 'followup' && query.data.visit_followup === 'end_visit'
  const placeReview = !!catalog.data.catalog_revision && runtime.mode !== 'prototype'
    && session?.user.role === 'dispatcher'
    && (query.data.status === 'received' || (runtime.mode === 'service' && !!runtime.staff_password_auth
      && query.data.status === 'confirmed' && !query.data.assignment_id))
    && query.data.location?.source_type !== 'service'
  const locationDraft = { value: placeDraft, onChange: setPlaceDraft }
  const reasonScope = locationDraftScope(query.data, catalog.data.catalog_revision)
  const reason = reasonDraft?.scope === reasonScope ? reasonDraft.value : ''
  const setReason = (value: string) => setReasonDraft({ scope: reasonScope, value })
  const reportCard = query.data.latest_report && session && <ReportApproval key={query.data.latest_report.id} ticket={query.data} csrf={session.csrf_token} onDone={correction => refreshed(correction ? 'correctionRequested' : 'approved')} onLoadLatest={async () => { const latest = await query.refetch(); if (latest.isError || !latest.data) throw latest.error ?? new Error('refresh_failed'); return latest.data }} />
  const executionCard = query.data.assignment_id && <StaffExecution key={`${query.data.assignment_id}:${query.data.latest_report?.id ?? ""}:${query.data.latest_report?.reviewed ?? false}`} assignmentId={query.data.assignment_id} />
  return <><Link className="back-link" to={{ pathname: '/admin/queue', search: queueSearch }}>← {t('back')}</Link><div className="page-heading ticket-heading"><div><p className="product-eyebrow">{t('dispatcher')} · {t('details')}</p><h1>{adminTicketTitle(query.data)}</h1><TicketReceiptReference value={query.data.public_no} /></div><StatusBadge status={query.data.status} /></div><TicketDates ticket={query.data} />{runtime.mode === 'service' && session?.user.role === 'dispatcher' && reporterIdentity && <dl className="ticket-facts"><div><dt>신고자 ID</dt><dd>{reporterIdentity.id}</dd></div><div><dt>이메일</dt><dd>{reporterIdentity.email}</dd></div></dl>}<ForegroundRefreshStatus refresh={refresh} />{message && <div className="notice notice-success" role="status">{message === 'assignmentRecovered' ? (runtime.mode === 'service' ? '배정 저장을 확인했어요. 작업자는 직원 로그인 후 내 작업에서 확인할 수 있어요.' : '배정 저장을 확인했어요. 이번 확인 응답에는 작업 링크가 포함되지 않아요.') : t(message)}</div>}{workerAccountNotice && <section className="notice worker-link-notice"><h2>작업자 안내</h2><p>배정된 작업자는 직원 로그인 후 내 작업에서 확인할 수 있어요.</p></section>}{link && runtime.mode !== 'service' && <section className="notice worker-link-notice"><h2>{t('workerLink')}</h2><p>{t('workerLinkHint')}</p><a className="button primary" href={link} target="_blank" rel="noreferrer">{t('openWorker')} ↗</a></section>}{needsSafetyWarning(query.data) && <p className="notice notice-error">{t('emergency')}</p>}<div className={`detail-layout admin-layout${hasReview ? " has-review" : ""}${activeAssignmentId ? " has-visit-summary" : ""}`}>{activeAssignmentId && <StaffVisitSummary key={`${activeAssignmentId}:${latestTicketEvent ?? query.data.updated_at}`} ticket={query.data} assignmentId={activeAssignmentId} />}<div className="admin-primary"><AiDraftCard ticket={query.data} catalog={catalog.data} />{(leadReport || leadExecution) && <fieldset disabled={pendingAtomic || pendingManual}>{leadReport ? reportCard : executionCard}</fieldset>}{placeReview && <AssignmentPlacePanel ticket={query.data} catalogRevision={catalog.data.catalog_revision} draft={locationDraft} reason={reason} setReason={setReason} disabled={pendingAtomic || pendingManual} />}{runtime.mode !== 'public_demo' && <fieldset className="admin-question-slot" disabled={pendingAtomic || pendingManual}><InformationRequests key={`info:${cacheScope}:${id}`} ticket={query.data} staff onUpdate={() => void query.refetch()} /></fieldset>}<AIFollowup key={`fq:${cacheScope}:${id}`} ticket={query.data} staff onUpdate={() => void query.refetch()} /></div><div className="admin-review"><AdminActions key={`${cacheScope}:${query.data.id}`} ticket={query.data} catalog={catalog.data} planning={planning.data} locationDraft={locationDraft} reason={reason} setReason={setReason} pendingAtomic={pendingAtomic} setPendingAtomic={setPendingAtomic} onDone={refreshed} onLoadLatest={async () => { const [result, latestCatalog] = await Promise.all([query.refetch(), catalog.refetch()]); if (!result.data || result.isError || !latestCatalog.data || latestCatalog.isError) throw result.error ?? latestCatalog.error ?? new Error('refresh_failed'); return { ticket: result.data, catalog: latestCatalog.data } }} /></div><div className="admin-extras"><fieldset className="admin-details-body" disabled={pendingAtomic || pendingManual}><SafetyPanel ticket={query.data} onUpdate={() => { void query.refetch(); void client.invalidateQueries({ queryKey: ['tickets'] }) }} /><LinkedSafetyReview key={query.data.id} ticket={query.data} onUpdate={() => void query.refetch()} /></fieldset><details className="admin-detail-disclosure" id="service-location-panel"><summary>추가 확인과 처리 기록</summary><fieldset className="admin-details-body" disabled={pendingAtomic || pendingManual}>{!placeReview && <LocationPanel ticket={query.data} staff readOnly={runtime.mode === 'public_demo'} onUpdate={() => void query.refetch()} />}{((runtime.mode === 'service' || runtime.mode === 'public_demo' || (runtime.mode === 'demo' && catalog.data.catalog_revision != null && catalog.data.catalog_revision > 0)) && session?.user.role === 'dispatcher' && ['received', 'confirmed'].includes(query.data.status)) && <ServiceLocation key={`service-location-${query.data.id}`} disabled={pendingAtomic || pendingManual} catalogRevision={catalog.data.catalog_revision} ticket={query.data} onUpdate={() => void query.refetch()} onLoadLatest={async () => { const latest = await query.refetch(); if (latest.isError || !latest.data) throw latest.error ?? new Error('refresh_failed'); return latest.data }} />}<OrphanedReports ticket={query.data} onUpdate={() => void query.refetch()} onLoadLatest={async () => { const latest = await query.refetch(); if (latest.isError || !latest.data) throw latest.error ?? new Error('refresh_failed'); return latest.data }} /><DuplicatePanel ticket={query.data} onUpdate={() => void query.refetch()} /><TicketContent ticket={query.data} onRefresh={() => void query.refetch()} refreshing={query.isFetching} /></fieldset></details><aside className="admin-followup"><fieldset className="admin-details-body" disabled={pendingAtomic || pendingManual}>{runtime.mode === 'service' && session && (query.data.approved_reports ?? []).map(report => <MailRecovery key={report.id} ticketId={query.data.id} reportId={report.id} reportLabel={t(`result.${report.confirmed_result ?? report.result}`)} csrf={session.csrf_token} />)}{!leadExecution && executionCard}{!leadReport && reportCard}</fieldset></aside></div></div></>
}
export function assignmentSectionVisible(status: Ticket['status'], atomic: boolean, pendingManual: boolean): boolean {
  return atomic || pendingManual || ['confirmed', 'assigned', 'in_progress'].includes(status)
}
function AdminActions({ ticket, catalog, planning, locationDraft, reason, setReason, pendingAtomic, setPendingAtomic, onDone, onLoadLatest }: { ticket: Ticket; catalog: Catalog; planning?: PlanningCapabilities; locationDraft: LocationDraftControl; reason: string; setReason: (value: string) => void; pendingAtomic: boolean; setPendingAtomic: (value: boolean) => void; onLoadLatest: () => Promise<{ ticket: Ticket; catalog: Catalog }>; onDone: (message: string, assigned?: Assigned, alreadyRefreshed?: boolean) => Promise<void> }) {
  const { t } = useTranslation('product')
  const { session, manualAssignments } = useSession()
  useSyncExternalStore(manualAssignments.subscribe, manualAssignments.snapshot)
  const pendingManual = Boolean(manualAssignments.get(ticket.id))
  const runtime = useRuntime()
  const [category, setCategory] = useState(ticket.category_code ?? '')
  const [location, setLocation] = useState(ticket.location_text)
  const [editVersion, setEditVersion] = useState(ticket.location?.version ?? 0)
  const [editCategory, setEditCategory] = useState(ticket.category_code)
  const [editCatalogRevision, setEditCatalogRevision] = useState(catalog.catalog_revision ?? null)
  const staleEdit = editVersion !== (ticket.location?.version ?? 0)
  const catalogStale = catalog.catalog_revision !== null && catalog.catalog_revision !== undefined && catalog.catalog_revision > 0 && ticket.catalog_revision !== catalog.catalog_revision
  async function loadLatest() {
    setBusy(true); setError(null)
    try { const latest = await onLoadLatest(); setEditVersion(latest.ticket.location?.version ?? 0); setEditCategory(latest.ticket.category_code); setEditCatalogRevision(latest.catalog.catalog_revision ?? null); setEditConflict(false); key.current = null } catch (value) { setError(value) } finally { setBusy(false) }
  }
  const atomic = !pendingManual && (pendingAtomic || (ticket.status === 'received' && runtime.mode !== 'prototype') || (ticket.status === 'confirmed' && !ticket.assignment_id && runtime.mode === 'service' && runtime.staff_password_auth))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [editConflict, setEditConflict] = useState(false)
  const [assignmentAttention, setAssignmentAttention] = useState(false)
  const assignmentDetails = useRef<HTMLDetailsElement>(null)
  const assignmentRequired = !ticket.assignment_id || pendingAtomic || pendingManual || assignmentAttention || Boolean(error) || busy || Boolean(reason.trim())
  useEffect(() => { if (assignmentRequired && assignmentDetails.current) assignmentDetails.current.open = true }, [assignmentRequired])
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const canEdit = ['received', 'confirmed'].includes(ticket.status)
  const assignmentVisible = assignmentSectionVisible(ticket.status, atomic, pendingManual)
  const editCategories = catalog.categories.filter(item => !legacyCategories.has(item.code) || item.code === ticket.category_code)
  async function action(path: string, body: unknown, message: string, method = 'POST') {
    if (!session || busy || pendingAtomic || pendingManual) return
    setBusy(true); setError(null)
    try { key.current = persistentKey(key.current, { path, body, method }); const updated = await request<Ticket>(`/tickets/${ticket.id}${path}`, { method, body, key: key.current.key }, { csrf: session.csrf_token }); if (method === 'PATCH') { setCategory(updated.category_code ?? ''); setLocation(updated.location_text); setEditVersion(updated.location?.version ?? 0); setEditCategory(updated.category_code); key.current = null } await onDone(message) } catch (value) { setError(value); if (method === 'PATCH' && value instanceof ApiError && value.status === 409) setEditConflict(true) } finally { setBusy(false) }
  }
  return <div className="action-stack">
    <details hidden={!assignmentVisible} ref={assignmentDetails} onToggle={event => { if (assignmentRequired && !event.currentTarget.open) event.currentTarget.open = true }}><summary>{ticket.assignment_id ? '다른 작업 · 다시 배정' : '확인하고 배정'}</summary>{!atomic && assignmentVisible && <><label htmlFor="action-reason">{t('reason')}</label><textarea id="action-reason" value={reason} onChange={event => setReason(event.target.value)} placeholder={t('reasonPlaceholder')} rows={3} maxLength={1000} disabled={busy || pendingManual} /></>}
    <div id={atomic ? 'category-review' : undefined}><AdminAssignment onAttention={setAssignmentAttention} ticket={ticket} catalog={catalog} planning={planning} locationDraft={locationDraft} reason={reason} onReason={atomic ? setReason : undefined} onPending={setPendingAtomic} externalBusy={busy} onConfirm={atomic && ticket.status === 'received' ? () => void action('/confirm', { reason: reason.trim() }, 'confirmed') : undefined} onDone={onDone} onLoadLatest={onLoadLatest} /></div></details>
    <details id="category-edit-panel"><summary>내용을 따로 수정하거나 확인</summary><section className="content-card action-card"><h2>{t('reviewSection')}</h2><p className="field-hint">{t('reviewHint')}</p>{catalogStale && <p className="notice">이 신고는 이전 학교 자료로 분류됐어요. 현재 학교 자료에서 분류를 다시 확인하고 사유와 함께 저장하기 전에는 새 배정 추천에서 제외돼요. 수동 배정은 계속할 수 있어요.</p>}{staleEdit && !editConflict && <div className="notice" role="status"><p>{t('staleEdit')} 입력한 내용은 유지돼요.</p><button type="button" className="text-button" disabled={busy} onClick={() => void loadLatest()}>{t('loadLatestEdit')}</button></div>}<fieldset disabled={busy || pendingAtomic || pendingManual || !canEdit}>{!atomic && !assignmentVisible && canEdit && <><label htmlFor="action-reason">{t('reason')}</label><textarea id="action-reason" value={reason} onChange={event => setReason(event.target.value)} placeholder={t('reasonPlaceholder')} rows={3} maxLength={1000} /></>}<label htmlFor="category">{t('category')}</label><select id="category" value={category} onChange={event => setCategory(event.target.value)}><option value="">{t('unknown')}</option>{[...new Set(editCategories.map(item => item.domain))].map(domain => <optgroup key={domain} label={t(`categoryGroup.${domain}`, { defaultValue: '분야 확인 필요' })}>{editCategories.filter(item => item.domain === domain).map(item => <option key={item.code} value={item.code}>{item.label}</option>)}</optgroup>)}</select><label htmlFor="edit-location">{t('location')}</label><input id="edit-location" value={location} maxLength={300} onChange={event => setLocation(event.target.value)} /></fieldset><div className="stack-buttons">{canEdit && <button className="button secondary" disabled={busy || pendingAtomic || pendingManual || staleEdit || editConflict || !reason.trim() || !category} onClick={() => void action('', { expected_location_version: editVersion, expected_category_code: editCategory, expected_catalog_revision: editCatalogRevision, category_code: category, location_text: location.trim(), reason: reason.trim() }, 'saved', 'PATCH')}>{t(busy ? 'submittingAction' : 'save')}</button>}{ticket.status === 'received' && !atomic && <button className="button primary" disabled={busy || pendingManual || !reason.trim()} onClick={() => void action('/confirm', { reason: reason.trim() }, 'confirmed')}>{runtime.mode === 'prototype' ? t(busy ? 'submittingAction' : 'confirm') : '접수만 확인'}</button>}</div></section></details>
    {Boolean(error) && <ErrorNotice error={error} onReload={editConflict && !busy ? () => void loadLatest() : undefined} />}
  </div>
}
