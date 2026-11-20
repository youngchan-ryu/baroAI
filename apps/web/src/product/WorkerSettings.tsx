import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation } from 'react-router'
import type { components } from '../api/schema'
import { ApiError, request } from './api'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import { WorkerHoursFields } from './WorkerHoursFields'
import { formatOffset, hoursDraft, parseHours } from './workingHours'

const fields: Record<string, string> = { ELEC: '전기', MECH: '설비', ARCH_CIVIL: '건축', CIVIL: '토목', FACILITY: '시설 관리', TRANSFER: '이관' }
const endpoint = '/worker/my-settings'
type Settings = components['schemas']['WorkerOwnSettings']

export function WorkerSettings() {
  const { cacheScope, session } = useSession()
  const { hash } = useLocation()
  const section = useRef<HTMLElement>(null)
  useEffect(() => { if (hash === '#my-settings') section.current?.scrollIntoView() }, [hash])
  const queryKey = ['worker-own-settings', cacheScope]
  const query = useQuery({ queryKey, queryFn: ({ signal }) => request<Settings>(endpoint, { signal }), retry: false, enabled: Boolean(session) })
  if (!session) return null
  const accessEnded = query.error instanceof ApiError && [401, 403].includes(query.error.status)
  return <section id="my-settings" className="content-card" ref={section}>
    <h2>내 분야·근무 설정</h2>
    {query.isPending && <p>설정을 불러오고 있어요.</p>}
    {query.isError && <ErrorNotice error={query.error} retry={() => void query.refetch()} />}
    {query.data && !accessEnded && <WorkerSettingsDetails
      settings={query.data} queryKey={queryKey}
      refresh={async () => {
        const latest = await query.refetch()
        if (latest.isError || !latest.data) throw latest.error ?? new Error('schedule_missing')
        return latest.data
      }}
      refreshing={query.isFetching}
    />}
  </section>
}

function WorkerSettingsDetails({ settings, queryKey, refresh, refreshing }: {
  settings: Settings
  queryKey: string[]
  refresh: () => Promise<Settings>
  refreshing: boolean
}) {
  const { session } = useSession()
  const client = useQueryClient()
  const [editingRevision, setEditingRevision] = useState<number | null>(null)
  const [draft, setDraft] = useState(() => hoursDraft())
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const hours = parseHours(draft)
  const revisionConflict = error instanceof ApiError && error.code === 'schedule_revision_conflict'
  const stale = editingRevision !== null && (editingRevision !== settings.schedule_revision || revisionConflict)
  const reservation = error instanceof ApiError && error.code === 'worker_has_plan_reservations'

  function begin(latest: Settings) {
    setEditingRevision(latest.schedule_revision)
    setDraft(hoursDraft(latest.hours))
    setError(null)
    setSaved(false)
  }
  async function restart() {
    setBusy(true)
    try { begin(await refresh()) } catch (failure) { setError(failure) } finally { setBusy(false) }
  }
  async function save(event: FormEvent) {
    event.preventDefault()
    if (!session || busy || editingRevision === null || !hours || stale || refreshing) return
    setBusy(true)
    setError(null)
    try {
      await client.cancelQueries({ queryKey })
      const body: components['schemas']['WorkerOwnHoursInput'] = { expected_revision: editingRevision, hours }
      const updated = await request<Settings>(endpoint, { method: 'PUT', body }, { csrf: session.csrf_token })
      client.setQueryData(queryKey, updated)
      setEditingRevision(null)
      setSaved(true)
    } catch (failure) { setError(failure) } finally { setBusy(false) }
  }

  return <>
    <p>담당 분야 · {settings.field_codes.length ? settings.field_codes.map(code => fields[code] ?? '기타 분야').join(' · ') : '등록된 분야 없음'}</p>
    <p className="field-hint">배정 분야를 바꾸려면 담당자에게 요청해 주세요.</p>
    {settings.profile_stale && <p className="notice">담당 분야가 이전 학교 자료 기준이에요. 담당자에게 확인해 주세요.</p>}
    {settings.schedule_stale && <p className="notice">근무 시간이 배정에 반영되려면 담당자가 최신 위치 자료로 일정을 확인해야 해요.</p>}
    {!settings.schedule_configured && <p>담당자가 근무 일정을 등록하면 여기서 시간을 수정할 수 있어요.</p>}
    {settings.schedule_configured && <>
      {settings.hours ? <>
        <p>근무 {settings.hours.shift_start}–{settings.hours.shift_end} (서울 시간)</p>
        <p>{settings.hours.lunch_start ? `점심 ${settings.hours.lunch_start}–${settings.hours.lunch_end}` : '점심시간 미설정'}</p>
      </> : settings.shift_start_min != null && settings.shift_end_min != null && <p>기존 근무 범위 · 계획 기준시각 + {formatOffset(settings.shift_start_min)}–{formatOffset(settings.shift_end_min)} (시:분 경과)</p>}
      {!settings.schedule_enabled && <p className="field-hint">이 일정은 순회 배정에 사용하지 않아요.</p>}
      {editingRevision === null && <button type="button" className="button secondary" disabled={refreshing || !settings.schedule_revision} onClick={() => begin(settings)}>근무·점심시간 수정</button>}
    </>}
    {Boolean(settings.days_off.length) && <p>등록된 휴무일 · {settings.days_off.join(', ')}</p>}
    {saved && <p role="status">근무·점심시간을 저장했어요.</p>}
    {editingRevision !== null && <form className="worker-hours-form" onSubmit={event => void save(event)}>
      <p className="field-hint">서울 시간 기준으로 입력해 주세요. 점심을 설정하려면 근무시간 안에서 시작과 종료를 모두 입력해 주세요.</p>
      {stale && <div className="notice" role="alert"><p>일정이 바뀌었어요. 입력한 내용을 확인하고 최신 일정으로 다시 시작해 주세요.</p><button type="button" className="text-button" disabled={busy || refreshing} onClick={() => void restart()}>최신 일정으로 다시 시작</button></div>}
      {reservation && <p className="notice" role="alert">승인된 방문이 남아 있어요. 방문을 마치거나 담당자와 일정을 확인한 뒤 수정해 주세요.</p>}
      {Boolean(error) && !revisionConflict && !reservation && <ErrorNotice error={error} />}
      <fieldset disabled={busy}>
        <WorkerHoursFields draft={draft} onChange={setDraft} />
        <div className="form-actions">
          <button type="submit" className="button primary" disabled={stale || refreshing || !hours}>근무 시간 저장</button>
          <button type="button" className="button secondary" onClick={() => { setEditingRevision(null); setError(null) }}>취소</button>
        </div>
      </fieldset>
    </form>}
  </>
}
