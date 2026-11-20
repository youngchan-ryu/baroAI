import './reportComparison.css'
import { useOptionalRuntime } from './runtime'
import { PhotoProtection } from './PhotoProtection'
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, persistentKey, request, type Auth } from './api'
import { WorkerFormDraft, useWorkerField, immutableRequest } from './workerDraft'
import type { Result, Ticket } from './types'
import type { Report, ReportStructure } from './reportTypes'
import { requiresReportReconfirmation, workerConfirmed } from './reportTypes'
import { ErrorNotice, Photos, ResultBadge } from './shared'

const blank: ReportStructure = { actions: [], materials_used: [], residual_issue: '', revisit_needed: false, photo_comparison: 'not_performed', source: 'manual' }
const lines = (value: string) => value.split('\n').map(item => item.trim()).filter(Boolean)
const pendingAnalysis = (report: Report) => ['queued', 'running', 'retry_wait'].includes(report.analysis_status ?? '')
function workerStructure(report: Report): ReportStructure {
  const structure = report.confirmed ?? report.draft
  if (structure) return structure
  const actions = lines(report.text)
  return { ...blank, actions: actions.length <= 5 && actions.every(action => action.length <= 120) ? actions : [] }
}

function ReportElapsed({ savedAt }: { savedAt?: string | null }) {
  const [currentTime, setCurrentTime] = useState(Date.now)
  useEffect(() => {
    if (!savedAt) return
    const timer = window.setInterval(() => setCurrentTime(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [savedAt])
  const timestamp = savedAt ? Date.parse(savedAt) : NaN
  if (!Number.isFinite(timestamp)) return null
  const seconds = Math.max(0, Math.floor((currentTime - timestamp) / 1000))
  const elapsed = seconds < 60 ? `${seconds}초` : `${Math.floor(seconds / 60)}분 ${seconds % 60}초`
  return <p className="field-hint">보고 저장 후 <time dateTime={`PT${seconds}S`}>{elapsed}</time> 경과</p>
}
export function ReportSnapshot({ report, bearer, auth, onPhotoUpdate, disabled = false, isCurrent, compact = false }: { compact?: boolean; report: Report; bearer?: string; auth?: Auth; disabled?: boolean; isCurrent?: () => boolean; onPhotoUpdate?: () => void }) {
  const { t } = useTranslation('product')
  const structure = report.confirmed ?? report.draft
  return <section className="content-card"><div className="section-title"><h2>{t('reportOriginal')}</h2><ResultBadge result={report.result} /></div><p className="ticket-body">{report.text}</p><Photos photos={report.photos} auth={auth ?? { bearer }} />{(bearer || auth?.assignmentId) && onPhotoUpdate && report.photos.map(photo => <PhotoProtection key={photo.id} photo={photo} auth={auth ?? { bearer }} onUpdate={onPhotoUpdate} disabled={disabled} isCurrent={isCurrent} onBusy={() => {}} />)}{!compact && structure && <div><h3>{t(report.confirmed ? 'reportConfirmedStructure' : report.draft?.source === 'live' ? 'reportDraftStructureLive' : 'reportDraftStructure')}</h3><StructureRead structure={structure} /></div>}{!compact && <p className="field-hint">{t('photoNotCompared')}</p>}</section>
}
function StructureRead({ structure, workerReport = false }: { structure: ReportStructure; workerReport?: boolean }) {
  const { t } = useTranslation('product')
  return <dl className="report-structure"><div><dt>{t('reportActions')}</dt><dd>{(structure.actions ?? []).length ? (structure.actions ?? []).join(' · ') : t('reportUnspecified')}</dd></div><div><dt>{t('reportMaterials')}</dt><dd>{(structure.materials_used ?? []).length ? (structure.materials_used ?? []).join(' · ') : t('reportUnspecified')}</dd></div><div><dt>{t('reportResidual')}</dt><dd>{structure.residual_issue || (workerReport ? '작업자 보고에 기재되지 않았어요' : t('reportUnspecified'))}</dd></div><div><dt>{t('reportRevisit')}</dt><dd>{structure.revisit_needed ? t('reportRevisitYes') : workerReport ? '작업자 보고에 필요하다고 표시되지 않았어요' : t('reportRevisitNo')}</dd></div><div><dt>{t('reportStructureSource')}</dt><dd>{t(structure.source === 'legacy' ? 'reportLegacy' : structure.source === 'replay' ? 'reportReplay' : structure.source === 'live' ? 'reportLive' : 'reportManual')}</dd></div></dl>
}
export function WorkerReportConfirmation({ report, latestReport, assignmentId, token, auth, memory, isCurrent = () => true, onAccessEnded, onPendingChange, staleRead = false, nextWork, onDone, onLoadLatest }: { report: Report; latestReport?: Report | null; memory?: WorkerFormDraft; isCurrent?: () => boolean; onAccessEnded?: () => void; onPendingChange?: (pending: boolean) => void; staleRead?: boolean; nextWork?: ReactNode; assignmentId: string; token?: string; auth?: Auth; onDone: () => Promise<void>; onLoadLatest: () => Promise<Report> }) {
  const { t } = useTranslation('product')
  const runtime = useOptionalRuntime()
  const [local] = useState(() => { const form = new WorkerFormDraft('legacy-confirm'); form.activate('legacy'); return form })
  const form = memory ?? local
  const owner = form.uploads.token()
  const current = () => isCurrent() && form.uploads.current(owner)
  const [snapshot, setSnapshot] = useWorkerField(form, 'snapshot', () => report)
  const [structure, setStructure] = useWorkerField<ReportStructure>(form, 'structure', () => workerStructure(report))
  const [actions, setActions] = useWorkerField(form, 'actions', () => (workerStructure(report).actions ?? []).join('\n'))
  const [materials, setMaterials] = useWorkerField(form, 'materials', () => ((report.confirmed ?? report.draft)?.materials_used ?? []).join('\n'))
  const [result, setResult] = useWorkerField<Result>(form, 'result', () => report.confirmed_result ?? report.result)
  const [reason, setReason] = useWorkerField(form, 'reason', () => '')
  const [dirty, setDirty] = useWorkerField(form, 'dirty', () => false)
  const [appliedDraft, setAppliedDraft] = useWorkerField(form, 'appliedDraft', () => Boolean(report.draft && ['replayed', 'drafted'].includes(report.analysis_status ?? '')))
  const [manualOpen, setManualOpen] = useWorkerField(form, 'manualOpen', () => !pendingAnalysis(report))
  const [busy, setBusy] = useWorkerField(form, 'busy', () => false)
  const [error, setError] = useState<unknown>(null)
  type ConfirmBody = { expected_revision: number; confirmed_result: Result; structure: ReportStructure; reason: string }
  const [retryKey, setRetryKey] = useWorkerField<{fingerprint:string;key:string}|null>(form, 'retryKey', () => null)
  const [pending, setPending] = useWorkerField<{body:ConfirmBody;key:string}|null>(form, 'pending', () => null)
  useEffect(() => { onPendingChange?.(Boolean(pending)); return () => onPendingChange?.(false) }, [onPendingChange, pending])
  const [saved, setSaved] = useWorkerField(form, 'saved', () => false)
  const [blocked, setBlocked] = useWorkerField(form, 'blocked', () => false)
  const latest = latestReport === undefined ? report : latestReport
  const stale = blocked || !latest || snapshot.id !== latest.id || (snapshot.revision ?? 0) !== (latest.revision ?? 0)
  const conflict = snapshot.result === 'completed' && ((snapshot.warnings ?? []).includes('completion_conflict') || Boolean(structure.residual_issue.trim()) || structure.revisit_needed)
  const invalidLists = [lines(actions), lines(materials)].some(list => list.length > 5 || list.some(item => item.length > 120))
  const manual = !appliedDraft || !['replayed', 'drafted'].includes(snapshot.analysis_status ?? '') || !snapshot.draft
  const waiting = pendingAnalysis(snapshot)
  const availableDraft = latest && latest.id === snapshot.id && latest.draft && ['replayed', 'drafted'].includes(latest.analysis_status ?? '') && ((latest.revision ?? 0) !== (snapshot.revision ?? 0) || latest.analysis_status !== snapshot.analysis_status)
  const availableTerminal = latest && latest.id === snapshot.id && waiting && !latest.draft && !workerConfirmed(latest) && !latest.reviewed && ['failed', 'superseded', 'replayed', 'drafted'].includes(latest.analysis_status ?? '')
  function acknowledge(next: Report) {
    setSnapshot(next); setRetryKey(null); setBlocked(false); setManualOpen(true)
    if (!dirty) {
      const nextStructure = workerStructure(next)
      setStructure(nextStructure); setActions((nextStructure.actions ?? []).join('\n')); setMaterials((nextStructure.materials_used ?? []).join('\n'))
      setResult(next.confirmed_result ?? next.result)
      setAppliedDraft(Boolean(next.draft && ['replayed', 'drafted'].includes(next.analysis_status ?? '')))
    } else setAppliedDraft(false)
  }
  async function reload() {
    if (pending || busy || !current()) return
    setBusy(true); setError(null)
    try { const latest = await onLoadLatest(); if (!current()) return; acknowledge(latest) } catch (value) { if (current()) setError(value) } finally { if (current()) setBusy(false) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!current() || busy || staleRead || (pending && blocked) || (!pending && (stale || invalidLists || (conflict && result !== 'needs_review')))) return
    const body = { expected_revision: snapshot.revision ?? 0, confirmed_result: result, structure: { ...structure, actions: lines(actions), materials_used: lines(materials), source: manual ? 'manual' : structure.source }, reason: reason.trim() || '작업자가 보고 내용을 확인했어요' }
    const selectedKey = pending ? { fingerprint: JSON.stringify(pending.body), key: pending.key } : persistentKey(retryKey, body)
    setRetryKey(selectedKey)
    const submission = pending ?? { body: immutableRequest(body) as ConfirmBody, key: selectedKey.key }
    onPendingChange?.(true); setPending(submission)
    setBusy(true); setError(null)
    try { await request<Ticket>(`/worker/assignments/${assignmentId}/reports/${snapshot.id}/confirm`, { method: 'POST', body: submission.body, key: submission.key }, auth ?? { bearer: token }); if (!current()) return; setPending(null); setSaved(true); await onDone() } catch (value) { if (current()) { if (value instanceof ApiError && value.code === 'assignment_not_found') onAccessEnded?.(); else { if (value instanceof ApiError && value.code !== 'idempotency_conflict' && [400,409,413,415,422].includes(value.status)) setPending(null); if (value instanceof ApiError && value.status === 409 && (memory || value.code === 'idempotency_conflict')) setBlocked(true); setError(value) } } } finally { if (current()) setBusy(false) }
  }
  if (saved) return <p className="notice" role="status">{t('workConfirmationSaved')}</p>
  return <div className="report-review-grid">
    {availableDraft && !pending && <section className="content-card">
      <p>새 AI 초안이 준비됐어요.{dirty ? ' 작성 중인 내용은 유지해요.' : ''}</p>
      <button type="button" className="button secondary" disabled={busy} onClick={() => { if (current() && latest) acknowledge(latest) }}>AI 초안 확인</button>
    </section>}
    {availableTerminal && !pending && <section className="content-card">
      <p>AI 정리 상태가 바뀌었어요. 최신 상태를 확인하고 직접 작성할 수 있어요.</p>
      <button type="button" className="button secondary" disabled={busy} onClick={() => { if (current() && latest) acknowledge(latest) }}>최신 상태 확인하고 직접 작성</button>
    </section>}
    {waiting && !manualOpen && !availableTerminal && <section className="content-card report-waiting">
      <h2>{runtime?.llm_mode === 'replay' ? '시연용 재생 초안을 준비하고 있어요.' : runtime?.llm_mode === 'live' ? 'AI가 보고를 정리하고 있어요.' : '보고 정리를 기다리고 있어요.'}</h2>
      <ReportElapsed savedAt={snapshot.created_at} />
      <button type="button" className="button secondary" onClick={() => setManualOpen(true)}>직접 확인하고 제출할 수도 있어요.</button>
    </section>}
    {nextWork}
    <form hidden={waiting && !manualOpen} className="content-card report-form" onSubmit={event => void submit(event)}>
      <h2>{!manual && snapshot.draft?.source === 'live' ? 'AI가 정리한 보고' : !manual && snapshot.draft?.source === 'replay' ? 'AI가 정리한 보고 · 재생' : '보고 내용 확인'}</h2>
      {pending && <><p className="notice" role="status">{t('workPendingHint')}</p>{blocked && <><p className="notice">{t('workIdempotencyConflict')}</p><button type="button" className="button secondary" onClick={() => { void onDone().catch(value => { if (current()) setError(value) }) }}>{t('refresh')}</button></>}</>}
      <p>{availableTerminal ? 'AI 정리 상태 확인이 필요해요.' : t(`reportAnalysis.${snapshot.analysis_status ?? 'queued'}`, { defaultValue: t('reportAnalysisUnknown') })}</p>
      {requiresReportReconfirmation(snapshot) && <p className="notice">{t('reportLegacyReconfirm')}</p>}
      {stale && <><p className="notice" role="status">{t('reportStale')}</p><button type="button" className="button secondary" disabled={busy || Boolean(pending)} onClick={() => void reload()}>{t('reportReloadDraft')}</button></>}
      <fieldset disabled={busy || Boolean(pending)}>
        <label>{t('reportActions')}<textarea value={actions} onChange={event => { setDirty(true); setActions(event.target.value) }} rows={2} maxLength={605} /></label>
        {invalidLists && <p role="alert">{t('reportListInvalid')}</p>}
        {conflict && <><label>{t('reportConfirmedResult')}<select value={result} onChange={event => { setDirty(true); setResult(event.target.value as Result) }}>
          <option value={snapshot.result} disabled>{t(`result.${snapshot.result}`)}</option><option value="needs_review">{t('result.needs_review')}</option>
        </select></label><p className="notice" role="status">{t('reportCompletionConflict')}</p></>}
        <details className="report-optional" open={Boolean(materials.trim() || structure.residual_issue.trim() || structure.revisit_needed || invalidLists) || undefined}>
          <summary>자재·남은 문제·확인 메모 (선택)</summary>
          <label>{t('reportMaterials')}<textarea value={materials} onChange={event => { setDirty(true); setMaterials(event.target.value) }} rows={2} maxLength={605} /></label>
          <label>{t('reportResidual')}<textarea value={structure.residual_issue} onChange={event => { setDirty(true); setStructure({ ...structure, residual_issue: event.target.value }) }} rows={2} maxLength={500} /></label>
          {snapshot.result !== 'revisit' && <label className="checkbox-label"><input type="checkbox" checked={structure.revisit_needed} onChange={event => { setDirty(true); setStructure({ ...structure, revisit_needed: event.target.checked }) }} />{t('reportRevisit')}</label>}
          {!conflict && snapshot.result !== 'needs_review' && <label>{t('reportConfirmedResult')}<select value={result} onChange={event => { setDirty(true); setResult(event.target.value as Result) }}>
            <option value={snapshot.result}>{t(`result.${snapshot.result}`)}</option><option value="needs_review">{t('result.needs_review')}</option>
          </select></label>}
          <label>{t('reportConfirmReason')}<textarea value={reason} onChange={event => { setDirty(true); setReason(event.target.value) }} rows={2} maxLength={1000} /></label>
        </details>
      </fieldset>
      {!pending && !busy && (staleRead || stale || invalidLists || (conflict && result !== 'needs_review')) && <p className="field-hint">{staleRead || stale ? '최신 보고를 다시 확인해 주세요.' : invalidLists ? '항목 수와 길이를 확인해 주세요.' : '결과를 확인 필요로 변경해 주세요.'}</p>}
      <button type="submit" className="button primary" disabled={busy || staleRead || Boolean(pending && blocked) || (!pending && (stale || invalidLists || (conflict && result !== 'needs_review')))}>{t(pending ? 'workPendingRetry' : 'reportConfirmSend')}</button>
      {Boolean(error) && <ErrorNotice error={error} />}
    </form>
    <details className="content-card report-original"><summary>내가 쓴 원문</summary><ReportSnapshot report={snapshot} auth={auth ?? { bearer: token }} onPhotoUpdate={() => { if (current()) void onDone().catch(value => { if (current()) setError(value) }) }} disabled={busy || Boolean(pending)} isCurrent={current} /></details>
  </div>
}
export function ReportApproval({ ticket, csrf, onDone, onLoadLatest }: { ticket: Ticket; csrf: string; onDone: (correction: boolean) => Promise<void>; onLoadLatest: () => Promise<Ticket> }) {
  const { t } = useTranslation('product')
  const runtime = useOptionalRuntime()
  const report = ticket.latest_report as Report | null
  const [snapshot, setSnapshot] = useState(report)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [conflict, setConflict] = useState(false)
  const [correctionSent, setCorrectionSent] = useState(false)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  if (!report || report.reviewed || !snapshot) return null
  const stale = snapshot.id !== report.id || snapshot.revision !== report.revision
  const correctionRequested = correctionSent || Boolean(snapshot.correction_requested_at || report.correction_requested_at)
  const ready = workerConfirmed(snapshot) && !correctionRequested
  async function reload() { setBusy(true); setError(null); try { const latest = await onLoadLatest(); setSnapshot(latest.latest_report as Report | null); setConflict(false); key.current = null } catch (value) { setError(value) } finally { setBusy(false) } }
  async function submit(correction = false, revisit = false) {
    if (!snapshot || !ready || stale || conflict || busy || !reason.trim()) return
    const body = { report_id: snapshot.id, expected_report_revision: snapshot.revision ?? 0, reason: reason.trim() }
    const path = correction ? `/tickets/${ticket.id}/reports/${snapshot.id}/request-correction` : `/tickets/${ticket.id}/review-report`
    const payload = correction ? { expected_report_revision: body.expected_report_revision, reason: body.reason } : revisit ? { ...body, review_outcome: 'revisit' } : body
    key.current = persistentKey(key.current, { path, payload }); setBusy(true); setError(null)
    try { await request<Ticket>(path, { method: 'POST', body: payload, key: key.current.key }, { csrf }); if (correction) setCorrectionSent(true); await onDone(correction) } catch (value) { setError(value); if (value instanceof ApiError && value.status === 409 && ['report_revision_conflict', 'report_not_reviewable', 'report_not_correctable'].includes(value.code)) setConflict(true) } finally { setBusy(false) }
  }
  return <section className="content-card action-card"><h2>{t('reportReview')}</h2><p>{correctionRequested ? '보완 요청을 보냈어요. 새 보고를 기다리고 있어요.' : t(ready ? 'reportWorkerConfirmed' : 'reportWorkerPending')}</p>{correctionRequested && <p>보완 요청 사유: {report.correction_reason || snapshot.correction_reason || reason}</p>}{ticket.photos?.length || snapshot.photos.length ? <div className="report-photo-comparison" aria-label="신고와 작업 사진"><section><h3>신고 때</h3>{ticket.photos?.length ? <Photos photos={ticket.photos} /> : <p className="field-hint">첨부된 사진이 없어요</p>}</section><section><h3>작업 후</h3>{snapshot.photos.length ? <Photos photos={snapshot.photos} /> : <p className="field-hint">첨부된 사진이 없어요</p>}</section></div> : <p className="field-hint">첨부된 사진이 없어요</p>}<h3>작업자 원문</h3><p className="ticket-body">{snapshot.text}</p>{snapshot.confirmed && <><h3>확인된 보고 내용</h3><StructureRead structure={snapshot.confirmed} workerReport /></>}<p>{t('reportConfirmedResult')}: {t(`result.${snapshot.confirmed_result ?? snapshot.result}`)}</p><p className="field-hint">{t('reportReviewHint')}</p><h3>{t('reportReplyPreview')}</h3><p className="ticket-body">{snapshot.reply_preview || t('reportPreviewPending')}</p><p className="field-hint">{t(runtime?.mode === 'service' ? runtime.mail_delivery === 'local' ? 'reportPreviewHintServiceLocal' : runtime.mail_delivery === 'disabled' ? 'reportPreviewHintServiceDisabled' : 'reportPreviewHintService' : 'reportPreviewHint')}</p>{(stale || conflict) && <><p className="notice">{t('reportStale')}</p><button type="button" className="text-button" disabled={busy} onClick={() => void reload()}>{t('reportReloadApproval')}</button></>}<div className="approval-reason-chips">{['보고 내용을 확인했어요', '사진으로 조치를 확인했어요'].map(item => <button key={item} type="button" className="text-button report-reason-chip" disabled={busy} onClick={() => setReason(item)}>{item}</button>)}</div><label>{t('reportApprovalReason')}<textarea rows={3} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} disabled={busy} /></label><button type="button" className="button secondary full-width" disabled={busy || stale || conflict || !ready || !reason.trim()} onClick={() => void submit(true)}>보완 요청</button><p className="field-hint">재방문으로 전환하면 작업자의 원래 보고를 보존하고, 재방문 필요로 회신해요. 이후 새 방문을 별도로 배정할 수 있어요.</p><button type="button" className="button secondary full-width" disabled={busy || stale || conflict || !ready || !reason.trim()} onClick={() => void submit(false, true)}>재방문으로 전환</button><button type="button" className="button primary full-width" disabled={busy || stale || conflict || !ready || !reason.trim() || !snapshot.reply_preview} onClick={() => void submit()}>{t(runtime?.mode === 'service' ? 'reportApproveNotify' : 'reportApprovePublish')}</button>{Boolean(error) && <ErrorNotice error={error} />}</section>
}
