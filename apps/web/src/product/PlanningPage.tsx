import { workerDistinctLabel } from './workerDisplay'
import { useRuntime } from './runtime'
import type { SchoolWorker } from './SchoolSettingsPage'
import { ScheduleEditor } from './WorkerScheduleEditor'
import type { components } from '../api/schema'
import { InsertionResult } from './InsertionPage'
import { PlanReview, PlanningHandoff } from './PlanResults'
import { useRef, useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router'
import { useTranslation } from 'react-i18next'
import { api, persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice, Loading, StatusBadge } from './shared'
import { displayLocation } from './locationDisplay'
import type { Ticket } from './types'
import type { DispatchPlan, PlanApproval, PlanningCapabilities, PlanRequest, WorkerSchedule } from './planningTypes'

function minute(value: string, maximum: number) { return /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= 0 && Number(value) <= maximum }
export function PlanningPage() {
  const { t } = useTranslation('product')
  const { cacheScope } = useSession()
  const runtime = useRuntime()
  const { planId } = useParams()
  const query = useQuery({ queryKey: ['planning-capabilities', cacheScope], queryFn: () => request<PlanningCapabilities & {travel_data_status?: string}>('/planning/capabilities'), refetchOnWindowFocus: false })
  if (query.isPending) return <Loading />
  if (query.isError) return planId ? <ExistingPlanRecovery planId={planId} /> : <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if (!query.data.available && planId) return <ExistingPlanRecovery planId={planId} />
  if (runtime.mode === 'service' && query.data.travel_data_status === 'unavailable') return (
    <section className="content-card">
      <h1>오늘의 순회 계획</h1>
      <p>이동시간 자료를 사용할 수 없어 순회 계획을 만들 수 없어요.</p>
      <Link to="/admin/queue">업무함으로</Link>
    </section>
  )
  return <><Link className="back-link" to="/admin/queue">← {t('queue')}</Link><div className="page-heading"><h1>{t('planningTitle')}</h1><p>{t('planningLead')}</p><p>여러 신고와 작업자의 일정을 함께 검토하는 계획이에요. 한 신고의 작업자 추천은 업무함의 신고 상세에서 확인해요.</p></div>{!query.data.available ? <section className="content-card"><h2>{t('planningUnavailable')}</h2><p>{t(`planningReason.${query.data.reasons[0]}`, { defaultValue: t('planningUnavailableHint') })}</p><Link to="/admin/queue">업무함에서 배정</Link></section> : <PlanningLoader capabilities={query.data} />}</>
}
function ExistingPlanRecovery({ planId }: { planId: string }) {
  const { t } = useTranslation('product')
  const runtime = useRuntime()
  const { cacheScope } = useSession()
  const query = useQuery({ queryKey: ['planning-recovery', cacheScope, planId], queryFn: () => request<PlanApproval>(`/dispatch-plans/${encodeURIComponent(planId)}`), refetchOnWindowFocus: false })
  const [latest, setLatest] = useState<PlanApproval | null>(null)
  if (query.isPending) return <Loading />
  if (query.isError) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  const opened = latest ?? query.data
  return <><Link className="back-link" to="/admin/queue">← {t('queue')}</Link><p className="notice">{t('planningRecoveryOnly')}</p><PlanReview key={opened.plan.id} initial={opened.plan} tickets={[]} workers={[]} allowApprove={false} onApproved={setLatest} /><section className="content-card"><h2>{t('planningRecoveryAssignments')}</h2>{opened.assignments.map(item => <p key={item.assignment_id}>{item.assignment_id} · {item.worker_id} · {item.ticket_id} · {t(item.revoked ? (runtime.mode === 'service' ? 'servicePlanningRevokedAssignment' : 'planningRevokedAssignment') : 'planningRecoveryActive')}</p>)}</section></>
}
function PlanningLoader({ capabilities }: { capabilities: PlanningCapabilities }) {
  const { planId } = useParams()
  const { cacheScope } = useSession()
  const query = useQuery({ queryKey: ['planning-inputs', cacheScope, planId ?? 'new'], queryFn: async () => ({ tickets: (await api.tickets()).items, schedules: await request<WorkerSchedule[]>('/worker-schedules'), profiles: await request<SchoolWorker[]>('/admin/workers'), catalog: await request<components['schemas']['app__school_catalog_routes__CatalogView']>('/admin/catalog'), opened: planId ? await request<PlanApproval>(`/dispatch-plans/${encodeURIComponent(planId)}`) : null }), refetchOnWindowFocus: false })
  if (query.isPending) return <Loading />
  if (query.isError) return planId ? <ExistingPlanRecovery planId={planId} /> : <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if ((query.data.opened?.plan as (DispatchPlan & {kind?: string}) | undefined)?.kind === 'insertion') return <InsertionResult key={planId} initial={query.data.opened!} tickets={query.data.tickets} workers={query.data.schedules} />
  return <PlanningWorkspace key={planId ?? 'new'} capabilities={capabilities} initial={query.data} />
}
function PlanningWorkspace({ capabilities: initialCapabilities, initial }: { capabilities: PlanningCapabilities; initial: { tickets: Ticket[]; schedules: WorkerSchedule[]; profiles: SchoolWorker[]; catalog: { fields: {code: string; name: string}[] } | null; opened: PlanApproval | null } }) {
  const { t } = useTranslation('product')
  const runtime = useRuntime()
  const { session } = useSession()
  const navigate = useNavigate()
  const [capabilities, setCapabilities] = useState(initialCapabilities)
  const [datasetReviewNeeded, setDatasetReviewNeeded] = useState(false)
  const [sources, setSources] = useState(initial)
  const normalAssumptions = initial.opened && 'worker_ids' in initial.opened.plan.assumptions ? initial.opened.plan.assumptions : null
  const [workers, setWorkers] = useState<string[]>(normalAssumptions?.worker_ids ?? [])
  const [tickets, setTickets] = useState<Record<string, { selected: boolean; service: string; release: string; deadline: string; source: string }>>(Object.fromEntries((normalAssumptions?.tickets ?? []).map(item => [item.ticket_id, { selected: true, service: String(item.service_min), release: String(item.release_min), deadline: String(item.deadline_min), source: item.assumptions_source }])))
  const [base, setBase] = useState(() => { const iso = initial.opened?.plan.plan_start_at; if (!iso) return ''; const date = new Date(iso); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) })
  const [preset, setPreset] = useState(normalAssumptions?.preset_id ?? '')
  const [policy, setPolicy] = useState<PlanRequest['policy']>(initial.opened?.plan.assumptions.policy ?? 'ortools')
  const [mode, setMode] = useState<PlanRequest['mode']>(normalAssumptions?.mode ?? 'domain')
  const [assumptions, setAssumptions] = useState(initial.opened?.plan.assumptions.assumptions_version ?? '')
  const [seconds, setSeconds] = useState(String(initial.opened?.plan.assumptions.solver_seconds ?? 1))
  const [plan, setPlan] = useState<DispatchPlan | null>(initial.opened?.plan ?? null)
  const [approval, setApproval] = useState<PlanApproval | null>(initial.opened?.plan.status === 'approved' ? initial.opened : null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const selected = Object.entries(tickets).filter(([, value]) => value.selected)
  const valid = Boolean(capabilities.available && !datasetReviewNeeded && (capabilities.data_kind !== 'service' || workers.every(id => { const data = sources.schedules.find(item => item.worker_id === id)?.data; return data?.dataset_hash === capabilities.dataset_hash && data?.dataset_revision === capabilities.dataset_revision })) && base && Number.isFinite(new Date(base).getTime()) && preset && assumptions.trim() && workers.length && selected.length && Number(seconds) > 0 && Number(seconds) <= 3 && selected.every(([, value]) => minute(value.service, 1440) && Number(value.service) > 0 && minute(value.release, capabilities.max_offset_min) && minute(value.deadline, capabilities.max_offset_min) && Number(value.deadline) >= Number(value.release) && value.source.trim()))
  async function reloadSources() { const wasReviewNeeded = datasetReviewNeeded; setBusy(true); setError(null); setDatasetReviewNeeded(true); try { const latestCapabilities = await request<PlanningCapabilities>('/planning/capabilities'); const changed = latestCapabilities.data_kind !== capabilities.data_kind || latestCapabilities.dataset_hash !== capabilities.dataset_hash || latestCapabilities.dataset_revision !== capabilities.dataset_revision; setCapabilities(latestCapabilities); if (latestCapabilities.available) { const latestTickets = await api.tickets(); const schedules = await request<WorkerSchedule[]>('/worker-schedules'); const profiles = await request<SchoolWorker[]>('/admin/workers'); const catalog = await request<components['schemas']['app__school_catalog_routes__CatalogView']>('/admin/catalog'); setSources(previous => ({ ...previous, tickets: latestTickets.items, schedules, profiles, catalog })); if (!changed && !wasReviewNeeded) setDatasetReviewNeeded(false) } } catch (value) { setError(value) } finally { setBusy(false) } }
  async function generate(event: FormEvent) {
    event.preventDefault(); if (!session || busy || !valid) return
    const body: PlanRequest = { plan_start_at: new Date(base).toISOString(), preset_id: preset, ...(capabilities.data_kind === 'service' ? { dataset_hash: capabilities.dataset_hash, dataset_revision: capabilities.dataset_revision } : {}), worker_ids: workers, tickets: selected.map(([ticket_id, value]) => ({ ticket_id, service_min: Number(value.service), release_min: Number(value.release), deadline_min: Number(value.deadline), assumptions_source: value.source.trim() })), mode, policy, assumptions_version: assumptions.trim(), solver_seconds: Number(seconds) }
    key.current = persistentKey(key.current, body); setBusy(true); setError(null)
    try { const result = await request<DispatchPlan>('/dispatch-plans', { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); setPlan(result); setApproval(null); key.current = null; navigate(`/admin/planning/${encodeURIComponent(result.id)}`, { replace: true }) } catch (value) { setError(value) } finally { setBusy(false) }
  }
  function ticketDraft(id: string) { return tickets[id] ?? { selected: false, service: '', release: '', deadline: '', source: '' } }
  function editTicket(id: string, patch: Partial<ReturnType<typeof ticketDraft>>) { setTickets(previous => ({ ...previous, [id]: { ...ticketDraft(id), ...patch } })) }
  return <><section className="notice"><h2>{t(capabilities.data_kind === 'service' ? 'planningServiceDataset' : 'planningSynthetic')}</h2><details><summary>계산 기준</summary><p>{t(capabilities.data_kind === 'service' ? 'planningServiceDatasetHint' : 'planningAssumptionsHint')}</p></details></section><section className="content-card"><h2>{t('planningWorkers')}</h2><p className="field-hint">{t('planningOffsetHint')}</p><button type="button" className="text-button" disabled={busy} onClick={() => void reloadSources()}>{t('planningReloadSources')}</button>{datasetReviewNeeded && <label className="notice checkbox-label"><input type="checkbox" disabled={!capabilities.available || busy} onChange={event => { if (event.target.checked) setDatasetReviewNeeded(false) }} />{t('planningDatasetReconfirm')}</label>}{sources.schedules.map(schedule => <ScheduleEditor key={schedule.worker_id} displayLabel={workerDistinctLabel(schedule.worker_id, schedule.display_name, sources.schedules.map(item => ({id: item.worker_id, name: item.display_name})), runtime.mode)} value={schedule} profile={(Array.isArray(sources.profiles) ? sources.profiles : []).find(item => item.worker_id === schedule.worker_id)} fields={Array.isArray(sources.catalog?.fields) ? sources.catalog.fields : undefined} capabilities={capabilities} onSaved={updated => setSources(previous => ({ ...previous, schedules: previous.schedules.map(item => item.worker_id === updated.worker_id ? updated : item) }))} />)}</section><form className="content-card planning-form" onSubmit={event => void generate(event)}><h2>{t('planningInputTitle')}</h2><fieldset disabled={busy}><label>{t('planningBase')}<input type="datetime-local" required value={base} onChange={event => setBase(event.target.value)} /></label><p className="field-hint">{t('planningBaseHint')}</p><p className="field-hint">{t('planningDeviceTimezone', { timezone: Intl.DateTimeFormat().resolvedOptions().timeZone })}</p><label>{t('planningPreset')}<select required value={preset} onChange={event => setPreset(event.target.value)}><option value="">{t('planningChoose')}</option>{capabilities.presets.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>{capabilities.presets.filter(item => item.id === preset).map(item => <p key={item.id} className="notice">{item.provenance}</p>)}<label>{t('planningAssumptions')}<input required value={assumptions} maxLength={100} onChange={event => setAssumptions(event.target.value)} /></label><label>{t('planningPolicy')}<select value={policy} onChange={event => setPolicy(event.target.value as PlanRequest['policy'])}>{['ortools', 'fifo', 'urgency_fifo', 'nearest'].map(item => <option key={item} value={item}>{t(`planningPolicyLabel.${item}`)}</option>)}</select></label><label>{t('planningMode')}<select value={mode} onChange={event => setMode(event.target.value as PlanRequest['mode'])}>{['domain', 'region', 'region_support'].map(item => <option key={item} value={item}>{t(`planningModeLabel.${item}`)}</option>)}</select></label><p className="field-hint">{t('planningConstraintHint')}</p><label>{t('planningSolverLimit')}<input type="number" min="0.1" max="3" step="0.1" value={seconds} onChange={event => setSeconds(event.target.value)} /></label><fieldset><legend>{t('planningSelectWorkers')}</legend>{sources.schedules.map(worker => <label className="checkbox-label" key={worker.worker_id}><input type="checkbox" disabled={!worker.data?.enabled} checked={workers.includes(worker.worker_id)} onChange={event => setWorkers(previous => event.target.checked ? [...previous, worker.worker_id] : previous.filter(id => id !== worker.worker_id))} />{workerDistinctLabel(worker.worker_id, worker.display_name, sources.schedules.map(item => ({id: item.worker_id, name: item.display_name})), runtime.mode)}{!worker.data?.enabled && ` · ${t('planningWorkerDisabled')}`}</label>)}</fieldset><h3>{t('planningSelectTickets')}</h3><p>{t('planningExplicitTimes')} 이전 학교 자료로 분류된 신고는 현재 분류를 담당자가 다시 확인하기 전까지 추천에서 제외돼요. 업무함에서 직접 배정할 수 있어요.</p>{sources.tickets.map(ticket => { const value = ticketDraft(ticket.id); return <article className="planning-ticket" key={ticket.id}><label className="checkbox-label"><input type="checkbox" checked={value.selected} onChange={event => editTicket(ticket.id, { selected: event.target.checked })} />{ticket.public_no} · {ticket.category_label}</label><StatusBadge status={ticket.status} /><p>{displayLocation(ticket) || t('locationUnknown')}</p>{value.selected && <div className="planning-fields"><label>{t('planningServiceMinutes')} · {ticket.public_no}<input type="number" min="1" max="1440" value={value.service} onChange={event => editTicket(ticket.id, { service: event.target.value })} required /></label><label>{t('planningReleaseMinutes')} · {ticket.public_no}<input type="number" min="0" max={capabilities.max_offset_min} value={value.release} onChange={event => editTicket(ticket.id, { release: event.target.value })} required /></label><label>{t('planningDeadlineMinutes')} · {ticket.public_no}<input type="number" min="0" max={capabilities.max_offset_min} value={value.deadline} onChange={event => editTicket(ticket.id, { deadline: event.target.value })} required /></label><label>{t('planningTimeSource')} · {ticket.public_no}<input value={value.source} maxLength={500} required onChange={event => editTicket(ticket.id, { source: event.target.value })} /></label></div>}</article> })}<p className="field-hint">{t('planningNoAssignmentYet')}</p><button className="button primary" type="submit" disabled={!valid}>{t('planningGenerate')}</button></fieldset>{Boolean(error) && <ErrorNotice error={error} />}</form>{plan && <PlanReview key={plan.id} initial={plan} tickets={sources.tickets} workers={sources.schedules} allowApprove={capabilities.available && !datasetReviewNeeded && plan.input.data_kind === capabilities.data_kind && (plan.input.data_kind !== 'service' || (plan.input.dataset_hash === capabilities.dataset_hash && plan.input.dataset_revision === capabilities.dataset_revision))} onApproved={value => { setPlan(value.plan); setApproval(value.plan.status === 'approved' ? value : null) }} />}{approval && <PlanningHandoff approval={approval} tickets={sources.tickets} workers={sources.schedules} onReload={async () => { const latest = await request<PlanApproval>(`/dispatch-plans/${approval.plan.id}`); setPlan(latest.plan); setApproval(latest.plan.status === 'approved' ? latest : null); return latest }} />}</>
}
