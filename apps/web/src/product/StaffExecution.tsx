import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ApiError, request } from './api'
import { waiting } from './dateTime'
import { useSession } from './session'
import { ExecutionControls } from './ExecutionControls'
import { ErrorNotice, Loading } from './shared'
import type { WorkerExecution } from './executionTypes'
import type { Ticket } from './types'

function useStaffExecution(assignmentId: string) {
  const { cacheScope } = useSession()
  const queryKey = ['staff-execution', cacheScope, assignmentId]
  const query = useQuery({ queryKey, queryFn: ({ signal }) => request<WorkerExecution>(`/assignments/${assignmentId}/execution`, { signal }), refetchOnWindowFocus: false })
  const stopped = query.data?.phase === 'stopped' && query.error instanceof ApiError && query.error.code === 'assignment_not_current'
  return { query, queryKey, cacheScope, stopped }
}

function visitPhaseStartedAt(ticket: Ticket, execution: WorkerExecution) {
  switch (execution.phase) {
    case 'queued': return ticket.timeline?.filter(event => event.type === 'assigned' || event.type === 'reassigned').at(-1)?.at
    case 'moving': return execution.departed_at
    case 'working': return execution.started_at
    case 'visit_ended': return execution.visit_ended_at
    case 'stopped': {
      const lastVisitEvent = ticket.timeline?.filter(event => ['assigned', 'reassigned', 'visit_stopped'].includes(event.type)).at(-1)
      return lastVisitEvent?.type === 'visit_stopped' ? lastVisitEvent.at : undefined
    }
  }
}

export function StaffVisitSummary({ ticket, assignmentId }: { ticket: Ticket; assignmentId: string }) {
  const { t } = useTranslation('product')
  const { query, stopped } = useStaffExecution(assignmentId)
  const execution = !query.isError || stopped ? query.data : undefined
  const phaseStartedAt = execution && visitPhaseStartedAt(ticket, execution)
  const [currentTime, setCurrentTime] = useState(Date.now)
  useEffect(() => {
    if (!phaseStartedAt) return
    const timer = window.setInterval(() => setCurrentTime(Date.now()), 15_000)
    return () => window.clearInterval(timer)
  }, [phaseStartedAt])
  const now = Math.max(currentTime, query.dataUpdatedAt)
  const elapsed = phaseStartedAt && Date.parse(phaseStartedAt) <= now ? waiting(phaseStartedAt, now).replace(' 대기', ' 경과') : '확인 전'
  const confirmedPlace = ticket.location?.state === 'confirmed' ? ticket.location.selected?.label : null
  return <section className="content-card admin-visit-summary" aria-labelledby="visit-summary-title">
    <h2 id="visit-summary-title">방문 요약</h2>
    <dl className="ticket-facts">
      <div><dt>담당 작업자</dt><dd>{ticket.worker_name || '확인 필요'}</dd></div>
      <div><dt>확정 위치</dt><dd>{confirmedPlace || '확정 위치 미지정'}</dd></div>
      <div><dt>현재 방문 단계</dt><dd>{execution ? t(`executionPhase.${execution.phase}`) : query.isPending ? '불러오는 중' : t('executionPhase.unknown')}</dd></div>
      <div><dt>경과 시간</dt><dd>{elapsed}</dd></div>
    </dl>
  </section>
}

export function StaffExecution({ assignmentId }: { assignmentId: string }) {
  const client = useQueryClient()
  const { query, queryKey, cacheScope, stopped } = useStaffExecution(assignmentId)
  const { t } = useTranslation('product')
  if (query.isPending) return <Loading />
  if (query.isError && !stopped) return query.error instanceof ApiError && query.error.code === 'assignment_not_current' ? <section className="content-card"><p>{t('executionNotCurrent')}</p></section> : <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  return <section className="content-card">{query.data?.active_plan_id && <Link className="button secondary" to={`/admin/planning/${query.data.active_plan_id}`}>{t('assignmentOpenPlan')}</Link>}<ExecutionControls key={assignmentId} assignmentId={assignmentId} execution={query.data!} onUpdated={value => { client.setQueryData(queryKey, value); void client.invalidateQueries({ queryKey: ['tickets', cacheScope] }); void client.invalidateQueries({ queryKey: ['ticket', cacheScope] }) }} allowRemaining={Boolean(query.data?.active_plan_id)} onReload={async () => { const latest = await query.refetch(); if ((latest.isError && !(latest.data?.phase === 'stopped' && latest.error instanceof ApiError && latest.error.code === 'assignment_not_current')) || !latest.data) throw latest.error ?? new Error('refresh_failed'); return latest.data }} /></section>
}
