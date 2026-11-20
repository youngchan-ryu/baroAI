import './workerList.css'
import { CampusMap, Directions } from './CampusMap'
import type { components } from '../api/schema'
import { useMemoryDraft } from './memoryDraft'
import { useWorkerField, WorkerFormDraft, type AssignmentDrafts } from './workerDraft'
import { useRuntime } from './runtime'
import { LinkedSafetySummary } from './LinkedSafety'
import { WorkerSafety } from './WorkerSafety'
import { dateTime, relativeTime } from './dateTime'
import { ReportSnapshot, WorkerReportConfirmation } from './ReportWorkflow'
import { workerConfirmed, type Report } from './reportTypes'
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { Link, useParams } from 'react-router'
import { useSession } from './session'
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, ApiError, persistentKey, request, type Auth } from './api'
import type { Assignment, Result, Ticket } from './types'
import { ErrorNotice, Loading, Photos, StatusBadge, TicketReceiptReference } from './shared'
import { UploadField, useUploads } from './UploadField'
import { WorkerSettings } from './WorkerSettings'
import { useForegroundRefresh } from './useForegroundRefresh'

function awaitsWorkerConfirmation(report: Report | null | undefined) {
  return Boolean(report && !report.reviewed && !report.correction_requested_at && !workerConfirmed(report))
}

const pendingReport = (assignment: Assignment | undefined) => {
  const report = assignment?.ticket.latest_report
  return ['working', 'visit_ended'].includes(assignment?.execution?.phase ?? '') && Boolean(report && !report.reviewed && !workerConfirmed(report as Report) && ['queued', 'running', 'retry_wait'].includes(report.analysis_status ?? ''))
}

export function WorkerPage() {
  const { t } = useTranslation('product')
  const { loading, switching } = useSession()
  const runtime = useRuntime()
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '')
  useEffect(() => { if (window.location.hash) window.history.replaceState(null, '', window.location.pathname + window.location.search) }, [])
  if (runtime.mode === 'service') return <section className="empty-state"><h1>작업자 업무 확인</h1><p>직원 계정으로 로그인해 내 작업을 확인해 주세요.</p><Link className="button primary" to="/work">내 작업으로 이동</Link></section>
  if (!token) return <section className="empty-state"><h1>{t('workerMissing')}</h1><p>{t('workerMissingHint')}</p></section>
  if (loading || switching) return <Loading />
  return <WorkerBearerPage token={token} />
}
function WorkerBearerPage({ token }: { token: string }) {
  const [accessEnded, setAccessEnded] = useState(false)
  const [confirmationPending, setConfirmationPending] = useState(false)
  const [cacheKey] = useState(() => crypto.randomUUID())
  const [autoRead, setAutoRead] = useState(false)
  const query = useQuery({ queryKey: ['worker-assignment', cacheKey], queryFn: ({ signal }) => api.assignment(token, signal), gcTime: 0, retry: false, refetchOnWindowFocus: false })
  const { refetch } = query
  const automaticRefetch = useCallback(async (...args: Parameters<typeof refetch>) => { setAutoRead(true); const result = await refetch(...args); if (!result.isError) setAutoRead(false); return result }, [refetch])
  const manualRefetch = async () => { const recovering = autoRead && query.isError; if (!recovering) setAutoRead(false); const result = await query.refetch(); if (!result.isError) setAutoRead(false); return result }
  useForegroundRefresh({ ...query, refetch: automaticRefetch }, { enabled: Boolean(!accessEnded && !query.isError && !confirmationPending && pendingReport(query.data)) })
  if (accessEnded || (query.error instanceof ApiError && query.error.status === 401 && query.error.code === 'invalid_worker_token')) return <section className="content-card"><p className="notice">작업 링크가 만료되었거나 철회됐어요. 담당자에게 새 링크를 요청해 주세요.</p></section>
  if (query.isPending) return <Loading />
  if (query.isError && (!query.data || !autoRead || (query.error instanceof ApiError && [401, 403, 404, 409].includes(query.error.status)))) return <ErrorNotice error={query.error} retry={() => void manualRefetch()} />
  return <WorkerAssignmentScreen onAccessEnded={() => setAccessEnded(true)} onConfirmationPending={setConfirmationPending} assignment={query.data!} refreshError={query.isError} auth={{ bearer: token }} fetching={query.isFetching} isCurrent={() => true} reload={async () => { const latest = await manualRefetch(); if (latest.isError || !latest.data) throw latest.error ?? new Error('assignment_unavailable'); return latest.data }} />
}

type WorkList = Omit<components['schemas']['WorkerAssignmentList'], 'items'> & { items: (components['schemas']['WorkerAssignmentSummary'] & { confirmed_location_label?: string | null; map_latitude?: number | null; map_longitude?: number | null; plan_group?: string | null; plan_sequence?: number | null })[] }
async function loadWorkList(offset: number, signal: AbortSignal): Promise<WorkList & { legacyOrder?: boolean }> {
  try {
    return await request<WorkList>(`/worker/assignments?limit=20&offset=${offset}&sort=work_order`, { signal })
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 422) throw error
    const result = await request<WorkList>(`/worker/assignments?limit=20&offset=${offset}&sort=urgent`, { signal })
    return { ...result, legacyOrder: true }
  }
}
export function WorkListPage() {
  const { t } = useTranslation('product')
  const { cacheScope } = useSession()
  const [offset, setOffset] = useState(0)
  const query = useQuery({ queryKey: ['my-work', cacheScope, offset], queryFn: ({ signal }) => loadWorkList(offset, signal) })
  const details = useQueries({ queries: (query.data?.items ?? []).map(item => ({ queryKey: ['my-work-detail', cacheScope, item.assignment_id], queryFn: ({ signal }: { signal: AbortSignal }) => request<Assignment>(`/worker/assignments/${encodeURIComponent(item.assignment_id)}`, { signal }), staleTime: 60_000, retry: false, refetchOnWindowFocus: false })) })
  const firstVisit = offset === 0 ? query.data?.items.find(item => ['moving', 'working', 'queued'].includes(item.execution.phase)) : undefined
  return <><div className="page-heading heading-with-action">
    <div><h1>{t('myWorkTitle')}</h1><p>{t('myWorkHint')}</p></div>
    <button type="button" className="button secondary" disabled={query.isFetching} onClick={() => void query.refetch()}>{t('refresh')}</button>
  </div>{firstVisit && <div className="form-actions"><Link className="button primary" to={`/work/${firstVisit.assignment_id}`}>
    {firstVisit.execution.phase === 'queued' ? '다음 방문 열기' : '진행 중인 작업 열기'}
  </Link></div>}{query.isPending ? <Loading /> : query.isError ? <ErrorNotice error={query.error} retry={() => void query.refetch()} /> : <>{!query.data.items.length && <section className="empty-state"><h2>{t('myWorkEmpty')}</h2></section>}<p className="field-hint">{query.data.legacyOrder ? '긴급도순으로 표시해요.' : '진행 중인 작업을 먼저, 승인된 방문 순서와 긴급도를 반영해 표시해요. 지도는 현재 페이지에서 아직 방문할 확정 위치를 보여줘요.'}</p><CampusMap points={query.data.items.flatMap(item => item.map_latitude != null && item.map_longitude != null && !['visit_ended', 'stopped'].includes(item.execution.phase) ? [{ latitude: item.map_latitude, longitude: item.map_longitude, label: item.confirmed_location_label || '확정 위치', group: item.plan_group, sequence: item.plan_sequence }] : [])} /><div className="ticket-list worker-list">{query.data.items.map((item, index) => (
  <Link className="ticket-card worker-list-card" key={item.assignment_id} to={`/work/${item.assignment_id}`}>
    <h2 className="worker-list-location">{details[index]?.isError ? '신고 내용 확인하기' : details[index]?.data?.ticket?.text?.split("\n")[0].slice(0, 80) || '신고 내용 불러오는 중'}</h2><p>{item.confirmed_location_label || item.location_text || '확정 위치 미지정'}</p>
    {item.plan_sequence != null && <p className="field-hint">승인된 방문 순서 {item.plan_sequence + 1}번</p>}<WorkerSafety safety={item.safety} compact />
    <p className="worker-list-step">{item.execution.correction_requested ? '보완 보고 필요' : item.execution.phase === 'queued' ? '내 할 일' : item.execution.phase === 'moving' || item.execution.phase === 'working' ? '진행 중' : '방문 상태 확인'} · {t(`executionPhase.${item.execution.phase}`)}</p>
    {item.execution.phase === 'visit_ended' && awaitsWorkerConfirmation(details[index]?.data?.ticket.latest_report) && <p className="pending-label">내 확인이 필요한 보고</p>}
    {item.execution.correction_requested && <p className="pending-label">보완 요청이 있어요 · 새 보고 작성</p>}
    <div className="ticket-card-top worker-list-meta"><span>{item.public_no.slice(0, 6)}</span><StatusBadge status={item.status} />{item.assigned_at && <span>배정 <time dateTime={item.assigned_at} title={dateTime(item.assigned_at, 'full')}>{relativeTime(item.assigned_at)}</time></span>}<span className="worker-list-detail">상세 보기 →</span></div>
  </Link>
))}</div><div className="form-actions">{offset > 0 && <button className="button secondary" onClick={() => setOffset(value => Math.max(0, value - 20))}>{t('myWorkPrevious')}</button>}{offset + query.data.items.length < query.data.total && <button className="button secondary" onClick={() => setOffset(value => value + 20)}>{t('myWorkMore')}</button>}</div></>}<WorkerSettings /></>
}
export function WorkDetailPage() {
  const { assignmentId = '' } = useParams()
  const { cacheScope } = useSession()
  return <WorkDetailContent key={JSON.stringify([cacheScope, assignmentId])} assignmentId={assignmentId} />
}
function WorkDetailContent({ assignmentId }: { assignmentId: string }) {
  const { t } = useTranslation('product')
  const [confirmationPending, setConfirmationPending] = useState(false)
  const { session, cacheScope, draftStore, workerDrafts } = useSession()
  const [autoRead, setAutoRead] = useState(false)
  const token = draftStore.token()
  const isCurrent = () => draftStore.current(token)
  const drafts = workerDrafts.get(assignmentId)
  useSyncExternalStore(drafts.subscribe, drafts.snapshot, drafts.snapshot)
  const query = useQuery({ queryKey: ['my-work-detail', cacheScope, assignmentId], queryFn: async ({ signal }) => {
    try {
      const value = await request<Assignment>(`/worker/assignments/${encodeURIComponent(assignmentId)}`, { signal })
      if (!isCurrent()) throw new Error('session_changed')
      workerDrafts.verify(assignmentId)
      return value
    } catch (value) { if (isCurrent() && value instanceof ApiError && (value.code === 'assignment_not_found' || value.code === 'assignment_forbidden')) workerDrafts.deny(assignmentId); throw value }
  }, staleTime: 0, gcTime: 0, refetchOnMount: 'always', retry: false, refetchOnWindowFocus: false })
  const { refetch } = query
  const automaticRefetch = useCallback(async (...args: Parameters<typeof refetch>) => { setAutoRead(true); const result = await refetch(...args); if (!result.isError) setAutoRead(false); return result }, [refetch])
  const manualRefetch = async () => { const recovering = autoRead && query.isError; if (!recovering) setAutoRead(false); const result = await query.refetch(); if (!result.isError) setAutoRead(false); return result }
  useForegroundRefresh({ ...query, refetch: automaticRefetch }, { enabled: Boolean(isCurrent() && !drafts.accessEnded && !drafts.pending('confirm:') && !confirmationPending && !query.isError && pendingReport(query.data)) })
  if (query.isPending) return <Loading />
  if (drafts.accessEnded) return <section className="content-card"><p className="notice">{t('workAccessEnded')}</p><Link className="button secondary" to="/work">{t('myWork')}</Link></section>
  if (query.isError && (!query.data || !autoRead || (query.error instanceof ApiError && [401, 403, 404, 409].includes(query.error.status)))) return <><Link className="back-link" to="/work">{t('myWork')}</Link>{query.error instanceof ApiError && [403,404,409].includes(query.error.status) ? <p className="notice">{t('myWorkAssignmentUnavailable')}</p> : <ErrorNotice error={query.error} retry={() => void manualRefetch()} />}</>
  return <><Link className="back-link" to="/work">← {t('myWork')}</Link><WorkerAssignmentScreen key={`${cacheScope}:${assignmentId}`} assignment={query.data!} refreshError={query.isError} drafts={drafts} onAccessEnded={() => workerDrafts.deny(assignmentId)} onConfirmationPending={setConfirmationPending} auth={{ csrf: session?.csrf_token, assignmentId }} fetching={query.isFetching} isCurrent={isCurrent} reload={async () => { const latest = await manualRefetch(); if (latest.isError || !latest.data) throw latest.error ?? new Error('assignment_unavailable'); return latest.data }} /></>
}
function WorkerAssignmentScreen({ assignment, auth, fetching, refreshError, isCurrent, reload, drafts, onAccessEnded, onConfirmationPending }: { drafts?: AssignmentDrafts; onAccessEnded?: () => void; onConfirmationPending?: (pending: boolean) => void; assignment: Assignment; auth: Auth; fetching: boolean; refreshError?: boolean; isCurrent: () => boolean; reload: () => Promise<Assignment> }) {
  const { cacheScope } = useSession()
  const queryClient = useQueryClient()
  const { t } = useTranslation('product')
  const blockedByOngoing = ['predecessor_visit_not_ended', 'worker_has_ongoing_visit'].includes(assignment.execution?.blocked_reason ?? '')
  const blockedList = useQuery({ queryKey: ['my-work', cacheScope, 0], queryFn: ({ signal }) => request<WorkList>('/worker/assignments?limit=20&offset=0&sort=urgent', { signal }), enabled: Boolean(auth.assignmentId && blockedByOngoing), retry: false, refetchOnWindowFocus: false })
  const activeOthers = blockedList.data?.items.filter(item => item.assignment_id !== assignment.assignment_id && ['moving', 'working'].includes(item.execution.phase)) ?? []
  const blockedWork = !blockedList.isError && !blockedList.isFetching && blockedList.data?.total === blockedList.data?.items.length && activeOthers.length === 1 ? activeOthers[0] : null
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [executionNeedsRefresh, setExecutionNeedsRefresh] = useState(false)
  const executionKey = useRef<{ fingerprint: string; key: string } | null>(null)
  const reportHeading = useRef<HTMLElement>(null)
  const [reportTarget, setReportTarget] = useState<HTMLElement | null>(null)
  const [reportObservation, setReportObservation] = useState<{ target: Element; visible: boolean } | null>(null)
  const setReportHeading = useCallback((node: HTMLElement | null) => { reportHeading.current = node; setReportTarget(node) }, [])
  const observedTarget = reportTarget?.closest('form') ?? reportTarget
  const reportVisible = Boolean(observedTarget && reportObservation?.target === observedTarget && reportObservation.visible)
  useEffect(() => {
    if (!observedTarget || typeof IntersectionObserver === 'undefined') return
    let active = true
    const observer = new IntersectionObserver(([entry]) => { if (active) setReportObservation({ target: observedTarget, visible: entry.isIntersecting }) }, { rootMargin: '0px 0px -80px 0px' })
    observer.observe(observedTarget)
    return () => { active = false; observer.disconnect() }
  }, [observedTarget])
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const ticket = assignment.ticket
  const execution = assignment.execution
  const correction = ticket.latest_report?.correction_requested_at ? ticket.latest_report : null
  const awaitingReport = awaitsWorkerConfirmation(ticket.latest_report)
  async function reportSaved() {
    await reload()
    void queryClient.invalidateQueries({ queryKey: ['my-work', cacheScope] })
  }
  async function refresh() { try { await reload(); if (isCurrent() && mounted.current) { setExecutionNeedsRefresh(false); setError(null) } } catch (value) { if (isCurrent() && mounted.current) setError(value) } }
  async function executionAction(action: 'depart' | 'start') {
    if (busy || executionNeedsRefresh || !isCurrent()) return
    setBusy(true); setError(null)
    const body = { expected_execution_revision: execution?.revision }; executionKey.current = persistentKey(executionKey.current, { action, body })
    try { await request<Ticket>(`/worker/assignments/${assignment.assignment_id}/${action}`, { method: 'POST', body, key: executionKey.current.key }, auth); if (isCurrent() && mounted.current) await reload() } catch (value) { if (isCurrent()) { if (value instanceof ApiError && (value.code === 'assignment_not_found' || value.code === 'assignment_forbidden' || (Boolean(auth.bearer) && value.status === 401 && value.code === 'invalid_worker_token'))) onAccessEnded?.(); if (mounted.current) { setExecutionNeedsRefresh(true); setError(value) } } } finally { if (isCurrent() && mounted.current) setBusy(false) }
  }
  return <><div className="page-heading ticket-heading"><div><p className="product-eyebrow">{t('workerHint')}</p><h1>{ticket.text.split("\n")[0].slice(0, 80)}</h1><TicketReceiptReference value={ticket.public_no} /></div><StatusBadge status={ticket.status} /></div><div className="worker-refresh"><button className="button secondary" disabled={fetching || busy} onClick={() => void refresh()}>{t('refresh')}</button>{refreshError && <p className="notice" role="status">최신 상태를 불러오지 못했어요. 다시 확인해 주세요.</p>}</div><div className="worker-layout"><WorkerSafety safety={ticket.safety} /><section className="content-card worker-task-detail"><h2 className="worker-ticket-title">신고 원문</h2><p className="ticket-body">{ticket.text}</p><dl className="ticket-facts"><div><dt>확정 위치</dt><dd>{ticket.location?.state === 'confirmed' && ticket.location.selected?.label ? ticket.location.selected.label : '확정 위치 미지정'}</dd></div>{ticket.location_text && ticket.location_text.trim() !== ticket.location?.selected?.label?.trim() && <div><dt>신고자가 입력한 위치</dt><dd>{ticket.location_text}</dd></div>}<div><dt>{t('category')}</dt><dd>{ticket.category_label || t('unknown')}</dd></div></dl>{ticket.location?.state === 'confirmed' && ticket.location.selected && <div className="worker-destination">{ticket.location.selected.latitude != null && ticket.location.selected.longitude != null ? <Directions point={{ latitude: ticket.location.selected.latitude, longitude: ticket.location.selected.longitude, label: ticket.location.selected.label }} /> : <a className="button secondary" href="https://map.snu.ac.kr/web/main.action" target="_blank" rel="noopener noreferrer">서울대 캠퍼스맵</a>}</div>}<Photos photos={ticket.photos} auth={auth} /><section className="worker-execution"><h3>{t('executionTitle')}</h3><p className="worker-progress" aria-label="작업 진행 단계">{['queued', 'moving', 'working'].map((phase, index) => <span key={phase} aria-current={execution?.phase === phase ? 'step' : undefined}>{index + 1}. {t(`executionPhase.${phase}`)}</span>)}</p>{execution?.legacy_state_inferred && <p className="field-hint">{t('executionLegacy')}</p>}{execution?.blocked_reason && <div className="notice">{blockedByOngoing ? <><p>진행 중인 작업을 먼저 끝내 주세요.</p>{blockedWork ? <Link to={`/work/${blockedWork.assignment_id}`}>그 작업으로 이동</Link> : auth.assignmentId && <Link to="/work">내 작업에서 확인</Link>}</> : execution.blocked_reason === 'visit_already_ended' && !auth.assignmentId ? '이번 방문은 이미 종료됐어요. 새 방문은 새 배정 링크에서 열어 주세요.' : t(`executionBlocked.${execution.blocked_reason}`, { defaultValue: t('executionBlockedUnknown') })}</div>}{!execution && <p className="notice">{t('executionUnknown')}</p>}{execution?.phase === 'stopped' && <p className="notice">{t('executionStopped')}</p>}{execution?.phase === 'visit_ended' && <p className="notice">{awaitingReport ? '현장 방문을 마쳤어요. 이 보고는 나중에 확인해 제출할 수 있어요.' : t(ticket.status === 'completed' ? 'executionCompletedVisit' : 'executionUnresolvedVisit')}</p>}{executionNeedsRefresh && <p className="notice">처리 결과를 확인할 수 없어요. 최신 상태를 확인한 뒤 다시 진행해 주세요.</p>}{execution?.can_start && execution.can_depart && <button className="button secondary full-width" disabled={busy || executionNeedsRefresh} onClick={() => void executionAction('start')}>현장에서 바로 작업 시작</button>}</section>{Boolean(error) && <ErrorNotice error={error} />}</section>
    <LinkedSafetySummary ticket={ticket} />
    {ticket.latest_report && (workerConfirmed(ticket.latest_report as Report) || ticket.latest_report.reviewed) && <><p className="notice" role="status">{correction ? `보완 요청: ${correction.correction_reason}` : t(ticket.latest_report.reviewed ? 'reviewed' : 'approvalPending')}</p><ReportSnapshot report={ticket.latest_report as Report} auth={auth} onPhotoUpdate={() => void refresh()} compact={!ticket.latest_report.reviewed && !correction} /></>}
    {(drafts?.pending('confirm:') || (['working', 'visit_ended'].includes(execution?.phase ?? '') && ticket.latest_report && !ticket.latest_report.reviewed && !workerConfirmed(ticket.latest_report as Report))) && <WorkerReportConfirmation key={drafts?.pending('confirm:')?.key ?? ticket.latest_report!.id} report={drafts?.pending('confirm:')?.read<Report>('snapshot') ?? ticket.latest_report as Report} latestReport={ticket.latest_report as Report | null} assignmentId={assignment.assignment_id} auth={auth} memory={drafts?.pending('confirm:') ?? drafts?.get(`confirm:${ticket.latest_report!.id}`)} isCurrent={isCurrent} onAccessEnded={onAccessEnded} onPendingChange={onConfirmationPending} staleRead={refreshError} nextWork={auth.assignmentId && execution?.phase === 'visit_ended' && !refreshError && <p className="report-next-action"><Link className="button secondary" to="/work">다음 작업 확인</Link></p>} onDone={reportSaved} onLoadLatest={async () => { const latest = await reload(); if (!latest.ticket.latest_report) throw new Error('report_unavailable'); return latest.ticket.latest_report as Report }} />}
    {(drafts?.pending('report:') || (execution?.can_report && ticket.status === 'in_progress' && (!ticket.latest_report || ticket.latest_report.reviewed || Boolean(correction))) || (drafts && !drafts.report(ticket.latest_report?.id ?? 'first').read('saved') && Boolean(drafts.report(ticket.latest_report?.id ?? 'first').uploads.snapshot().text))) ? <WorkerReport category={ticket.category_code} headingRef={setReportHeading} key={drafts?.report(ticket.latest_report?.id ?? 'first').key ?? ticket.latest_report?.id ?? 'first'} assignmentId={assignment.assignment_id} auth={auth} memory={drafts?.report(ticket.latest_report?.id ?? 'first')} canReport={Boolean(execution?.can_report && (!ticket.latest_report || ticket.latest_report.reviewed || Boolean(correction)))} reportCycle={ticket.latest_report?.id ?? 'first'} supersedesReportId={correction?.id} executionRevision={execution?.revision ?? 0} executionNeedsRefresh={executionNeedsRefresh} isCurrent={isCurrent} onAccessEnded={onAccessEnded} onReload={async () => { const latest = await reload(); if (!latest.execution?.can_report || (latest.ticket.latest_report && !latest.ticket.latest_report.reviewed && !latest.ticket.latest_report.correction_requested_at)) throw new Error('execution_not_reportable'); if (isCurrent() && mounted.current) setExecutionNeedsRefresh(false); return { revision: latest.execution.revision, cycle: latest.ticket.latest_report?.id ?? 'first' } }} onDone={reportSaved} /> : !execution?.can_report && !ticket.latest_report && !blockedByOngoing && <p className="muted">{t('workerState')}</p>}

    {(execution?.can_depart || execution?.can_start || (execution?.can_report && ticket.status === 'in_progress' && !ticket.latest_report && !reportVisible)) && <div className="worker-action-bar">
      {execution?.can_depart ? <button className="button primary full-width" disabled={busy || executionNeedsRefresh} onClick={() => void executionAction('depart')}>{t('executionDepart')}</button> : execution?.can_start ? <button className="button primary full-width" disabled={busy || executionNeedsRefresh} onClick={() => void executionAction('start')}>{execution.phase === 'queued' ? '현장에서 바로 작업 시작' : t('executionStart')}</button> : <button type="button" className="button primary full-width" onClick={() => { if (!isCurrent()) return; const heading = reportHeading.current; if (heading) { heading.focus({ preventScroll: true }); heading.scrollIntoView({ block: 'start' }) } }}>보고로 이동</button>}
    </div>}
  </div></>
}
const commonActions: Record<Result, string[]> = {
  completed: ['고장 부품을 교체하고 정상 작동을 확인했어요.', '현장에서 조치하고 정상 상태를 확인했어요.'],
  temporary: ['임시 조치를 했어요. 추가 수리가 필요해요.', '현장 접근을 제한했어요. 추가 점검이 필요해요.'],
  revisit: ['교체 부품을 확보한 뒤 다시 방문해야 해요.', '추가 인력과 함께 다시 방문해야 해요.'],
  needs_review: ['현장을 점검했지만 원인을 확인하지 못했어요.', '상태를 확인했어요. 담당자의 추가 판단이 필요해요.'],
}

function suggestedActions(result: Result, category?: string | null) {
  if (result !== 'completed') return commonActions[result]
  if (category?.includes('LIGHT')) return ['조명을 교체하고 점등을 확인했어요.', '조명 작동을 점검하고 복구했어요.']
  if (category?.includes('LEAK')) return ['누수 부위를 수리하고 재발 여부를 확인했어요.', '연결부를 교체하고 누수가 멈췄는지 확인했어요.']
  return commonActions.completed
}

type PendingReport = { body: Readonly<{ result: Result; text: string; upload_ids: readonly string[]; expected_execution_revision: number; supersedes_report_id?: string }>; key: string }
function WorkerReport({ category, headingRef, assignmentId, auth, executionRevision, executionNeedsRefresh, reportCycle, supersedesReportId, canReport, memory, isCurrent, onAccessEnded, onReload, onDone }: { category?: string | null; headingRef: (node: HTMLElement | null) => void; assignmentId: string; auth: Auth; executionRevision: number; executionNeedsRefresh: boolean; reportCycle: string; supersedesReportId?: string; canReport: boolean; memory?: WorkerFormDraft; isCurrent: () => boolean; onAccessEnded?: () => void; onReload: () => Promise<{ revision: number; cycle: string }>; onDone: () => Promise<void> }) {
  const { t } = useTranslation('product')
  const isPublic = useRuntime().mode === 'public_demo'
  const [local] = useState(() => { const form = new WorkerFormDraft('legacy-report'); form.activate('legacy'); return form })
  const form = memory ?? local
  const [snapshotRevision, setSnapshotRevision] = useWorkerField(form, 'revision', () => executionRevision)
  const [snapshotCycle, setSnapshotCycle] = useWorkerField(form, 'cycle', () => reportCycle)
  const [blocked, setBlocked] = useWorkerField(form, 'blocked', () => false)
  const [result, setResult] = useWorkerField<Result | ''>(form, 'result', () => '')
  const [pending, setPending] = useWorkerField<PendingReport | null>(form, 'pending', () => null)
  const [busy, setBusy] = useWorkerField(form, 'busy', () => false)
  const [submitted, setSubmitted] = useWorkerField(form, 'saved', () => false)
  const [error, setError] = useState<unknown>(null)
  const uploads = useUploads(form.uploads)
  const data = useMemoryDraft(form.uploads)
  const text = data.text
  const setText = (value: string) => form.uploads.edit(form.uploads.token(), { text: value })
  const token = form.uploads.token()
  const current = () => isCurrent() && form.uploads.current(token)
  const stale = blocked || snapshotRevision !== executionRevision || snapshotCycle !== reportCycle || !canReport
  async function reloadExecution() {
    if (pending || busy || !current()) return
    setBusy(true); setError(null)
    try { const latest = await onReload(); if (current()) { setSnapshotRevision(latest.revision); setSnapshotCycle(latest.cycle); setBlocked(false) } } catch (value) { if (current()) setError(value) } finally { if (current()) setBusy(false) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!current() || busy || (pending && blocked) || (!pending && (!result || !text.trim() || stale || !uploads.ready || uploads.preparing || uploads.protecting))) return
    setBusy(true); setError(null)
    form.uploads.update(token, { sending: true })
    try {
      let saved = pending
      if (!saved) {
        const upload_ids = isPublic ? [] : await uploads.upload(auth)
        if (!current()) return
        saved = { body: Object.freeze({ result: result as Result, text: text.trim(), upload_ids: Object.freeze([...upload_ids]), expected_execution_revision: snapshotRevision, ...(supersedesReportId ? { supersedes_report_id: supersedesReportId } : {}) }), key: crypto.randomUUID() }
        setPending(saved)
        form.uploads.update(token, { sealed: true })
      }
      await request<Ticket>(`/worker/assignments/${assignmentId}/report`, { method: 'POST', body: saved.body, key: saved.key }, auth)
      if (!current()) return
      setPending(null); setSubmitted(true)
      form.uploads.update(token, { sealed: false })
      await onDone()
    } catch (value) {
      if (current()) {
        if (value instanceof ApiError && (value.code === 'assignment_not_found' || (Boolean(auth.bearer) && value.status === 401 && value.code === 'invalid_worker_token'))) onAccessEnded?.()
        else {
          if (value instanceof ApiError && value.code !== 'idempotency_conflict' && [400,409,413,415,422].includes(value.status)) { setPending(null); form.uploads.update(token, { sealed: false }) }
          if (value instanceof ApiError && value.status === 409) setBlocked(true)
          setError(value)
        }
      }
    } finally { if (current()) { setBusy(false); form.uploads.update(token, { sending: false }) } }
  }
  if (submitted) return <div ref={headingRef} tabIndex={-1} className="notice notice-success worker-report-target" role="status">{t('reportDraftSaved')}</div>
  return <form className="content-card report-form" onSubmit={event => void submit(event)}><h2 ref={headingRef} tabIndex={-1} className="worker-report-target">{t('workReport')}</h2>{memory && <p className="field-hint">{t('workDraftMemoryHint')}</p>}{pending && <><p className="notice" role="status">{t('workPendingHint')}</p>{blocked && <><p className="notice">{t('workIdempotencyConflict')}</p><button type="button" className="button secondary" onClick={() => { void onDone().catch(value => { if (current()) setError(value) }) }}>{t('refresh')}</button></>}</>}{!pending && stale && <p className="notice">{t('executionDraftStale')}</p>}{(stale || executionNeedsRefresh) && <button type="button" className="text-button" disabled={busy || Boolean(pending)} onClick={() => void reloadExecution()}>{t('executionReloadForReport')}</button>}<fieldset disabled={busy || Boolean(pending)}><fieldset className="work-results"><legend>{t('resultLabel')}</legend>{([['completed', '작업 완료', '원인을 처리했어요.'], ['temporary', '임시 조치', '당장 위험은 막았지만 수리가 더 필요해요.'], ['revisit', '재방문 필요', '부품이나 인력이 더 필요해 다시 와야 해요.'], ['needs_review', '확인 필요', '원인을 확정하지 못했어요. 담당자 판단이 필요해요.']] as [Result, string, string][]).map(([value, label, description]) => <label className="work-result" key={value}><input type="radio" name="work-result" value={value} checked={result === value} onChange={() => {
      if (result && suggestedActions(result, category).includes(text)) setText('')
      setResult(value)
    }} required /><span><strong>{label}</strong><small>{description}</small></span></label>)}{!result && <p className="field-hint">조치 결과를 골라 주세요.</p>}</fieldset><label htmlFor="work-text">{t('workText')}</label>
    {result && <div className="report-action-options" role="group" aria-label="자주 쓰는 조치">
      {suggestedActions(result, category).map(action => <button type="button" className="button secondary" key={action} aria-pressed={text === action} disabled={Boolean(text.trim()) && !suggestedActions(result, category).includes(text)} onClick={() => setText(action)}>{action}</button>)}
    </div>}
    <textarea id="work-text" value={text} onChange={event => setText(event.target.value)} placeholder="실제로 한 조치를 한 줄로 적어 주세요." rows={2} maxLength={4000} required /></fieldset>{!isPublic && <UploadField uploads={uploads} auth={auth} disabled={busy || Boolean(pending)} />}{Boolean(error) && <ErrorNotice error={error} />}<button className="button primary full-width" disabled={busy || Boolean(pending && blocked) || (!pending && (stale || !result || !text.trim() || !uploads.ready || uploads.preparing || uploads.protecting))}>{busy ? t('reportDraftSaving') : pending ? t('workPendingRetry') : '조치 저장'}</button></form>
}
