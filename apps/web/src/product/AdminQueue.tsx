import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api, request } from './api'
import { displayLocation } from './locationDisplay'
import { workerConfirmed } from './reportTypes'
import { useRuntime } from './runtime'
import { useSession } from './session'
import { safetyUrgencies, urgencyLabel } from './safetyDisplay'
import { ErrorNotice, Loading } from './shared'
import { dateTime, displayDate, isLongWait, waiting } from './dateTime'
import type { Ticket } from './types'
import type { PlanningCapabilities } from './planningTypes'
import { useForegroundRefresh } from './useForegroundRefresh'
import { completionAt, completionLabel, matchesSearch, periodBounds } from './adminQueueFilters'
import type { Period, Sort } from './adminQueueFilters'
import './adminQueue.css'

type Group = 'todo' | 'all' | 'review' | 'assign' | 'report' | 'followup' | 'other' | 'completed' | 'information_waiting'
const groups: { id: Group; label: string }[] = [
  { id: 'todo', label: '할 일' }, { id: 'all', label: '전체' }, { id: 'review', label: '접수 검토' },
  { id: 'information_waiting', label: '신고자 답변 대기' }, { id: 'assign', label: '배정 필요' }, { id: 'report', label: '보고 검토' },
  { id: 'followup', label: '후속 확인' }, { id: 'other', label: '진행 중' },
  { id: 'completed', label: '완료' },
]
export function queueAction(ticket: Ticket): { group: Exclude<Group, 'all' | 'todo' | 'completed'>; label: string; next: string } {
  if (ticket.information_status === 'waiting') return { group: 'information_waiting', label: '답변 대기', next: '신고자의 답변을 기다리고 있어요.' }
  if (ticket.information_status === 'answered') return { group: 'review', label: '답변 도착', next: '답변을 확인해 주세요.' }
  if (ticket.latest_report && !ticket.latest_report.reviewed && !ticket.latest_report.correction_requested_at && workerConfirmed(ticket.latest_report)) return { group: 'report', label: '최신 보고 검토', next: '최신 보고와 남은 일을 확인해 주세요.' }
  if (ticket.visit_followup) return { group: 'followup', label: '미해결 후속', next: ticket.visit_followup === 'end_visit' ? '방문 종료를 확인해 주세요.' : '새 배정을 검토해 주세요.' }
  if (ticket.status === 'received') return { group: 'review', label: '접수 검토', next: '원문과 위치, 분류를 확인해 주세요.' }
  if (ticket.status === 'confirmed' && !ticket.assignment_id) return { group: 'assign', label: '배정 필요', next: '작업자 배정을 검토해 주세요.' }
  if (ticket.latest_report && !ticket.latest_report.reviewed) return { group: 'other', label: ticket.latest_report.correction_requested_at ? '보완 요청 중' : '작업자 보고 확인 대기', next: '보고 진행 상태를 확인해 주세요.' }
  if (ticket.status === 'completed') return { group: 'other', label: '완료된 신고', next: '승인된 보고와 회신을 확인해 주세요.' }
  return { group: 'other', label: '진행 상태 확인', next: '상세에서 현재 상태를 확인해 주세요.' }
}
function suggestedUrgency(ticket: Ticket): number | null {
  const draft = ticket.ai_draft
  return draft && !draft.stale && Number.isInteger(draft.ai_urgency) && draft.ai_urgency >= 0 && draft.ai_urgency <= 3 ? draft.ai_urgency : null
}
export function queueUrgency(ticket: Ticket): { label: string; tone: string; rank: number } {
  const value = ticket.safety ? safetyUrgencies(ticket.safety).current : null
  const suggested = suggestedUrgency(ticket)
  const rank = ticket.safety?.emergency_banner ? 0 : value ?? suggested ?? 2
  if (value != null) return { label: urgencyLabel(value), tone: ['urgent', 'priority', 'normal', 'minor'][value] ?? 'unknown', rank }
  if (suggested != null) return { label: `AI 제안 · ${urgencyLabel(suggested)}`, tone: `suggested ${['urgent', 'priority', 'normal', 'minor'][suggested]}`, rank }
  return { label: urgencyLabel(null), tone: 'unknown', rank }

}
export const queueWait = waiting
export function queueStageElapsed(ticket: Ticket): { label: string; at?: string } {
  const action = queueAction(ticket)
  if (action.group === 'report') return { label: '보고 검토 대기' }
  if (ticket.latest_report && !ticket.latest_report.reviewed) return { label: '보고 확인 대기' }
  if (ticket.status !== 'assigned' && ticket.status !== 'in_progress') return { label: `접수 후 ${waiting(ticket.created_at).replace(' 대기', '')}`, at: ticket.created_at }
  const events = ticket.timeline ?? []
  let assignment = -1
  for (let i = 0; i < events.length; i++) if (events[i].type === 'assigned' || events[i].type === 'reassigned') assignment = i
  if (ticket.status === 'in_progress') {
    const start = assignment < 0 ? undefined : events.slice(assignment + 1).reverse().find(event => event.type === 'started')
    if (start && Number.isFinite(Date.parse(start.at))) return { label: `작업 중 ${waiting(start.at).replace(' 대기', '')}`, at: start.at }
    return { label: '작업 진행 중' }
  }
  const assigned = events[assignment]
  if (assigned && Number.isFinite(Date.parse(assigned.at))) return { label: `배정 후 ${waiting(assigned.at).replace(' 대기', '')}`, at: assigned.at }
  return { label: '배정됨' }
}

function actionLinkLabel(ticket: Ticket, group: ReturnType<typeof queueAction>['group']): string {
  if (group === 'followup') return ticket.visit_followup === 'end_visit' ? '방문 종료 확인' : '재배정 검토'
  return { information_waiting: '상세 보기', review: ticket.information_status === 'answered' ? '답변 확인' : '확인하고 배정', assign: '작업자 배정', report: '보고 검토', other: '상세 보기' }[group]
}
function QueueSearch({ applied, navigationKey, onApply }: { applied: string; navigationKey: string; onApply: (value: string, replace?: boolean) => void }) {
  const [draft, setDraft] = useState(applied)
  const [previousKey, setPreviousKey] = useState(navigationKey)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [navigationKey])
  if (previousKey !== navigationKey) {
    setPreviousKey(navigationKey)
    setDraft(applied)
  }
  const change = (value: string) => {
    setDraft(value)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => onApply(value.trim(), true), 200)
  }
  return <div className="queue-search"><label htmlFor="queue-search">업무 검색</label><div><input ref={input} id="queue-search" type="search" value={draft} placeholder="접수 번호, 내용, 위치, 분류, 작업자" onChange={event => change(event.target.value)} /><button type="button" disabled={!draft} onClick={() => { if (timer.current) clearTimeout(timer.current); setDraft(''); onApply(''); input.current?.focus() }}>검색 지우기</button></div></div>
}
export function AdminQueue() {
  const { t } = useTranslation('product')
  const { cacheScope } = useSession()
  const runtime = useRuntime()
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const rawGroup = params.get('group') ?? 'todo'
  const filter: Group = groups.some(group => group.id === rawGroup) ? rawGroup as Group : 'todo'
  const rawPeriod = params.get('period') ?? 'all'
  const period: Period = ['all', 'today', '7', '30', 'custom'].includes(rawPeriod) ? rawPeriod as Period : 'all'
  const rawSort = params.get('sort') ?? 'urgent'
  const sort: Sort = ['urgent', 'oldest', 'newest'].includes(rawSort) ? rawSort as Sort : 'urgent'
  const appliedSearch = params.get('q') ?? ''
  const from = params.get('from') ?? '', to = params.get('to') ?? ''
  const bounds = periodBounds(period, from, to)
  const invalidRange = period === 'custom' && !bounds
  const [resetVersion, setResetVersion] = useState(0)
  const update = (changes: Record<string, string>, replace = false) => setParams(current => {
    const next = new URLSearchParams(current)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    return next
  }, { replace })
  const clear = () => {
    setResetVersion(value => value + 1)
    update({ q: '', group: '', period: '', from: '', to: '', sort: '', ops: '', category_group: '', category_code: '', building: '', urgency: '' })
  }
  const operationsFilter = params.get('ops') === '1' && !!from && !!to
  const operationsParams = new URLSearchParams({ operations_start: from, operations_end: to })
  for (const key of ['category_group', 'category_code', 'building', 'urgency']) { const value = params.get(key); if (value) operationsParams.set(key, value) }
  const query = useQuery({ queryKey: ['tickets', cacheScope, operationsFilter ? operationsParams.toString() : ''], queryFn: ({ signal }) => operationsFilter ? request<{ items: Ticket[] }>(`/tickets?${operationsParams}`, { signal }) : api.tickets(signal), refetchOnWindowFocus: false, retry: false })
  const travel = useQuery({ queryKey: ['planning-capabilities', cacheScope], queryFn: () => request<PlanningCapabilities & {travel_data_status?: string}>('/planning/capabilities'), enabled: runtime.mode === 'service', retry: false, refetchOnWindowFocus: false })
  const refresh = useForegroundRefresh(query)
  if (query.isPending) return <Loading />
  if (query.isError && !query.data) return <><div className="page-heading"><h1>담당자 업무함</h1></div><ErrorNotice error={query.error} retry={() => void query.refetch()} /></>
  const actionRank = { information_waiting: 4, report: 0, review: 1, assign: 2, followup: 3, other: 4 }
  const periodInactive = period !== 'all' && !['all', 'other', 'completed'].includes(filter)
  const dateApplies = (group: Group) => group === 'all' || group === 'other' || group === 'completed'
  const groupMatches = (item: Ticket, group: Group) => group === 'all' || (group === 'todo' ? item.status !== 'completed' : group === 'completed' ? item.status === 'completed' : group === 'other' ? item.status !== 'completed' && queueAction(item).group === 'other' : queueAction(item).group === group)
  const matches = (item: Ticket, group: Group) => {
    const created = Date.parse(item.created_at)
    return groupMatches(item, group) && matchesSearch(item, appliedSearch) && (!dateApplies(group) || !bounds || (Number.isFinite(created) && created >= bounds.start && created < bounds.end))
  }
  const items = query.data.items
  const visible = items.filter(item => matches(item, filter)).sort((a, b) => {
    const age = (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0)
    const order = sort === 'oldest' ? age : sort === 'newest' ? -age : queueUrgency(a).rank - queueUrgency(b).rank || actionRank[queueAction(a).group] - actionRank[queueAction(b).group] || age
    return order || a.id.localeCompare(b.id)
  })
  return <div className="admin-queue"><div className="queue-heading"><div><h1>담당자 업무함</h1></div><div>{runtime.mode !== 'public_demo' && <details><summary>더 보기</summary><Link className="text-button" to="/admin/operations">운영 현황</Link><Link className="text-button" to="/admin/planning">배정 계획</Link></details>}</div></div>
    {runtime.mode === 'service' && travel.data?.travel_data_status === 'unavailable' && <p className="notice">{t(travel.data.reasons.includes('institution_unverified') ? 'servicePlanningUnavailableRecommendation' : 'servicePlanningUnavailableManual')}</p>}<div className="queue-refresh-controls">
      <span title="화면을 보는 동안 5초마다 자동 갱신">{refresh.dataUpdatedAt ? <>5초마다 갱신 · <time dateTime={new Date(refresh.dataUpdatedAt).toISOString()}>{dateTime(new Date(refresh.dataUpdatedAt).toISOString(), 'full')}</time></> : '자동 갱신 중'}</span>
      <button type="button" className="refresh-icon" aria-label="새로고침" title="새로고침" aria-disabled={refresh.isFetching} onClick={() => { if (!refresh.isFetching) refresh.refresh() }}><span aria-hidden="true">↻</span></button>
      {(refresh.stale || refresh.isError) && <span role="status">정보가 오래됐을 수 있어요. 표시된 업무를 유지하고 있습니다.</span>}
    </div>
    <div className="queue-tools">{operationsFilter && <p role="status">운영 현황에서 선택한 조건이 적용됐어요: {from} ~ {to}{[params.get('category_group'), params.get('category_code'), params.get('building'), params.get('urgency') != null ? `긴급도 ${params.get('urgency')}` : null].filter(Boolean).map(value => ` · ${value}`)}. 전체 업무를 보려면 조건을 지워 주세요.</p>}<QueueSearch key={resetVersion} navigationKey={location.key} applied={appliedSearch} onApply={(value, replace) => update({ q: value }, replace)} />
      <nav className="queue-filters" aria-label="업무 유형">{groups.filter(group => ['todo', 'review', 'assign', 'report', 'other', 'completed'].includes(group.id)).map(group => <button key={group.id} type="button" className={items.filter(item => matches(item, group.id)).length === 0 ? 'empty-filter' : ''} aria-pressed={filter === group.id} onClick={() => update({ group: group.id === 'todo' ? '' : group.id })}>{group.label} <span>{items.filter(item => matches(item, group.id)).length}</span></button>)}<details className="queue-more"><summary>유형 더 보기{['all', 'information_waiting', 'followup'].includes(filter) ? ` · ${groups.find(group => group.id === filter)?.label}` : ''}</summary>{groups.filter(group => ['all', 'information_waiting', 'followup'].includes(group.id)).map(group => <button key={group.id} type="button" aria-pressed={filter === group.id} onClick={() => update({ group: group.id })}>{group.label} <span>{items.filter(item => matches(item, group.id)).length}</span></button>)}</details></nav>
      <div className="queue-controls"><label className="queue-period-select">접수 기간<select disabled={operationsFilter} value={period} onChange={event => update({ period: event.target.value === 'all' ? '' : event.target.value })}><option value="all">전체</option><option value="today">오늘</option><option value="7">7일</option><option value="30">30일</option><option value="custom">직접 지정</option></select></label><label className="queue-sort">정렬<select value={sort} onChange={event => update({ sort: event.target.value === 'urgent' ? '' : event.target.value })}><option value="urgent">긴급한 순</option><option value="oldest">오래 기다린 순</option><option value="newest">최근 접수 순</option></select></label>{periodInactive && <p className="queue-period-hint">선택한 기간은 전체·진행 중·기타·완료에 적용해요. 현재 유형에는 적용하지 않아요.</p>}{period === 'custom' && <div className="queue-dates"><label>시작 날짜<input disabled={operationsFilter} type="date" value={from} onChange={event => update({ from: event.target.value })} /></label><label>끝 날짜<input disabled={operationsFilter} type="date" value={to} onChange={event => update({ to: event.target.value })} /></label></div>}{invalidRange && <p role="alert" className="queue-range-error">올바른 시작 날짜와 끝 날짜를 선택해 주세요. 기간은 적용하지 않았어요.</p>}</div>
      <div className="queue-result"><span>{items.length}건 중 {visible.length}건 표시</span><button type="button" onClick={clear}>조건 지우기</button></div></div>
    {visible.length === 0 ? <section className="empty-state"><h2>{items.length ? '조건에 맞는 업무가 없어요' : '접수된 업무가 없어요'}</h2><p>{items.length ? '검색어나 조건을 바꿔 보세요.' : '새 접수가 들어오면 여기에 표시돼요.'}</p>{items.length > 0 && <button type="button" onClick={clear}>조건 지우기</button>}</section> : <div className="queue-list">{visible.map(ticket => {
      const action = queueAction(ticket)
      const urgency = queueUrgency(ticket)
      const elapsed = queueStageElapsed(ticket)
      const draft = ticket.ai_draft && !ticket.ai_draft.stale ? ticket.ai_draft : null
      const proposedUrgency = suggestedUrgency(ticket)
      const currentUrgency = ticket.safety ? safetyUrgencies(ticket.safety).current : null
      const emergencyBanner = ticket.status !== 'completed' && ticket.safety?.emergency_banner
      const highlight = ticket.status !== 'completed' && (urgency.tone === 'urgent' || (emergencyBanner && !ticket.safety?.override_reason))
      const summary = draft?.summary?.trim()
      const proposedCategory = ticket.category_code === 'UNKNOWN' && draft?.category_name?.trim() ? draft.category_name.trim() : null
      const excerpt = runtime.mode === 'public_demo' && Array.from(ticket.text).length > 120 ? <>{Array.from(ticket.text).slice(0, 120).join('')}… <span className="queue-excerpt-label">내용 일부</span></> : ticket.text
      return <article key={ticket.id}><Link className={`queue-item${highlight ? ' current-urgent' : ''}`} to={`/admin/t/${ticket.id}`} state={{ queueSearch: location.search }}>
        <div className="queue-meta"><span className={`queue-urgency ${ticket.status === 'completed' && urgency.tone === 'urgent' ? 'unknown' : urgency.tone}`}>{ticket.status === 'completed' && urgency.tone === 'urgent' ? '긴급도 · 긴급' : urgency.label}</span><span className="queue-action">{action.label}</span><span className="queue-wait">{ticket.status === 'completed' ? (completionAt(ticket) ? <time dateTime={completionAt(ticket)!} title={dateTime(completionAt(ticket)!, 'full')}>{completionLabel(ticket)}</time> : completionLabel(ticket)) : <>{elapsed.at ? <time dateTime={elapsed.at} title={dateTime(elapsed.at, 'full')}>{elapsed.label}</time> : elapsed.label} · <time dateTime={ticket.created_at} title={dateTime(ticket.created_at, 'full')}>{displayDate(ticket.created_at)} 접수</time></>}</span>{['review', 'assign', 'report', 'followup'].includes(action.group) && isLongWait(ticket.created_at, ticket.safety ? safetyUrgencies(ticket.safety).current : null) && <span className="queue-action" title="안내용 기준이며 처리 기한이 아닙니다">대기 길어짐</span>}</div>
        {ticket.safety?.rule_hits?.length
          ? <p className="queue-safety">원문에 위험 신호가 있어요.</p>
          : emergencyBanner && <p className="queue-safety">119 안내가 표시된 신고예요.</p>}
        {(ticket.safety?.rule_disagreement ?? (ticket.safety?.assessment_status === 'live_priority' && ticket.safety.ai_urgency != null && ticket.safety.rule_floor < ticket.safety.ai_urgency)) && <p className="queue-safety">AI와 규칙 판단이 달라요.</p>}
        {summary && <span className="queue-summary-label">AI 요약</span>}<h2>{summary || excerpt}</h2>
        <p className="queue-location">{displayLocation(ticket) || '위치 확인 필요'} · {proposedCategory ? <span className="queue-proposal">AI 제안: {proposedCategory} · 확인 전</span> : ticket.category_label || '분류 미정'}</p>
        {proposedUrgency != null && currentUrgency != null && <p className="queue-excerpt">AI 제안 긴급도 · {urgencyLabel(proposedUrgency)}</p>}
        {summary && <p className="queue-excerpt"><span className="queue-summary-label">신고 원문 </span>{excerpt}</p>}
        <span className="ticket-number">접수 번호 <span>{ticket.public_no.startsWith('B-') ? ticket.public_no.slice(0, 6) : ticket.public_no}</span></span><span className="queue-open">{actionLinkLabel(ticket, action.group)} <span aria-hidden="true">→</span></span>
      </Link></article>
    })}</div>}
  </div>
}
