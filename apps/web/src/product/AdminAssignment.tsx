import { useLocationCatalogSearch } from './useLocationCatalogSearch'
import { LocationSearch } from './LocationSearch'
import './schoolSettings.css'
import './assignmentReasonChips.css'
import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { useTranslation } from 'react-i18next'
import { ApiError, request } from './api'
import { ErrorNotice, Loading } from './shared'
import { useSession } from './session'
import { useRuntime } from './runtime'
import { workerDisplayLabel } from './workerDisplay'
import { safetyUrgencies } from './safetyDisplay'
import type { Assigned, Catalog, Ticket } from './types'
import { legacyCategories } from './ticketCategories'
import type { WorkerExecution } from './executionTypes'
import type { PlanningCapabilities } from './planningTypes'
import type { components } from '../api/schema'
import type { ManualPending } from './manualAssignmentStore'
import { currentAssignmentCandidates, locationDraftScope, suggestedAssignmentPlace, useAssignmentCandidates, useAssignmentDataset, type LocationDraftControl, type PlaceDraft } from './assignmentLocation'
type AtomicBody = components['schemas']['ConfirmAssignInput']
type AtomicPending = { body: AtomicBody; key: string }
type WorkerCandidate = components['schemas']['AssignmentWorkerCandidate']
type CandidateResult = components['schemas']['AssignmentWorkerCandidates']
function useWorkerRecommendation(ticket: Ticket, catalog: Catalog, category: string, location: string, allowException = false) {
  const { cacheScope } = useSession()
  const runtime = useRuntime()
  const context = (runtime.mode === 'demo' || runtime.mode === 'service') && category && category !== 'UNKNOWN' && location ? JSON.stringify([ticket.id, category, location]) : ''
  const [selected, setSelected] = useState<{ context: string; worker: string; manual: boolean } | null>(null)
  const query = useQuery({
    queryKey: ['worker-candidates', cacheScope, ticket.id, ticket.merge_version, catalog.catalog_revision, category, location],
    queryFn: () => request<CandidateResult>(`/tickets/${ticket.id}/worker-candidates?${new URLSearchParams({ category_code: category, location_code: location })}`),
    enabled: Boolean(context), retry: false, refetchOnWindowFocus: false,
  })
  const data = !query.isError && !query.isFetching && query.data && !query.data.context_reason && Array.isArray(query.data.candidates) && query.data.candidates.every(item => item && typeof item.worker_id === 'string' && typeof item.eligible === 'boolean' && Array.isArray(item.reasons) && item.reasons.every(reason => typeof reason === 'string')) ? query.data.candidates : null
  const valid = data?.filter(item => item.eligible && catalog.workers.some(worker => worker.id === item.worker_id)).sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity)) ?? []
  const first = valid[0]?.worker_id ?? ''
  useEffect(() => {
    // Pin the first fetched suggestion so later server rank changes do not replace it.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (context && first) setSelected(previous => previous?.context === context ? previous : { context, worker: first, manual: false })
  }, [context, first])
  const worker = selected?.context === context ? selected.worker : first
  const chosen = data?.find(item => item.worker_id === worker)
  const invalid = Boolean(data && (!chosen || (!chosen.eligible && !allowException)))
  const blocked = !allowException && (Boolean(context && (query.isFetching || ((!data) && selected?.context === context && !selected.manual))) || invalid)
  const status = !category || category === 'UNKNOWN' ? '분류를 고르면 추천 작업자를 보여드려요.' : !context ? '' : query.isError ? '추천을 불러오지 못했어요. 작업자를 직접 선택해 주세요.' : query.data?.context_reason ? '등록 위치와 분류를 확인할 수 없어 작업자를 직접 선택해 주세요.' : data && !valid.length ? '현재 조건에 적격인 작업자가 없어요.' : ''
  return { manuallySelected: selected?.context === context && selected.manual, retryContext: JSON.stringify([cacheScope, ticket.id, ticket.merge_version, catalog.catalog_revision, category, location]), contextReason: !query.isError && !query.isFetching ? query.data?.context_reason : null, worker, setWorker: (value: string) => setSelected({ context, worker: value, manual: true }), data, blocked, valid, status, failed: Boolean(context && query.isError), fetching: query.isFetching, retry: async () => context ? !(await query.refetch({ cancelRefetch: false })).isError : false, refresh: () => context ? query.refetch() : Promise.resolve() }
}
const categoryGroups = [
  ['ARCH_CIVIL', '건축'], ['CIVIL', '토목'], ['ELEC', '전기'], ['MECH', '기계'],
  ['other', '기타'],
] as const
function categoryGroup(domain: string | null | undefined) {
  return categoryGroups.some(([code]) => code === domain && code !== 'other') ? domain : 'other'
}
const reasonLabel = (reason: string) => ({ atomic_worker_day_off: '오늘 근무 아님', atomic_worker_outside_hours: '근무시간 아님', atomic_worker_lunch: '점심시간', atomic_worker_field_unavailable: '담당 분야 또는 일정 확인 필요', atomic_worker_schedule_stale: '일정 자료 갱신 필요', atomic_location_category_unconfirmed: '등록 위치와 분류 확인 필요', school_catalog_unavailable: '학교 자료 확인 필요' } as Record<string, string>)[reason] ?? '배정 조건 확인 필요'
const actionableRecoveryReasons = new Set(['atomic_worker_schedule_stale', 'atomic_worker_field_unavailable'])
const fieldLabel = (field: string | null) => ({ ELEC: '전기 분야', MECH: '설비 분야', ARCH_CIVIL: '건축 분야', CIVIL: '토목 분야', TRANSFER: '이관 분야', FACILITY: '시설 분야' } as Record<string, string>)[field ?? ''] ?? '담당 분야'
function WorkerOptions({ catalog, data, allowException = false }: { catalog: Catalog; data: WorkerCandidate[] | null; allowException?: boolean }) {
  const runtime = useRuntime()
  const names = catalog.workers.map(item => item.display_name)
  const workers = data ? [...catalog.workers].sort((a, b) => Number(data.find(x => x.worker_id === b.id)?.eligible ?? false) - Number(data.find(x => x.worker_id === a.id)?.eligible ?? false) || (data.find(x => x.worker_id === a.id)?.rank ?? Infinity) - (data.find(x => x.worker_id === b.id)?.rank ?? Infinity)) : catalog.workers
  return <>{workers.map(item => { const candidate = data?.find(x => x.worker_id === item.id); return <option key={item.id} value={item.id} disabled={Boolean(data && !allowException && !candidate?.eligible)}>{workerDisplayLabel(item.display_name, item.staff_code, names, runtime.mode)}{(item as typeof item & { is_local_test_account?: boolean }).is_local_test_account && ' · 시연용'}{candidate?.has_ongoing_visit ? ' · 다른 방문 진행 중' : ''}{candidate && !candidate.eligible ? ` · ${candidate.reasons.map(reasonLabel).join(', ')}` : ''}</option> })}</>
}
function RecommendationNotice({ catalog, data, worker, setWorker, valid, status, urgency, failed, fetching, retry, retryDisabled }: { setWorker: (value: string) => void; catalog: Catalog; data: WorkerCandidate[] | null; worker: string; valid: WorkerCandidate[]; status: string; urgency?: number | null; failed: boolean; fetching: boolean; retry: () => Promise<boolean>; retryDisabled: boolean }) {
  const runtime = useRuntime()
  const names = catalog.workers.map(item => item.display_name)
  const label = (item: WorkerCandidate) => {
    const canonical = catalog.workers.find(worker => worker.id === item.worker_id)
    const base = workerDisplayLabel(canonical?.display_name ?? item.display_name, canonical?.staff_code ?? item.staff_code, names, runtime.mode)
    const testAccount = (canonical as typeof canonical & { is_local_test_account?: boolean })?.is_local_test_account || (item as WorkerCandidate & { is_local_test_account?: boolean }).is_local_test_account
    return testAccount ? `${base} · 시연용` : base
  }
  const buttonRef = useRef<HTMLButtonElement>(null)
  const retrying = useRef(false)
  const focusEligible = useRef(false)
  const [holdingButton, setHoldingButton] = useState(false)
  const [retryFailed, setRetryFailed] = useState(false)
  useEffect(() => {
    if (failed && retryFailed) {
      // Keep the same button until the query has published its failure.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setRetryFailed(false)
    }
  }, [failed, retryFailed])
  useEffect(() => {
    const cancelFocus = () => { focusEligible.current = false }
    window.addEventListener('blur', cancelFocus)
    return () => window.removeEventListener('blur', cancelFocus)
  }, [])
  async function retryRecommendation() {
    if (retrying.current || fetching || retryDisabled) return
    retrying.current = true
    focusEligible.current = document.activeElement === buttonRef.current
    setHoldingButton(true)
    setRetryFailed(false)
    try {
      const success = await retry()
      setRetryFailed(!success)
      const button = buttonRef.current
      if (success && focusEligible.current && button?.isConnected && document.activeElement === button && !button.closest('fieldset:disabled')) {
        button.closest('form')?.querySelector<HTMLSelectElement>('select[data-recommendation-worker]')?.focus()
      }
    } finally {
      retrying.current = false
      focusEligible.current = false
      setHoldingButton(false)
    }
  }
  const selected = data?.find(item => item.worker_id === worker)
  function evidence(candidate: WorkerCandidate) {
    return <span>{fieldLabel(candidate.field_code)} · 미종료 업무 {candidate.open_assignments}건{candidate.has_ongoing_visit && ' · 다른 방문 진행 중'}{candidate.estimated_travel_min != null ? ` · 거점에서 약 ${candidate.estimated_travel_min}분(추정)` : candidate.same_building ? ' · 등록 거점 같은 건물' : candidate.same_region ? ' · 등록 거점 같은 권역' : ''}</span>
  }
  return <>
    {status && <p className="field-hint" role="status">{status}</p>}
    {(failed || holdingButton || retryFailed) && !retryDisabled && <button ref={buttonRef} type="button" className="text-button" aria-disabled={fetching || holdingButton} onBlur={() => { focusEligible.current = false }} onClick={() => void retryRecommendation()}>추천 다시 불러오기</button>}
    {valid.length > 0 && <div className="worker-recommendation" aria-label="추천 작업자">{valid.slice(0, 3).map((candidate, index) => <button type="button" key={candidate.worker_id} className="worker-recommendation-row" aria-pressed={worker === candidate.worker_id} disabled={retryDisabled} onClick={() => setWorker(candidate.worker_id)}><strong>{index + 1}순위{index === 0 ? ' · 추천' : ''}{worker === candidate.worker_id ? ' · 현재 선택' : ''}</strong><span>{label(candidate)}</span>{evidence(candidate)}</button>)}</div>}
    {data?.some(candidate => !candidate.eligible) && <details><summary>배정 조건 확인이 필요한 작업자</summary>{data.filter(candidate => !candidate.eligible).map(candidate => <p key={candidate.worker_id}>{label(candidate)} · {candidate.reasons.map(reasonLabel).join(', ')}</p>)}</details>}
    {selected?.has_ongoing_visit && <p className="notice">선택한 작업자는 다른 방문을 진행 중이에요. 배정할 수 있지만 이전 방문이 끝나야 이 작업을 시작할 수 있어요.</p>}
    {valid.length > 0 && valid.every(item => item.has_ongoing_visit === true) && <p className="notice">적격 작업자가 모두 다른 방문을 진행 중이에요. 배정 후 이전 방문이 끝날 때까지 기다려야 해요.</p>}
    {data && worker && !selected?.eligible && <p className="notice">선택한 작업자의 적격 여부를 다시 확인해 주세요.</p>}
    {data && valid.length > 0 && urgency != null && urgency <= 1 && !valid.some(item => item.open_assignments === 0) && <p className="notice">{urgency === 0 ? '긴급' : '우선'} 신고를 맡을 수 있는 작업자 중 진행 중인 업무가 0건인 사람이 없어요. 업무량을 확인해 주세요.</p>}
  </>
}
export function AdminAssignment({ ticket, catalog, planning, locationDraft, reason, onReason, onPending, onConfirm, onAttention, externalBusy = false, onDone, onLoadLatest }: { onAttention?: (value: boolean) => void; ticket: Ticket; catalog: Catalog; planning?: PlanningCapabilities; locationDraft?: LocationDraftControl; reason: string; onReason?: (value: string) => void; onPending?: (value: boolean) => void; onConfirm?: () => void; externalBusy?: boolean; onDone: (message: string, value?: Assigned, alreadyRefreshed?: boolean) => Promise<void>; onLoadLatest: () => Promise<{ ticket: Ticket; catalog: Catalog }> }) {
  const { cacheScope, manualAssignments } = useSession()
  useSyncExternalStore(manualAssignments.subscribe, manualAssignments.snapshot)
  const runtime = useRuntime()
  const [pending, setPending] = useState<AtomicPending | null>(null)
  const entry = manualAssignments.get(ticket.id)
  const manualPending = entry?.command ?? null
  const [manualBusy, setManualBusy] = useState(false)
  const [manualTicketId, setManualTicketId] = useState<string | null>(null)
  const atomic = manualTicketId !== ticket.id && !manualPending && (Boolean(pending) || (ticket.status === 'received' && runtime.mode !== 'prototype') || (ticket.status === 'confirmed' && !ticket.assignment_id && runtime.mode === 'service' && runtime.staff_password_auth))
  const [formAttention, setFormAttention] = useState(false)
  const query = useQuery({ queryKey: ['staff-execution', cacheScope, ticket.assignment_id], queryFn: () => request<WorkerExecution>(`/assignments/${ticket.assignment_id}/execution`), enabled: Boolean(ticket.assignment_id) && !atomic && ['confirmed', 'assigned', 'in_progress'].includes(ticket.status), refetchOnWindowFocus: false })
  useEffect(() => { onAttention?.(Boolean(!atomic && (formAttention || manualPending || manualBusy || query.isError))) }, [onAttention, atomic, formAttention, manualPending, manualBusy, query.isError])
  if (atomic) return <AtomicAssignment ticket={ticket} catalog={catalog} planning={planning} locationDraft={locationDraft} reason={reason} onReason={onReason} onPending={onPending} onConfirm={onConfirm} externalBusy={externalBusy} pending={pending} setPending={setPending} onManual={() => { setPending(null); onPending?.(false); setManualTicketId(ticket.id) }} onDone={onDone} onLoadLatest={onLoadLatest} />
  if (!manualPending && !['confirmed', 'assigned', 'in_progress'].includes(ticket.status)) return null
  if (!manualPending && ticket.assignment_id && query.isPending) return <Loading />
  if (!manualPending && ticket.assignment_id && query.isError) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  return <AssignmentForm onAttention={setFormAttention} key={manualPending ? (manualPending.body.expected_assignment_id ?? 'unassigned') : (ticket.assignment_id ?? 'unassigned')} ticket={ticket} externalBusy={externalBusy} pending={manualPending} store={manualAssignments} busy={manualBusy || Boolean(entry?.busy)} setBusy={setManualBusy} catalog={catalog} reason={reason} execution={query.data ?? null} reload={async () => { const latest = await query.refetch(); if (latest.isError || !latest.data) throw latest.error ?? new Error('refresh_failed'); return latest.data }} onDone={onDone} />
}
function AtomicAssignment({ ticket, catalog, planning, locationDraft, reason, onReason, onPending, onConfirm, externalBusy, pending, setPending, onManual, onDone, onLoadLatest }: { ticket: Ticket; catalog: Catalog; planning?: PlanningCapabilities; locationDraft?: LocationDraftControl; reason: string; onReason?: (value: string) => void; onPending?: (value: boolean) => void; onConfirm?: () => void; externalBusy: boolean; pending: AtomicPending | null; setPending: (value: AtomicPending | null) => void; onManual: () => void; onDone: (message: string, value?: Assigned, alreadyRefreshed?: boolean) => Promise<void>; onLoadLatest: () => Promise<{ ticket: Ticket; catalog: Catalog }> }) {
  const { session, cacheScope } = useSession()
  const runtime = useRuntime()
  const [categoryChoice, setCategory] = useState<string | null>(null)
  const reviewPreset = ticket.ai_draft && !ticket.ai_draft.stale ? 'AI 초안과 원문을 확인했어요' : '신고 원문을 확인했어요'
  const suggestedCategory = ticket.category_code === 'UNKNOWN' && !ticket.ai_draft?.stale && (catalog.categories ?? []).some(c => c.code === ticket.ai_draft?.category_code && c.code !== 'UNKNOWN' && !legacyCategories.has(c.code)) ? ticket.ai_draft!.category_code : ''
  const category = categoryChoice ?? (ticket.category_code !== 'UNKNOWN' ? ticket.category_code : suggestedCategory)
  const [choiceOverride, setChoice] = useState<string | null>(null)
  const choice = choiceOverride ?? (ticket.location?.state !== 'confirmed' && ticket.location?.candidates?.length === 1 ? ticket.location.candidates[0].code : '')
  const [ownPlaceDraft, setOwnPlaceDraft] = useState<PlaceDraft | null>(null)
  const scope = locationDraftScope(ticket, catalog.catalog_revision)
  const placeDraft = locationDraft ? locationDraft.value : ownPlaceDraft
  const placeEdited = placeDraft?.scope === scope
  const [locationText, setLocationText] = useState('')
  const usesRegisteredDataset = runtime.mode === 'service' || runtime.mode === 'public_demo' || (runtime.mode === 'demo' && catalog.catalog_revision != null)
  const datasetRequired = usesRegisteredDataset && ticket.location?.source_type !== 'service'
  const dataset = useAssignmentDataset(ticket, catalog.catalog_revision, datasetRequired)
  const candidates = useAssignmentCandidates(ticket, catalog.catalog_revision, datasetRequired)
  const locationSearch = useLocationCatalogSearch((dataset.data?.locations ?? []).filter(item => typeof item.code === 'string' && typeof item.label === 'string').map(item => ({ code: item.code, label: item.label })), session?.csrf_token, cacheScope, catalog.catalog_revision)
  const [datasetReviewed, setDatasetReviewed] = useState<string | null>(null)
  const datasetFingerprint = dataset.data ? `${dataset.data.revision}:${dataset.data.sha256}` : null
  if (datasetFingerprint && datasetReviewed === null) setDatasetReviewed(datasetFingerprint)
  const candidateData = currentAssignmentCandidates(ticket, candidates.data, catalog.catalog_revision)
  const confirmedPlace = ticket.location?.state === 'confirmed' && ticket.location.source_type === 'catalog'
    && ticket.location.catalog_revision === catalog.catalog_revision ? ticket.location.selected?.code ?? '' : ''
  const suggestedPlace = confirmedPlace || suggestedAssignmentPlace(candidateData)
  const matchingCandidate = datasetRequired && !dataset.isFetching && !dataset.isError && !candidates.isFetching
    && (confirmedPlace || !candidates.isError) && dataset.data?.locations?.some(item => item.code === suggestedPlace)
    ? suggestedPlace : ''
  const selectedDataset = placeEdited ? placeDraft.code : matchingCandidate
  const setDatasetCode = (code: string) => {
    if (code !== selectedDataset) onReason?.('')
    if (locationDraft) locationDraft.onChange({ scope, code })
    else setOwnPlaceDraft({ scope, code })
  }
  const registeredLocation = selectedDataset || (ticket.location?.state === 'confirmed' && ['catalog', 'service'].includes(ticket.location.source_type) ? ticket.location.selected?.code : '') || ''
  const recommendation = useWorkerRecommendation(ticket, catalog, legacyCategories.has(category) ? '' : category, registeredLocation)
  const { worker, setWorker } = recommendation
  const selectedWorker = catalog.workers.find(item => item.id === worker)
  const selectedLabel = selectedWorker ? workerDisplayLabel(selectedWorker.display_name, selectedWorker.staff_code, catalog.workers.map(item => item.display_name), runtime.mode) : ''
  const assignmentAction = ticket.status === 'confirmed' && runtime.mode === 'service' && runtime.staff_password_auth
    ? recommendation.valid[0]?.worker_id === worker && selectedLabel ? `추천 작업자 ${selectedLabel} 배정` : '선택한 작업자 배정'
    : '확인하고 배정'
  const visibleCandidates = recommendation.data?.filter(item => catalog.workers.some(worker => worker.id === item.worker_id)) ?? []
  const settingsReasons = visibleCandidates.flatMap(item => item.reasons)
  const showSettingsRecovery = (runtime.mode === 'service' || runtime.mode === 'demo') && Boolean(catalog.catalog_revision && catalog.catalog_revision > 0) && visibleCandidates.length > 0 && visibleCandidates.every(item => item.eligible === false && item.reasons.length > 0 && item.reasons.every(reason => actionableRecoveryReasons.has(reason)))
  const needsFieldReview = settingsReasons.includes('atomic_worker_field_unavailable')
  const [reviewed, setReviewed] = useState(() => ({ version: ticket.merge_version, catalog: catalog.catalog_revision ?? null }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [rejected, setRejected] = useState(false)
  const stale = reviewed.version !== ticket.merge_version || reviewed.catalog !== (catalog.catalog_revision ?? null)
  const noSchool = usesRegisteredDataset && !(catalog.catalog_revision && catalog.catalog_revision > 0)
  const unconfirmed = !category || category === 'UNKNOWN' || legacyCategories.has(category) || ((catalog.categories ?? []).length > 0 && !(catalog.categories ?? []).some(c => c.code === category)) || (ticket.location?.state !== 'confirmed' && !(choice === '__text' ? locationText.trim() : choice && ticket.location?.candidates?.some(item => item.code === choice)) && !selectedDataset)
  const textOnly = choice === '__text' && !selectedDataset
  const invalidDataset = datasetRequired && (dataset.isError || dataset.isFetching || (datasetReviewed !== null && datasetFingerprint !== datasetReviewed) || !dataset.data || !selectedDataset || !dataset.data.locations?.some(item => item.code === selectedDataset))
  const needsRegisteredReview = recommendation.contextReason === 'atomic_location_category_unconfirmed' && ticket.location?.source_type === 'service' && ticket.location?.state === 'confirmed' && (runtime.mode === 'service' || (runtime.mode === 'demo' && Boolean(catalog.catalog_revision && catalog.catalog_revision > 0)))
  const openReview = (id: string) => { const panel = document.getElementById(id); if (panel instanceof HTMLDetailsElement) panel.open = true }
  const serviceLocationMismatch = planning?.available && planning.data_kind === 'service' && ticket.location?.state === 'confirmed'
    && !selectedDataset && (ticket.location.source_type !== 'service' || ticket.location.dataset_hash !== planning.dataset_hash || ticket.location.dataset_revision !== planning.dataset_revision)
  async function reviewLatest() {
    if (pending && !rejected) return
    setBusy(true); setError(null)
    try { const latest = await onLoadLatest(); const [latestDataset] = await Promise.all([datasetRequired ? dataset.refetch() : Promise.resolve({ data: undefined }), datasetRequired ? candidates.refetch() : Promise.resolve(), recommendation.refresh()]); if (latestDataset.data) setDatasetReviewed(`${latestDataset.data.revision}:${latestDataset.data.sha256}`); setReviewed({ version: latest.ticket.merge_version, catalog: latest.catalog.catalog_revision ?? null }); setPending(null); onPending?.(false); setRejected(false) }
    catch (value) { setError(value) } finally { setBusy(false) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!session || busy || externalBusy || rejected || (!pending && (stale || noSchool || unconfirmed || textOnly || invalidDataset || needsRegisteredReview || recommendation.blocked || !catalog.workers.some(item => item.id === worker) || !reason.trim()))) return
    const command = pending ?? { body: { worker_id: worker, reason: reason.trim(), expected_ticket_version: reviewed.version, expected_catalog_revision: reviewed.catalog, ...(category !== ticket.category_code ? { category_code: category } : {}), ...(choice && !selectedDataset ? { location_choice: choice === '__text' ? { kind: 'text' as const, text: locationText.trim() } : { kind: 'candidate' as const, code: choice } } : {}), ...(selectedDataset && dataset.data ? { dataset_location: { code: selectedDataset, dataset_revision: dataset.data.revision, sha256: dataset.data.sha256 } } : {}) }, key: crypto.randomUUID() }
    setPending(command); onPending?.(true); setBusy(true); setError(null)
    try {
      const result = await request<{ ticket_id: string; assignment_id: string }>(`/tickets/${ticket.id}/confirm-assign`, { method: 'POST', body: command.body, key: command.key }, { csrf: session.csrf_token })
      setPending(null); onPending?.(false)
      await onDone('assignmentDone', { ticket, assignment_id: result.assignment_id, worker_token: null })
    } catch (value) { if (value instanceof ApiError && [400, 409, 422].includes(value.status)) { setRejected(true); onPending?.(false) };  setError(value) }
    finally { setBusy(false) }
  }
  const code = error instanceof ApiError ? error.code : ''
  const scheduleStale = code === 'atomic_worker_schedule_stale'
  const canOpenPlanning = runtime.mode === 'demo' || runtime.mode === 'service'
  const scheduleRecoveryPath = canOpenPlanning && catalog.catalog_revision != null && catalog.catalog_revision > 0 ? '/admin/settings#worker-schedules' : '/admin/planning'
  const explanation = scheduleStale ? '이 작업자의 일정이 이전 학교 자료를 기준으로 저장돼 있어요. 현재 자료로 일정을 다시 확인하고 저장해 주세요.' : code === 'school_catalog_unavailable' || noSchool ? '학교 업무 자료가 없어 한 번에 배정할 수 없어요.' : category && category !== 'UNKNOWN' && !legacyCategories.has(category) && unconfirmed ? '위치와 분류·담당 부서를 다시 확인하고 사유와 함께 저장해 주세요.' : code === 'atomic_worker_field_unavailable' ? '이 작업자의 담당 분야를 확인할 수 없어요.' : ''
  return <form className="content-card action-card assignment-form" onSubmit={event => void submit(event)}><h2>확인하고 배정</h2>{needsRegisteredReview && <p className="notice" role="status">등록 위치와 분류를 현재 학교 자료에서 다시 확인해 주세요. 먼저 <a href="#service-location-panel" onClick={() => openReview('service-location-panel')}>등록 위치 다시 확인</a>하고, 다음으로 <a href="#category-edit-panel" onClick={() => openReview('category-edit-panel')}>분류와 담당 부서 다시 확인</a> 후 사유와 함께 저장해 주세요.</p>}{serviceLocationMismatch && !needsRegisteredReview && <p className="notice">현재 계획 자료와 확인된 위치가 일치하는지 확인해 주세요. 계획 자료를 사용하는 배정이라면 <a href="#service-location">배정에 사용할 등록 위치</a>를 확인한 뒤 <a href="#category-review">분류와 담당 부서</a>도 사유와 함께 다시 저장해 주세요.</p>}{explanation && <p className="notice">{explanation} {scheduleStale ? canOpenPlanning ? <Link to={scheduleRecoveryPath}>일정 확인하기</Link> : '운영자에게 확인해 주세요.' : noSchool || code === 'school_catalog_unavailable' ? <Link to="/admin/settings">학교 자료 확인하기</Link> : '내용을 확인하고 다시 시도해 주세요.'}</p>}{stale && !pending && <p className="notice" role="status">신고 또는 학교 자료가 바뀌었어요. 최신 내용을 다시 검토해 주세요.</p>}{pending && !rejected && <p className="notice">전송 결과를 확인하지 못했어요. 같은 내용으로 다시 확인해 주세요.</p>}{(rejected || (!pending && (stale || (datasetReviewed !== null && datasetFingerprint !== datasetReviewed)))) && <button type="button" className="text-button" disabled={busy} onClick={() => void reviewLatest()}>최신 내용 불러오기</button>}<fieldset disabled={busy || externalBusy || Boolean(pending)}><label>분류<select value={category} onChange={event => setCategory(event.target.value)}><option value="">선택해 주세요</option>{legacyCategories.has(category) && <option value={category} disabled>현재 분류 · {catalog.categories?.find(item => item.code === category)?.label ?? ticket.category_label ?? category}</option>}{categoryGroups.map(([domain, label]) => { const items = (catalog.categories ?? []).filter(item => item.code !== 'UNKNOWN' && !legacyCategories.has(item.code) && categoryGroup(item.domain) === domain); return items.length ? <optgroup key={domain} label={label}>{items.map(item => <option key={item.code} value={item.code}>{item.label}</option>)}</optgroup> : null })}</select></label>{Boolean(suggestedCategory) && <small className="field-hint location-provenance">{categoryChoice === null ? 'AI 제안' : '담당자 수정'}</small>}{ticket.location?.state !== 'confirmed' && !datasetRequired && <label>위치 후보<select value={choice} onChange={event => setChoice(event.target.value)}><option value="">선택해 주세요</option>{ticket.location?.candidates?.map(item => <option key={item.code} value={item.code}>{item.label}</option>)}<option value="__text">직접 입력</option></select>{choice === '__text' && <input aria-label="위치 직접 입력" maxLength={300} value={locationText} onChange={event => setLocationText(event.target.value)} />}</label>}{datasetRequired && <div className="assignment-location-field"><LocationSearch label="배정에 사용할 등록 위치" placeholder="건물명이나 동 번호로 검색" items={locationSearch.items} disabled={busy || externalBusy || Boolean(pending)} searchStatus={locationSearch.status} onQuery={locationSearch.setQuery} value={selectedDataset} onChange={setDatasetCode} />{selectedDataset && <div className="detail-controls"><small className="field-hint">{placeEdited ? '담당자 선택' : confirmedPlace ? '확인된 위치' : '추천 위치 · 수정 가능'}</small><button type="button" className="text-button" onClick={() => setDatasetCode('')}>위치 선택 지우기</button></div>}</div>}{!datasetRequired && usesRegisteredDataset && ticket.location?.state === 'confirmed' && ticket.location.selected && <div className="assignment-confirmed-place"><p className="field-hint">배정에 사용할 등록 위치</p><strong>{ticket.location.selected.label}</strong><a className="text-button" href="#service-location" onClick={() => openReview('service-location-panel')}>등록 위치 변경</a></div>}<label>작업자<select data-recommendation-worker value={worker} onChange={event => setWorker(event.target.value)} required><option value="">선택해 주세요</option><WorkerOptions catalog={catalog} data={recommendation.data} /></select></label>{worker && recommendation.valid.length > 0 && <small className="field-hint location-provenance">{recommendation.manuallySelected ? '담당자 선택' : '추천 작업자 · 수정 가능'}</small>}<RecommendationNotice catalog={catalog} key={recommendation.retryContext} {...recommendation} status={needsRegisteredReview ? '' : recommendation.status} urgency={ticket.safety ? safetyUrgencies(ticket.safety).current : null} retryDisabled={busy || externalBusy || Boolean(pending)} />{runtime.mode === 'service' && recommendation.data && recommendation.valid.length === 0 && !showSettingsRecovery && <p className="notice">직원 분야와 일정을 확인해 주세요. <Link to="/admin/settings">학교 설정에서 확인</Link></p>}{showSettingsRecovery && !pending && !busy && !externalBusy && <p className="notice">{needsFieldReview ? '작업자의 업무 분야·소속과 일정을 확인해 주세요.' : '작업자의 일정을 현재 학교 자료로 확인해 주세요.'} <Link to={needsFieldReview ? '/admin/settings' : '/admin/settings#worker-schedules'}>학교 설정에서 확인</Link></p>}{onReason && <><label>확인·변경 사유<textarea value={reason} onChange={event => onReason(event.target.value)} rows={2} maxLength={1000} /></label><div className="assignment-reason-chips"><button type="button" className="text-button" onClick={() => onReason(reviewPreset)}>{reviewPreset}</button>{Boolean(ticket.photos?.length) && <button type="button" className="text-button" onClick={() => onReason('현장 사진으로 확인했어요')}>현장 사진으로 확인했어요</button>}</div></>}</fieldset>{!pending && !busy && <p className="field-hint">{rejected ? "요청이 거절됐어요. 최신 내용을 확인해 주세요." : stale ? "최신 신고와 학교 자료를 확인해 주세요." : noSchool ? "학교 자료를 먼저 확인해 주세요." : unconfirmed ? "분류와 위치를 선택해 주세요." : textOnly ? "직접 입력한 위치를 먼저 저장해 주세요." : invalidDataset || needsRegisteredReview ? "등록 위치와 분류를 확인해 주세요." : recommendation.blocked ? "추천 작업자와 배정 조건을 확인해 주세요." : !catalog.workers.some(item => item.id === worker) ? "작업자를 선택해 주세요." : !reason.trim() ? "확인 사유를 입력해 주세요." : "추천 조건을 확인한 뒤 배정해요."}</p>}<button className="button primary full-width" disabled={busy || externalBusy || rejected || (!pending && (stale || noSchool || unconfirmed || textOnly || invalidDataset || needsRegisteredReview || recommendation.blocked || !catalog.workers.some(item => item.id === worker) || !reason.trim()))} type="submit">{pending ? '전송 결과 다시 확인' : assignmentAction}</button>{textOnly && <p className="notice">직접 입력한 위치는 한 번에 배정할 수 없어요. 내용을 따로 저장하고 수동으로 확인해 주세요.</p>}{onConfirm && <button type="button" className="text-button" disabled={busy || externalBusy || Boolean(pending) || !reason.trim()} onClick={onConfirm}>접수만 확인</button>}{ticket.status === 'confirmed' && runtime.mode === 'service' && runtime.staff_password_auth && <><p className="field-hint">{rejected ? '배정 요청이 거절됐어요. 기존 배정 이력이나 변경된 내용을 확인한 뒤 수동 배정을 선택할 수 있어요.' : '추천 배정이 어려우면 수동으로 작업자를 확인해 배정할 수 있어요.'}</p><button type="button" className="text-button" disabled={busy || externalBusy || Boolean(pending && !rejected)} onClick={onManual}>수동 배정으로 전환</button></>}<p className="field-hint">AI 초안은 담당자가 확인한 뒤 반영돼요. 방문 일정은 이 화면에서 약속하지 않아요.</p>{Boolean(error) && <ErrorNotice error={error} />}</form>
}

function AssignmentForm({ ticket, catalog, reason, execution, reload, pending, store, busy, setBusy, externalBusy, onDone, onAttention }: { onAttention: (value: boolean) => void; externalBusy: boolean; pending: ManualPending | null; store: import('./manualAssignmentStore').ManualAssignmentStore; busy: boolean; setBusy: (value: boolean) => void; ticket: Ticket; catalog: Catalog; reason: string; execution: WorkerExecution | null; reload: () => Promise<WorkerExecution>; onDone: (message: string, value?: Assigned, alreadyRefreshed?: boolean) => Promise<void> }) {
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const { t } = useTranslation('product')
  const { session, cacheScope } = useSession()
  const client = useQueryClient()
  const [snapshot, setSnapshot] = useState(execution)
  const registeredLocation = ticket.location?.state === 'confirmed' && ['catalog', 'service'].includes(ticket.location.source_type) ? ticket.location.selected?.code ?? '' : ''
  const recommendation = useWorkerRecommendation(ticket, catalog, ticket.category_code, registeredLocation, true)
  const { worker, setWorker } = recommendation
  const [overrideWorker, setOverrideWorker] = useState<string | null>(null)
  const [overrideReason, setOverrideReason] = useState('')
  const [stop, setStop] = useState(false)
  const [blocked, setBlocked] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const stale = blocked || snapshot?.revision !== execution?.revision || snapshot?.active_plan_id !== execution?.active_plan_id
  const ongoing = snapshot && ['moving', 'working'].includes(snapshot.phase)
  useEffect(() => { onAttention(Boolean(stale || busy || error || pending || overrideWorker || overrideReason.trim() || stop || recommendation.manuallySelected)) }, [onAttention, stale, busy, error, pending, overrideWorker, overrideReason, stop, recommendation.manuallySelected])
  async function loadLatest() { setBusy(true); setError(null); try { setSnapshot(await reload()); setBlocked(false); setStop(false); setOverrideWorker(null); setOverrideReason('') } catch (value) { setError(value) } finally { setBusy(false) } }
  async function assign(event: FormEvent) {
    event.preventDefault(); if (!session || busy || externalBusy || (!pending && (stale || recommendation.blocked || snapshot?.active_plan_id || !worker || !reason.trim() || (overrideWorker === worker && !overrideReason.trim()) || (ongoing && !stop)))) return
    const command = pending ?? { ticketId: ticket.id, body: { worker_id: worker, reason: reason.trim(), expected_assignment_id: ticket.assignment_id ?? null, expected_execution_revision: snapshot?.revision ?? null, ...(ongoing ? { stop_current_visit: true } : {}), ...(overrideWorker === worker ? { override_reason: overrideReason.trim() } : {}) }, key: crypto.randomUUID() }
    if (!store.start(command)) return
    setBusy(true); setError(null)
    let result: Assigned
    try { result = await request<Assigned>(`/tickets/${command.ticketId}/assign`, { method: 'POST', body: command.body, key: command.key }, { csrf: session.csrf_token }) }
    catch (value) {
      if (value instanceof ApiError && value.status < 500 && value.status !== 408) {
        store.finish(command, true)
        if (value.status === 422 && value.code === 'assignment_field_override_required') { setOverrideWorker(command.body.worker_id); setOverrideReason(''); setError(null) }
        else { if (value.status === 409) setBlocked(true); setError(value) }
      } else { store.finish(command, false); setError(value) }
      setBusy(false); return
    }
    if (store.get(command.ticketId)?.command !== command) { store.finish(command, false); return }
    try {
      await Promise.all([client.invalidateQueries({ queryKey: ['ticket', cacheScope, command.ticketId] }, { throwOnError: true }), client.invalidateQueries({ queryKey: ['tickets', cacheScope] }, { throwOnError: true }), client.invalidateQueries({ queryKey: ['staff-execution', cacheScope] }, { throwOnError: true })])
      if (store.get(command.ticketId)?.command !== command) { store.finish(command, false); return }
      if (mounted.current) await onDone(pending && !result.worker_token ? 'assignmentRecovered' : 'assignmentDone', result, true)
    } catch { store.finish(command, false); setBusy(false); return }
    if (!store.finish(command, true)) return
    setBusy(false)
  }
  return <form className="content-card action-card assignment-form" onSubmit={event => void assign(event)}>
      <h2>{t('assignSection')} · 수동 선택</h2><p className="field-hint">담당자가 직접 선택해요. 진행 중인 작업자가 선택되면 기존 방문을 마친 뒤 시작해요. 분야가 다르면 예외 사유가 필요해요.</p>
      {pending ? <>
        <p className="notice">{busy ? '배정 결과를 확인하고 있어요.' : '전송 결과를 확인하지 못했어요. 같은 내용으로 다시 확인해 주세요.'}</p>
        <button className="button primary full-width" disabled={busy || externalBusy} type="submit">전송 결과 다시 확인</button>
      </> : snapshot?.active_plan_id ? <>
        <p className="notice">{t('assignmentPlanManaged')}</p>
        <Link className="button secondary" to={`/admin/planning/${snapshot.active_plan_id}`}>{t('assignmentOpenPlan')}</Link>
      </> : <>
        {ticket.assignment_id && <p className="field-hint">{t('serviceReassignHint')}</p>}
        {stale && <p className="notice">{t('executionControlStale')}</p>}
        {stale && <button type="button" className="text-button" disabled={busy} onClick={() => void loadLatest()}>{t('executionReloadControl')}</button>}
        <fieldset disabled={busy}>
          <label>{t('worker')}<select data-recommendation-worker value={worker} onChange={event => { setWorker(event.target.value); setOverrideWorker(null); setOverrideReason(''); setError(null) }} required>
            <option value="">{t('selectWorker')}</option>
            <WorkerOptions catalog={catalog} data={recommendation.data} allowException />
          </select></label>
          <RecommendationNotice catalog={catalog} key={recommendation.retryContext} {...recommendation} urgency={ticket.safety ? safetyUrgencies(ticket.safety).current : null} retryDisabled={busy || externalBusy || Boolean(pending)} />
          {overrideWorker === worker && <>
            <p className="notice" role="alert">담당 분야 또는 부서가 일치하지 않아요. 예외로 배정하려면 사유를 따로 남겨 주세요.</p>
            <label>분야 불일치 예외 배정 사유<textarea value={overrideReason} onChange={event => setOverrideReason(event.target.value)} rows={2} maxLength={1000} required /></label>
          </>}
          {ongoing && <>
            <p className="notice">{t('serviceAssignmentStopHint')}</p>
            <label className="checkbox-label"><input type="checkbox" checked={stop} onChange={event => setStop(event.target.checked)} />{t('serviceAssignmentStopConfirm')}</label>
          </>}
          {<p className="field-hint">{stale ? "최신 배정 상태를 확인해 주세요." : !worker ? "작업자를 선택해 주세요." : !reason.trim() ? "확인 사유를 입력해 주세요." : overrideWorker === worker && !overrideReason.trim() ? "분야 예외 사유를 입력해 주세요." : ongoing && !stop ? "기존 방문 중지 여부를 확인해 주세요." : "서버가 직원 자격과 배정 조건을 다시 확인해요."}</p>}<button className="button primary full-width" disabled={stale || recommendation.blocked || !worker || !reason.trim() || (overrideWorker === worker && !overrideReason.trim()) || Boolean(ongoing && !stop)} type="submit">{t(ticket.assignment_id ? 'reassign' : 'assign')}</button>
        </fieldset>
      </>}
      {Boolean(error) && <ErrorNotice error={error} />}
    </form>
}
