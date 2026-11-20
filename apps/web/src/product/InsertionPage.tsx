import { workerDistinctLabel } from './workerDisplay'
import { useOptionalRuntime } from './runtime'
import { dateTime } from './dateTime'
import { useRef, useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router'
import { useTranslation } from 'react-i18next'
import { api, ApiError, persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice, Loading } from './shared'
import { PlanReview, PlanningHandoff } from './PlanResults'
import type { Ticket } from './types'
import type { DispatchPlan, InsertionRequest, PlanApproval, PlanningCapabilities, PlanRequest, WorkerSchedule } from './planningTypes'
function time(value: string) { return dateTime(value, 'full') }
type Draft = { selected: boolean; service: string; release: string; deadline: string; source: string }
const empty: Draft = { selected: false, service: '', release: '', deadline: '', source: '' }
const integer = (value: string, min: number, max: number) => /^-?\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= min && Number(value) <= max
export function InsertionPage() {
  const { planId = '' } = useParams()
  const runtime = useOptionalRuntime()
  const { cacheScope } = useSession()
  const query = useQuery({ queryKey: ['insertion-inputs', cacheScope, planId], queryFn: async () => { const capabilities = await request<PlanningCapabilities & {travel_data_status?: string}>('/planning/capabilities'); if (!capabilities.available || capabilities.data_kind === 'service') return { capabilities, opened: null, tickets: [], schedules: [] }; return { capabilities, opened: await request<PlanApproval>(`/dispatch-plans/${encodeURIComponent(planId)}`), tickets: (await api.tickets()).items, schedules: await request<WorkerSchedule[]>('/worker-schedules') } }, refetchOnWindowFocus: false })
  const { t } = useTranslation('product')
  if (query.isPending) return <Loading />
  if (query.isError) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if (runtime?.mode === 'service' && query.data.capabilities.travel_data_status === 'unavailable') return <section className="content-card"><h1>일괄 배정안 사용 안내</h1><p>{t(query.data.capabilities.reasons.includes('institution_unverified') ? 'servicePlanningUnavailableRecommendation' : 'servicePlanningUnavailableManual')}</p><Link to="/admin/queue">업무함으로</Link></section>
  if (!query.data.capabilities.available || query.data.capabilities.data_kind === 'service') return <section className="content-card"><h1>{t('planningUnavailable')}</h1><p>{t(query.data.capabilities.data_kind === 'service' ? 'insertionServiceUnavailable' : 'planningServiceBlocked')}</p></section>
  if (!query.data.opened) return null
  return <InsertionForm key={planId} initial={query.data.opened} tickets={query.data.tickets} workers={query.data.schedules} />
}
function InsertionForm({ initial, tickets, workers }: { initial: PlanApproval; tickets: Ticket[]; workers: WorkerSchedule[] }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const navigate = useNavigate()
  const [parent, setParent] = useState(initial)
  const [snapshotRevision, setSnapshotRevision] = useState(initial.plan.revision)
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [assumptions, setAssumptions] = useState('')
  const [policy, setPolicy] = useState<PlanRequest['policy']>('ortools')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [blocked, setBlocked] = useState(false)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const selected = Object.entries(drafts).filter(([, value]) => value.selected)
  const valid = selected.length > 0 && selected.length <= 20 && assumptions.trim() && selected.every(([, value]) => integer(value.service, 1, 1440) && integer(value.release, -100000, 100000) && integer(value.deadline, -100000, 100000) && Number(value.deadline) >= Number(value.release) && value.source.trim())
  async function reload(rebase: boolean) { const latest = await request<PlanApproval>(`/dispatch-plans/${parent.plan.id}`); setParent(latest); if (rebase) { setSnapshotRevision(latest.plan.revision); setBlocked(false); key.current = null } return latest }
  async function loadLatest() { setBusy(true); setError(null); try { await reload(true) } catch (value) { setError(value) } finally { setBusy(false) } }
  async function generate(event: FormEvent) {
    event.preventDefault(); if (!session || busy || blocked || !valid || parent.plan.status !== 'approved' || snapshotRevision !== parent.plan.revision) return
    const body: InsertionRequest = { expected_parent_revision: snapshotRevision, urgent_tickets: selected.map(([ticket_id, value]) => ({ ticket_id, service_min: Number(value.service), release_min: Number(value.release), deadline_min: Number(value.deadline), assumptions_source: value.source.trim() })), policy, solver_seconds: 1, assumptions_version: assumptions.trim() }
    key.current = persistentKey(key.current, body); setBusy(true); setError(null)
    try { const plan = await request<DispatchPlan>(`/dispatch-plans/${parent.plan.id}/insertions`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); navigate(`/admin/planning/${encodeURIComponent(plan.id)}`) } catch (value) { if (value instanceof ApiError && value.status === 409) setBlocked(true); setError(value) } finally { setBusy(false) }
  }
  function edit(id: string, patch: Partial<Draft>) { setDrafts(previous => ({ ...previous, [id]: { ...(previous[id] ?? empty), ...patch } })) }
  return <><Link className="back-link" to={`/admin/planning/${parent.plan.id}`}>← {t('insertionParent')}</Link><h1>{t('insertionTitle')}</h1><section className="notice"><h2>{t('planningSynthetic')}</h2><p>{t('insertionScope')}</p><details><summary>계산 기준</summary><p>{t('insertionTimingHint')}</p></details><p>{t('insertionParentBase')}: {time(parent.plan.plan_start_at)} (KST)</p></section><button type="button" className="text-button" disabled={busy} onClick={() => void loadLatest()}>{t('insertionReload')}</button>{(blocked || snapshotRevision !== parent.plan.revision) && <p className="notice">{t('insertionStale')}</p>}{parent.plan.status !== 'approved' && <p className="notice">{t('insertionParentUnavailable')}</p>}<PlanningHandoff approval={parent} tickets={tickets} workers={workers} onReload={() => reload(false)} executionOnly /><form className="content-card planning-form" onSubmit={event => void generate(event)}><h2>{t('insertionSelectUrgent')}</h2><fieldset disabled={busy}><label>{t('planningAssumptions')}<input value={assumptions} maxLength={100} onChange={event => setAssumptions(event.target.value)} /></label><label>{t('planningPolicy')}<select value={policy} onChange={event => setPolicy(event.target.value as PlanRequest['policy'])}>{['ortools', 'fifo', 'urgency_fifo', 'nearest'].map(item => <option key={item} value={item}>{t(`planningPolicyLabel.${item}`)}</option>)}</select></label>{tickets.filter(ticket => ticket.status === 'confirmed' && !ticket.assignment_id).map(ticket => { const value = drafts[ticket.id] ?? empty; return <article className="planning-ticket" key={ticket.id}><label className="checkbox-label"><input type="checkbox" checked={value.selected} onChange={event => edit(ticket.id, { selected: event.target.checked })} />{ticket.public_no} · {ticket.category_label}</label>{value.selected && <div className="planning-fields"><label>{t('planningServiceMinutes')} · {ticket.public_no}<input type="number" min={1} max={1440} value={value.service} onChange={event => edit(ticket.id, { service: event.target.value })} /></label><label>{t('insertionRelease')} · {ticket.public_no}<input type="number" min={-100000} max={100000} value={value.release} onChange={event => edit(ticket.id, { release: event.target.value })} /></label><label>{t('insertionDeadline')} · {ticket.public_no}<input type="number" min={-100000} max={100000} value={value.deadline} onChange={event => edit(ticket.id, { deadline: event.target.value })} /></label><label>{t('planningTimeSource')} · {ticket.public_no}<input value={value.source} maxLength={500} onChange={event => edit(ticket.id, { source: event.target.value })} /></label></div>}</article> })}<button className="button primary" disabled={!valid || blocked || snapshotRevision !== parent.plan.revision || parent.plan.status !== 'approved'}>{t('insertionGenerate')}</button></fieldset>{Boolean(error) && <ErrorNotice error={error} />}</form></>
}
export function InsertionResult({ initial, tickets, workers }: { initial: PlanApproval; tickets: Ticket[]; workers: WorkerSchedule[] }) {
  const [current, setCurrent] = useState(initial)
  return <><InsertionComparison plan={current.plan} tickets={tickets} workers={workers} /><PlanReview key={current.plan.id} initial={current.plan} tickets={tickets} workers={workers} onApproved={setCurrent} />{current.plan.status === 'approved' && <PlanningHandoff approval={current} tickets={tickets} workers={workers} onReload={async () => { const latest = await request<PlanApproval>(`/dispatch-plans/${current.plan.id}`); setCurrent(latest); return latest }} />}</>
}
export function InsertionComparison({ plan, tickets, workers }: { plan: DispatchPlan; tickets: Ticket[]; workers: WorkerSchedule[] }) {
  const runtime = useOptionalRuntime()
  const { t } = useTranslation('product')
  const metadata = plan.insertion
  if (!metadata) return <p>{t('insertionMetadataUnavailable')}</p>
  const date = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? time(value) : t('unknown')
  const diff = Array.isArray(metadata.diff) ? metadata.diff.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && typeof item.ticket_id === 'string' && typeof item.kind === 'string' && ['fixed', 'retained', 'replaced', 'new', 'unassigned'].includes(item.kind)) : []
  const assumptions = 'urgent_tickets' in plan.assumptions ? plan.assumptions : null
  return <section className="content-card"><h2>{t('insertionComparison')}</h2><p className="notice">{t('planningSynthetic')}</p><details><summary>계산 기준</summary><p>{t('insertionTimingHint')}</p></details><dl><dt>{t('insertionParentBase')}</dt><dd>{date(metadata.parent_base)} (KST)</dd><dt>{t('insertionChildBase')}</dt><dd>{date(plan.plan_start_at)} (KST)</dd><dt>{t('insertionShift')}</dt><dd>{Number.isSafeInteger(metadata.offset_shift_min) ? String(metadata.offset_shift_min) : t('unknown')}{t('planningMinuteUnit')}</dd><dt>{t('insertionNotBefore')}</dt><dd>{date(metadata.dispatch_not_before_at)} (KST)</dd></dl>{assumptions && Array.isArray(assumptions.urgent_tickets) && <section><h3>{t('insertionOriginalInput')}</h3><p>{assumptions.assumptions_version}</p><ul>{assumptions.urgent_tickets.map(item => <li key={item.ticket_id}>{tickets.find(ticket => ticket.id === item.ticket_id)?.public_no ?? t('planningTicket')}<p>{t('planningServiceMinutes')}: {item.service_min} · {t('insertionRelease')}: {item.release_min} · {t('insertionDeadline')}: {item.deadline_min}</p><p>{item.assumptions_source}</p></li>)}</ul></section>}{['fixed', 'retained', 'replaced', 'new', 'unassigned'].map(kind => <section key={kind}><h3>{t(`insertionKind.${kind}`)}</h3>{!diff.some(item => item.kind === kind) && <p>{t('insertionNone')}</p>}<ul>{diff.filter(item => item.kind === kind).map((item, index) => <li key={index}><Link to={`/admin/t/${encodeURIComponent(String(item.ticket_id))}`}>{tickets.find(ticket => ticket.id === item.ticket_id)?.public_no ?? t('planningTicket')}</Link>{typeof item.worker_id === 'string' && <> · {workers.filter(worker => worker.worker_id === item.worker_id).map(worker => workerDistinctLabel(worker.worker_id, worker.display_name, workers.map(peer => ({id: peer.worker_id, name: peer.display_name})), runtime?.mode ?? 'service'))[0] ?? t('worker')}</>}{typeof item.reason === 'string' && <p>{t(`planningReason.${item.reason}`, { defaultValue: t('planningReasonUnknown') })}</p>}{item.urgent_escalation === true && <p className="notice notice-error">{t('planningUrgentEscalation')}</p>}</li>)}</ul><p className="field-hint">{t(`insertionKindHint.${kind}`)}</p></section>)}</section>
}
