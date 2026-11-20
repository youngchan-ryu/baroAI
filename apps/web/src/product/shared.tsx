import './statusBadge.css'
import { MailDeliveryStatus } from './MailDeliveryStatus'
import { useOptionalRuntime, useRuntime } from './runtime'
import { approvedReports, workerConfirmed, type Report } from './reportTypes'
import { AnalysisNotice } from './AnalysisNotice'
import { displayLocation } from './locationDisplay'
import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { dateKey, dateTime, displayDate, relativeTime } from './dateTime'
import { Link, Navigate, useLocation } from 'react-router'
import { useTranslation } from 'react-i18next'
import { ApiError, imageRequest, request, uploadPath, type Auth } from './api'
import { useSession } from './session'
import { TimelineReason } from './TimelineReason'
import type { Photo, Ticket } from './types'
import type { components } from '../api/schema'

export function ErrorNotice({ error, retry, onReload }: { error: unknown; retry?: () => void; onReload?: () => void }) {
  const { t } = useTranslation('product')
  const runtime = useOptionalRuntime()
  const code = error instanceof ApiError ? error.status : 0
  const mergeErrors = ['merge_source_report_pending', 'merge_source_stop_required', 'merge_not_open', 'merge_chain_forbidden', 'merge_candidate_changed', 'merge_not_active', 'merged_source_locked']
  const executionErrors = ['service_plan_execution_stale', 'execution_revision_conflict', 'predecessor_visit_not_ended', 'remaining_reconfirmation_required', 'report_pending_review', 'stopped_plan_requires_cancellation', 'plan_stop_confirmation_conflict', 'execution_location_unconfirmed', 'parent_plan_revision_conflict', 'insertion_time_range_exceeded']
  const executionMessage = error instanceof ApiError && error.code === 'worker_upload_closed' ? 'workerUploadClosed' : error instanceof ApiError && error.code === 'operation_abandoned' ? 'photoUploadAbandoned' : error instanceof ApiError && error.code === 'photo_storage_unavailable' ? 'photoStorageUnavailable' : error instanceof ApiError && executionErrors.includes(error.code) ? `executionError.${error.code}` : null
  const mergeMessage = error instanceof ApiError && mergeErrors.includes(error.code) ? `mergeError.${error.code}` : null
  return <div className="notice notice-error" role="alert"><p>{t(executionMessage ?? mergeMessage ?? (code === 401 || code === 403 || code === 404 ? (runtime?.mode === 'demo' ? 'deniedError' : 'deniedServiceError') : code === 409 ? 'conflictError' : code === 422 || code === 413 || code === 415 ? 'invalidError' : 'genericError'))}</p>{error instanceof ApiError && error.status >= 500 && error.requestId && <details><summary>문의용 코드</summary><small>{error.requestId}</small></details>}{code === 409 && onReload && <button type="button" className="text-button" onClick={onReload}>최신 내용 불러오기</button>}{retry && <button type="button" className="text-button" onClick={retry}>{t('retry')}</button>}</div>
}
export function ConnectionError({ title, error, retry }: { title: string; error: unknown; retry: () => void }) {
  return <section className="empty-state" role="alert"><h1>{title}</h1><p>연결을 확인하고 다시 시도해 주세요.</p>{error instanceof ApiError && error.status >= 500 && error.requestId && <details><summary>문의용 코드</summary><small>{error.requestId}</small></details>}<div className="form-actions"><button type="button" className="button primary" onClick={retry}>다시 시도</button><a className="button secondary" href="/">처음 화면으로</a></div></section>
}
export function Loading() { const { t } = useTranslation('product'); return <div className="loading-skeleton" role="status"><span className="skeleton-status">{t('loading')}</span><div className="skeleton-cards" aria-hidden="true">{[0, 1].map(i => <div className="skeleton-card" key={i}><span /><span /><span /></div>)}</div></div> }
export function Protected({ children, role }: { children: ReactNode; role?: string }) {
  const { session, cacheScope, loading, switching, error, reload, explicitLogout } = useSession()
  const runtime = useRuntime()
  const location = useLocation()
  const { t } = useTranslation('product')
  if (loading || switching) return <Loading />
  if (!session && error) return <ConnectionError title={t('authCheckFailed')} error={error} retry={reload} />
  if (!session && explicitLogout) return <Navigate to="/" replace />
  if (!session && runtime.mode !== 'demo') {
    const staff = runtime.mode === 'service' && runtime.staff_password_auth && (role === 'worker' || role === 'dispatcher')
    return <Navigate to={`${staff ? '/staff/login' : '/login'}?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  }
  if (!session) return <section className="empty-state"><h1>{t('loginRequired')}</h1><p>{t('loginHint')}</p>{Boolean(error) && <ErrorNotice error={error} retry={reload} />}<Link className="button primary" to={{ pathname: '/', search: `?${new URLSearchParams({ next: location.pathname + location.search })}` }}>시연 역할 선택</Link></section>
  if (role && session.user.role !== role) return <section className="empty-state"><h1>{t('forbidden')}</h1><Link to="/">{t('home')}</Link></section>
  return <div key={cacheScope}>{children}</div>
}
export function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation('product')
  return <span className={`ticket-status state-${status}`}>{t(`status.${status}`, { defaultValue: '상태 확인 필요' })}</span>
}
export function ResultBadge({ result }: { result: string }) {
  const { t } = useTranslation('product')
  return <span className={`result-badge result-${result}`}>{t(`result.${result}`, { defaultValue: '결과 확인 필요' })}</span>
}
// eslint-disable-next-line react-refresh/only-export-components
export function date(value: string) { return dateTime(value) }
export function Photos({ photos, bearer, auth }: { photos: Photo[]; bearer?: string; auth?: Auth }) {
  const { t } = useTranslation('product')
  return <div className="photo-grid">{photos.map((photo, index) => <PrivatePhoto key={photo.id} photo={photo} auth={auth ?? { bearer }} label={`${t('photos')} ${index + 1}`} />)}</div>
}
function PrivatePhoto({ photo, auth, label }: { photo: Photo; auth: Auth; label: string }) {
  const { t } = useTranslation('product')
  const {bearer, csrf, assignmentId} = auth
  const [source, setSource] = useState('')
  const [failed, setFailed] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let objectUrl = ''; let active = true
    const controller = new AbortController()
    queueMicrotask(() => { if (active) { setSource(''); setFailed(false) } })
    // Always use the authenticated local endpoint; never fetch arbitrary DTO URLs.
    imageRequest(uploadPath({bearer,csrf,assignmentId}, photo.id), {bearer,csrf,assignmentId}, controller.signal, () => active).then(blob => { if (active) { objectUrl = URL.createObjectURL(blob); setSource(objectUrl); setFailed(false) } }).catch(() => { if (active) setFailed(true) })
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [photo.id, bearer, csrf, assignmentId, revision])
  return <figure className="private-photo">{source ? <a href={source} target="_blank" rel="noreferrer"><img src={source} alt={label} /></a> : failed ? <div><span>{t('photoFailed')}</span><button className="text-button" onClick={() => setRevision(value => value + 1)}>{t('retry')}</button></div> : <span>{t('loading')}</span>}</figure>
}
export function TicketCard({ ticket, admin = false }: { ticket: Ticket; admin?: boolean }) {
  const { t } = useTranslation('product')
  return <Link className="ticket-card" to={`${admin ? '/admin/t' : '/t'}/${ticket.id}`}><div className="ticket-card-top"><span className="ticket-number">{admin ? ticket.public_no : ticket.public_no.slice(0, 6)}</span><StatusBadge status={ticket.status} /></div>{!admin && ticket.information_status === 'waiting' && <span className="pending-label">답변 필요</span>}<h2>{ticket.text}</h2><p className="ticket-location"><span aria-hidden="true">⌖</span> {displayLocation(ticket) || t('locationUnknown')}</p><div className="ticket-card-bottom"><span>{admin ? ticket.category_label || t('unknown') : ''}</span><span><time dateTime={ticket.created_at} title={dateTime(ticket.created_at, 'full')}>{admin ? dateTime(ticket.created_at) : relativeTime(ticket.created_at)} 접수</time> <span aria-hidden="true">↗</span></span></div>{ticket.latest_report && !ticket.latest_report.reviewed && admin && <p className="pending-label">{ticket.latest_report.correction_requested_at ? '보완 요청 중' : t(workerConfirmed(ticket.latest_report) ? 'approvalPending' : 'reportWorkerPending')} · {t(`result.${ticket.latest_report.confirmed_result ?? ticket.latest_report.result}`)}</p>}{admin && ticket.visit_followup && <p className="pending-label">{ticket.visit_followup === 'end_visit' ? '미해결 · 방문 종료 필요' : '미해결 · 새 배정 필요'}</p>}</Link>
}
export function TicketDates({ ticket }: { ticket: Ticket }) {
  return <p className="field-hint">접수 <time dateTime={ticket.created_at} title={dateTime(ticket.created_at, 'full')}>{displayDate(ticket.created_at)}</time> · 마지막 변경 <time dateTime={ticket.updated_at} title={dateTime(ticket.updated_at, 'full')}>{displayDate(ticket.updated_at)}</time></p>
}
export function StaffPhotoCoordinates({ ticketId, ticketPhotos }: { ticketId: string; ticketPhotos: Photo[] }) {
  const [photos, setPhotos] = useState<components['schemas']['PhotoCoordinatesView'][]>([])
  const [error, setError] = useState(false)
  useEffect(() => {
    let current = true
    void request<components['schemas']['TicketPhotoCoordinatesView']>(`/tickets/${encodeURIComponent(ticketId)}/photo-coordinates`).then(value => { if (current) setPhotos(value.photos) }).catch(() => { if (current) setError(true) })
    return () => { current = false }
  }, [ticketId])
  useEffect(() => {
    if (!photos.length) return
    const remaining = Math.min(...photos.map(photo => Date.parse(photo.delete_after) - Date.now()))
    const timer = window.setTimeout(() => setPhotos(current => current.filter(photo => Date.parse(photo.delete_after) > Date.now())), Math.max(0, Math.min(remaining, 2_147_483_647)))
    return () => window.clearTimeout(timer)
  }, [photos])
  if (error) return <p className="field-hint">사진 위치를 불러오지 못했어요. 다시 열어 확인해 주세요.</p>
  if (!photos.length) return null
  return <div><h3>사진 촬영 위치</h3><p className="field-hint">신고자가 제공한 참고 위치예요. 현장을 직접 확인해 주세요.</p><ul>{photos.map(photo => <li key={photo.id}>사진 {ticketPhotos.findIndex(item => item.id === photo.id) + 1}: 위도 {photo.latitude.toFixed(5)}, 경도 {photo.longitude.toFixed(5)}</li>)}</ul></div>
}
export function TicketContent({ ticket, bearer, onRefresh, refreshing, reporter = false }: { ticket: Ticket; bearer?: string; onRefresh?: () => void; refreshing?: boolean; reporter?: boolean }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const runtime = useOptionalRuntime()
  const internal = Boolean(bearer) || session?.user.role === 'dispatcher'
  const reports = approvedReports(ticket)
  const latest = ticket.latest_report as Report | null
  return <>
    <section className="content-card"><div className="section-title"><h2>{t('original')}</h2><span className="ticket-number">{reporter ? ticket.public_no.slice(0, 6) : ticket.public_no}</span></div><p className="ticket-body">{ticket.text}</p><dl className="ticket-facts"><div><dt>{t('location')}</dt><dd>{displayLocation(ticket) || t('locationUnknown')}</dd></div>{!reporter && <div><dt>{t('category')}</dt><dd>{ticket.category_label || t('unknown')}</dd></div>}{!reporter && ticket.worker_name && <div><dt>{t('worker')}</dt><dd>{ticket.worker_name}</dd></div>}</dl>{ticket.photos.length > 0 && <Photos photos={ticket.photos} bearer={bearer} />}{runtime?.mode === 'service' && session?.user.role === 'dispatcher' && <StaffPhotoCoordinates key={ticket.id} ticketId={ticket.id} ticketPhotos={ticket.photos} />}</section>
    {!reporter && <AnalysisNotice ticket={ticket} onRefresh={onRefresh} refreshing={refreshing} />}
    {latest && (internal || latest.reviewed) && !reports.some(report => report.id === latest.id) && <section className="content-card"><div className="section-title"><h2>{t('reported')}</h2><ResultBadge result={latest?.confirmed_result ?? latest.result} /></div><p className="ticket-body">{latest.text}</p><Photos photos={latest.photos} bearer={bearer} /><span className="muted-label">{t(latest.reviewed ? 'reviewed' : workerConfirmed(latest) ? 'approvalPending' : 'reportWorkerPending')}</span></section>}
    {reports.length > 0 && <section className="content-card"><h2>{t('approvedReportHistory')}</h2>{reports.map(report => <article key={report.id} className="approved-report"><ResultBadge result={report.confirmed_result ?? report.result} /><p className="ticket-body">{report.reply_text ?? report.text}</p>{report.legacy_migrated && <p className="field-hint">{t('reportLegacy')}</p>}<Photos photos={report.photos} bearer={bearer} /><p className="field-hint">{t('reportScreenPublished')}</p><MailDeliveryStatus status={report.delivery_status} /></article>)}</section>}
    {!reporter && runtime?.mode !== 'service' && <section className="content-card"><h2>{t('reply')}</h2><p className={ticket.reply_text ? 'ticket-body' : 'muted'}>{ticket.reply_text || t('noReply')}</p>{ticket.reply_text && <p className="field-hint">{t('reportScreenPublished')}</p>}{ticket.reply_text && <MailDeliveryStatus status={reports.at(-1)?.delivery_status ?? 'not_sent'} />}</section>}
    {reporter ? <ReporterTimeline ticket={ticket} /> : <section className="content-card"><h2>{t('timeline')}</h2>{ticket.timeline.length ? <ol className="timeline">{ticket.timeline.map((event, index) => <Fragment key={event.id}>{(index === 0 || dateKey(event.at) !== dateKey(ticket.timeline[index - 1].at)) && <li className="field-hint">{dateTime(event.at, 'day')}</li>}<li><div className="timeline-dot" /><div><h3>{event.type === 'assignment_field_override' ? '분야 불일치 예외 배정' : event.label}</h3><p>{event.actor_label ?? '행위자 정보 없음'}</p><time dateTime={event.at} title={dateTime(event.at, 'full')}>{dateTime(event.at, 'clock')}</time><TimelineReason type={event.type} reason={event.reason} /></div></li></Fragment>)}</ol> : <p className="muted">{t('noTimeline')}</p>}</section>}
  </>
}

function ReporterTimeline({ ticket }: { ticket: Ticket }) {
  const labels: Record<string, string> = { created: '접수', received: '접수', confirmed: '담당자 확인', assigned: '작업 배정', reassigned: '작업 배정', urgent_plan_assigned: '작업 배정', report_reviewed: '작업 보고 확인' }
  const events = ticket.timeline.filter(event => labels[event.type]).filter((event, index, all) => index === 0 || labels[event.type] !== labels[all[index - 1].type])
  return <section className="content-card"><h2>처리 이력</h2>{events.length ? <ol className="timeline">{events.map(event => <li key={event.id}><div className="timeline-dot" /><div><h3>{labels[event.type]}</h3><time dateTime={event.at} title={dateTime(event.at, 'full')}>{displayDate(event.at)}</time></div></li>)}</ol> : <p>표시할 이력이 없어요.</p>}</section>
}

export function TicketReceiptReference({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return <p className="field-hint">접수 번호 {value.slice(0, 6)} <button type="button" className="text-button" onClick={() => { void navigator.clipboard?.writeText(value).then(() => setCopied(true)).catch(() => setCopied(false)) }}>전체 번호 복사</button>{copied && <span role="status"> 복사했어요</span>}</p>
}
