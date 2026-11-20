import { LocationSearch } from './LocationSearch'
import { useRef, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import type { SchoolWorker } from './SchoolSettingsPage'
import type { PlanningCapabilities, WorkerSchedule } from './planningTypes'
import { WorkerHoursFields } from './WorkerHoursFields'
import { formatOffset, hoursDraft, offsetMinutes, parseHours } from './workingHours'

export type ScheduleContext = Pick<PlanningCapabilities, 'data_kind' | 'dataset_hash' | 'dataset_revision' | 'max_offset_min' | 'nodes' | 'regions'>

function offsetDraft(value: WorkerSchedule) {
  return value.data
    ? { start: formatOffset(value.data.shift_start_min), end: formatOffset(value.data.shift_end_min) }
    : { start: '', end: '' }
}

export function ScheduleEditor({ value, profile, fields, capabilities, onSaved, catalogRevision, contextValid = true, displayLabel }: { value: WorkerSchedule; profile?: SchoolWorker; fields?: {code: string; name: string}[]; capabilities: ScheduleContext; onSaved: (value: WorkerSchedule) => void; catalogRevision?: number; contextValid?: boolean; displayLabel?: string }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const [snapshot, setSnapshot] = useState(value)
  const [context, setContext] = useState(() => ({ catalogRevision, hash: capabilities.dataset_hash, revision: capabilities.dataset_revision }))
  const [latest, setLatest] = useState<WorkerSchedule | null>(null)
  const empty = { enabled: false, skills: [] as string[], region: '', start_node: '', end_node: '', assumptions_source: '' }
  const [draft, setDraft] = useState(value.data ?? empty)
  const [offset, setOffset] = useState(() => offsetDraft(value))
  const [useLocalTime, setUseLocalTime] = useState(!value.data || Boolean(value.data.hours))
  const [hours, setHours] = useState(() => hoursDraft(value.data?.hours))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const stale = snapshot.revision !== value.revision || snapshot.profile_revision !== value.profile_revision ||
    context.catalogRevision !== catalogRevision || context.hash !== capabilities.dataset_hash ||
    context.revision !== capabilities.dataset_revision || !contextValid
  const profileRevision = snapshot.profile_revision
  const profileReady = Boolean(profile && fields &&
    (catalogRevision === undefined || profile.catalog_revision === catalogRevision) &&
    profileRevision === profile.revision && draft.skills.every(skill => profile.field_codes.includes(skill)))
  const parsedHours = parseHours(hours)
  const start = offsetMinutes(offset.start)
  const end = offsetMinutes(offset.end)
  const validTimes = useLocalTime ? Boolean(parsedHours) : start >= 0 && start < end && end <= capabilities.max_offset_min
  const valid = !latest && profileReady && validTimes && capabilities.nodes.some(node => node.code === draft.start_node) && capabilities.nodes.some(node => node.code === draft.end_node) && Boolean(draft.assumptions_source.trim()) && draft.skills.length > 0 && capabilities.regions.includes(draft.region)
  function replace(latest: WorkerSchedule) {
    setContext({ catalogRevision, hash: capabilities.dataset_hash, revision: capabilities.dataset_revision })
    setSnapshot(latest)
    setDraft(latest.data ?? empty)
    setOffset(offsetDraft(latest))
    setHours(hoursDraft(latest.data?.hours))
    setUseLocalTime(!latest.data || Boolean(latest.data.hours))
    key.current = null
    onSaved(latest)
  }
  async function reload() { setBusy(true); setError(null); try { const list = await request<WorkerSchedule[]>('/worker-schedules'); const latest = list.find(worker => worker.worker_id === value.worker_id); if (!latest) throw new Error('schedule_missing'); setLatest(latest) } catch (error) { setError(error) } finally { setBusy(false) } }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!session || busy || stale || !valid) return
    const shift = useLocalTime && parsedHours ? {
      shift_start_min: snapshot.data?.shift_start_min ?? offsetMinutes(parsedHours.shift_start),
      shift_end_min: snapshot.data?.shift_end_min ?? offsetMinutes(parsedHours.shift_end),
      hours: parsedHours,
    } : { shift_start_min: start, shift_end_min: end }
    const body = {
      expected_revision: snapshot.revision, expected_profile_revision: profileRevision ?? 0,
      enabled: draft.enabled, skills: draft.skills, region: draft.region, ...shift,
      start_node: draft.start_node, end_node: draft.end_node, assumptions_source: draft.assumptions_source.trim(),
      ...(capabilities.data_kind === 'service' ? { dataset_hash: capabilities.dataset_hash, dataset_revision: capabilities.dataset_revision } : {}),
    }
    key.current = persistentKey(key.current, body); setBusy(true); setError(null)
    try { replace(await request<WorkerSchedule>(`/worker-schedules/${value.worker_id}`, { method: 'PUT', body, key: key.current.key }, { csrf: session.csrf_token })) } catch (error) { setError(error) } finally { setBusy(false) }
  }
  return <details className="schedule-editor"><summary>{displayLabel ?? value.display_name} · {t(value.data?.enabled ? 'planningWorkerEnabled' : 'planningWorkerDisabled')}</summary><form onSubmit={event => void save(event)}><p className="field-hint">{t('planningScheduleHint')} 직원 정보의 분야 중 일정에 사용할 분야를 선택해 주세요. 직원 분야 추가는 일정에 자동 반영되지 않아요.</p>{stale && <p className="notice">{t('planningStaleSchedule')}</p>}{latest && <div className="notice"><p>최신 일정 번호 {latest.revision} · 분야 {latest.data?.skills.join(', ') || '없음'} · {latest.data?.enabled ? '사용 중' : '사용 안 함'}. 현재 입력은 유지돼요.</p><button type="button" className="text-button" onClick={() => { replace(latest); setLatest(null) }}>최신 일정으로 다시 시작 · 작성 중 입력 초기화</button></div>}{!profileReady && <p className="notice">직원 정보와 일정의 분야를 확인해 주세요. 최신 정보를 불러온 뒤 기존 분야를 조정할 수 있어요.</p>}<button type="button" className="text-button" disabled={busy} onClick={() => void reload()}>{t('planningReloadSchedule')}</button><fieldset disabled={busy}><label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />{t('planningAvailability')}</label><fieldset><legend>{t('planningSkills')}</legend>{(profile && fields ? fields.filter(item => profile.field_codes.includes(item.code)).map(item => item.code) : []).map(domain => <label key={domain} className="checkbox-label"><input type="checkbox" checked={draft.skills.includes(domain)} onChange={event => setDraft({ ...draft, skills: event.target.checked ? [...draft.skills, domain] : draft.skills.filter(item => item !== domain) })} />{fields?.find(item => item.code === domain)?.name ?? t(`categoryGroup.${domain}`, { defaultValue: domain })}</label>)}</fieldset><label>{t('planningRegion')}<select value={draft.region ?? ''} onChange={event => setDraft({ ...draft, region: event.target.value })}><option value="">{t('planningNoRegion')}</option>{capabilities.regions.map(region => <option key={region} value={region}>{t(`planningRegionLabel.${region}`, { defaultValue: region })}</option>)}</select></label>{snapshot.data && !snapshot.data.hours && <label className="checkbox-label">
        <input type="checkbox" checked={useLocalTime} onChange={event => setUseLocalTime(event.target.checked)} />서울 시간으로 변경
      </label>}
      {useLocalTime ? <>
        <p className="field-hint">서울 시간에 맞춰 근무와 점심시간을 설정해 주세요.</p>
        <WorkerHoursFields draft={hours} onChange={setHours} />
      </> : <>
        <p className="field-hint">기존 근무 범위는 계획 기준시각부터 지난 시간이에요. 시:분으로 입력해 주세요.</p>
        <div className="planning-fields">
          <label>근무 시작 (기준시각 + 시:분)<input type="text" maxLength={5} placeholder="00:00" value={offset.start} onChange={event => setOffset({ ...offset, start: event.target.value })} /></label>
          <label>근무 종료 (기준시각 + 시:분)<input type="text" maxLength={5} placeholder="08:30" value={offset.end} onChange={event => setOffset({ ...offset, end: event.target.value })} /></label>
        </div>
      </>}<LocationSearch label={t('planningStartNode')} items={capabilities.nodes} disabled={busy} value={draft.start_node} onChange={code => setDraft({ ...draft, start_node: code })} /><LocationSearch label={t('planningEndNode')} items={capabilities.nodes} disabled={busy} value={draft.end_node} onChange={code => setDraft({ ...draft, end_node: code })} /><label>{t('planningScheduleSource')}<input value={draft.assumptions_source} maxLength={500} onChange={event => setDraft({ ...draft, assumptions_source: event.target.value })} /></label><button type="submit" className="button secondary" disabled={stale || !valid}>{t('planningSaveSchedule')}</button></fieldset>{Boolean(error) && <ErrorNotice error={error} />}</form></details>
}
