import { useOptionalRuntime } from './runtime'
import { workerDistinctLabel } from './workerDisplay'
import './schoolSettings.css'
import type { components } from '../api/schema'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useLocation } from 'react-router'
import { ApiError, imageRequest, persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice, Loading } from './shared'
import { dateTime, displayDate } from './dateTime'
import { ScheduleEditor, type ScheduleContext } from './WorkerScheduleEditor'
import type { WorkerSchedule } from './planningTypes'

type Catalog = components['schemas']['app__school_catalog_routes__CatalogView']
export type SchoolWorker = components['schemas']['ProfileView']
type Preview = components['schemas']['ImportView']

export function SchoolSettingsPage() {
  const mode = useOptionalRuntime()?.mode ?? 'service'
  const { session } = useSession()
  const { hash } = useLocation()
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [workers, setWorkers] = useState<SchoolWorker[]>([])
  const [preview, setPreview] = useState<Preview | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [previewTimeout, setPreviewTimeout] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [message, setMessage] = useState('')
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const loadGeneration = useRef(0)
  async function load() { const generation = ++loadGeneration.current; const [current, people] = await Promise.all([request<Catalog>('/admin/catalog'), request<SchoolWorker[]>('/admin/workers')]); if (generation === loadGeneration.current) { setCatalog(current); setWorkers(people) } }
  useEffect(() => { const counter = loadGeneration; void load().catch(setError); return () => { counter.current++ } }, [])
  const catalogLoaded = Boolean(catalog)
  useEffect(() => { if (catalogLoaded && hash === '#worker-schedules') document.getElementById('worker-schedules')?.scrollIntoView?.() }, [catalogLoaded, hash])
  async function download() { setError(null); try { const blob = await imageRequest('/admin/catalog-template', {}, new AbortController().signal); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'school-catalog.xlsx'; a.click(); URL.revokeObjectURL(url) } catch (value) { setError(value) } }
  function selectFile(nextFile: File | null) {
    setFile(nextFile)
    setPreview(null)
    setReason('')
    key.current = null
    setError(null)
    setPreviewTimeout(false)
    setMessage('')
  }
  async function upload(event: FormEvent) {
    event.preventDefault()
    if (!file || !session || busy) return
    setBusy(true)
    setUploading(true)
    setError(null)
    setPreviewTimeout(false)
    setMessage('')
    setPreview(null)
    key.current = null
    try {
      const body = new FormData()
      body.set('file', file)
      setPreview(await request<Preview>('/admin/catalog-imports', { method: 'POST', body, timeoutMs: 120_000 }, { csrf: session.csrf_token }))
    } catch (value) {
      if (value instanceof DOMException && value.name === 'TimeoutError') setPreviewTimeout(true)
      else setError(value)
    } finally {
      setUploading(false)
      setBusy(false)
    }
  }
  async function activate() { if (!preview || !session || !reason.trim() || busy) return; const body = { expected_active_revision: preview.base_revision, reason: reason.trim() }; key.current = persistentKey(key.current, { id: preview.id, ...body }); setBusy(true); setError(null); try { await request(`/admin/catalog-imports/${encodeURIComponent(preview.id)}/activate`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); await load(); setPreview(null); setReason(''); key.current = null; setMessage('학교 자료를 적용했어요. 직원 분야와 일정을 확인해 주세요.') } catch (value) { setError(value) } finally { setBusy(false) } }
  if (!catalog && !error) return <Loading />
  return <div className="school-settings"><Link className="back-link" to="/admin/queue">← 업무함</Link><div className="page-heading"><h1>학교 설정</h1><p>학교 자료와 기존 직원의 업무 분야를 관리해요. 계정이나 권한은 만들지 않아요.</p></div>{Boolean(error) && (error instanceof ApiError && (error.code === 'invalid_workbook' || error.code === 'catalog_provider_not_selected') ? <div className="notice notice-error" role="alert"><p>{error.code === 'invalid_workbook' ? '엑셀 양식을 다시 받아 형식을 확인하고 파일을 다시 선택해 주세요.' : '학교 자료를 적용할 운영 설정이 준비되지 않았어요. 운영자에게 확인을 요청하고, 설정 후 파일을 다시 미리보기해 주세요.'}</p>{error.status >= 500 && error.requestId && <details><summary>문의용 코드</summary><small>{error.requestId}</small></details>}</div> : <ErrorNotice error={error} retry={!catalog ? () => { setError(null); void load().catch(setError) } : undefined} />)}{message && <p className="notice" role="status">{message}</p>}{previewTimeout && <p className="notice" role="alert">미리보기 결과를 받지 못했어요. 학교 설정에는 적용되지 않았어요.</p>}{catalog && <><section className="content-card school-status"><h2>현재 적용된 학교 자료</h2><p>{catalog.revision ? <>적용 시각 {catalog.activated_at ? <time dateTime={catalog.activated_at} title={dateTime(catalog.activated_at, 'full')}>{displayDate(catalog.activated_at)}</time> : '미확인'}</> : '아직 적용된 학교 자료가 없어요. 아래에서 파일을 확인하고 적용해 주세요.'}</p><p className="school-dataset-metadata">{catalog.dataset ? `이동 자료 적용됨 · 출처 ${catalog.dataset.source === 'estimated_straight_line' ? '직선거리 기반 추정' : catalog.dataset.source === '공개자료 기반·기관 미검증' ? '공개 자료 기반 · 학교 확인 전' : catalog.dataset.source ?? '확인 필요'}` : '이동 자료가 없어도 수동 배정할 수 있어요. 추천에는 이동 자료가 필요해요.'}</p>{catalog.revision > 0 && <details><summary>처리 기록</summary><p>자료 번호 {catalog.revision}</p>{catalog.dataset && <p>이동 자료 {catalog.dataset.version} · 자료 번호 {catalog.dataset.revision} · 출처 버전 {catalog.dataset.source_version ?? '확인 필요'} · 확인값 {catalog.dataset.hash}</p>}</details>}<div className="school-summary">{[['위치', catalog.revision ? `${catalog.locations.length}곳` : '미확인'], ['좌표 있음', catalog.revision && catalog.locations.every(item => 'latitude' in item && 'longitude' in item) ? `${catalog.locations.filter(item => typeof item.latitude === 'number' && typeof item.longitude === 'number').length}곳` : '미확인'], ['분류', catalog.revision ? `${catalog.categories.length}개` : '미확인'], ['직원', `${workers.length}명`]].map(([label, value]) => <div key={label}><strong>{label}</strong><span>{value}</span></div>)}</div></section><details className="content-card school-import"><summary>자료 바꾸기</summary><h2>학교 자료 등록</h2><p>빈 양식을 받아 학교의 코드와 위치를 입력해 주세요. 계정 정보, 메일 주소나 비밀 정보는 입력하지 마세요. 업로드만으로 현재 자료는 바뀌지 않아요.</p><button type="button" className="button secondary" onClick={() => void download()}>엑셀 양식 받기</button><form className="school-upload" onSubmit={event => void upload(event)}><label>작성한 엑셀 파일<input type="file" accept=".xlsx" disabled={busy} onChange={event => selectFile(event.target.files?.[0] ?? null)} /></label>{file && <p style={{ overflowWrap: 'anywhere' }}>선택한 파일: {file.name} · {new Intl.NumberFormat('ko-KR').format(file.size)}바이트</p>}<button className="button secondary" disabled={!file || busy}>오류와 변경 내용 확인</button>{uploading && <p role="status">학교 자료를 확인하고 있어요.</p>}</form>{preview && <><h3 className="school-preview-heading">적용 전 확인</h3>{preview.errors.length > 0 && <><p>파일을 수정한 뒤 다시 올려 주세요. 현재 자료는 유지돼요.</p><ul>{preview.errors.map((item, index) => <li key={index}>{item.sheet} · {item.row ?? '-'}행 · {item.column ?? '-'}열: {item.message} ({item.code})</li>)}</ul></>}<p>이동 자료: {preview.dataset_preview.present ? `위치 ${preview.dataset_preview.location_count}개 · 출처 ${preview.dataset_preview.source}` : '없음'}. {preview.dataset_preview.changed ? '현재 이동 자료와 달라요.' : '현재 이동 자료와 같아요.'}</p><details><summary>처리 기록</summary><p>기준 자료 {preview.base_revision}</p>{preview.dataset_preview.present && <p>버전 {preview.dataset_preview.version} · 출처 버전 {preview.dataset_preview.source_version} · 확인값 {preview.dataset_preview.sha256}</p>}</details>{(['added', 'changed', 'deactivated'] as const).map(kind => <details key={kind} open={preview.changes[kind].length > 0}><summary>{kind === 'added' ? '추가' : kind === 'changed' ? '변경' : '사용 중지'} {preview.changes[kind].length}건</summary>{preview.changes[kind].map((item, index) => <div key={index}><strong>{item.sheet} · {item.key}</strong><p>이전: {item.before ? JSON.stringify(item.before) : '없음'}</p><p>변경 후: {item.after ? JSON.stringify(item.after) : '없음'}</p></div>)}</details>)}<label>적용 사유<textarea value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} /></label><button type="button" className="button primary" disabled={!preview.valid || !reason.trim() || busy} onClick={() => void activate()}>확인한 자료 적용</button><p className="field-hint">다른 사람이 자료를 바꾸었다면 파일을 다시 올려 확인해 주세요.</p></>}</details><section className="content-card school-workers"><h2>기존 직원 업무 정보</h2><p>분야를 추가해도 일정에 자동 반영되지 않아요. 배정 추천 화면에서 일정을 따로 확인해 주세요.</p>{workers.map(worker => <WorkerEditor key={worker.worker_id} displayLabel={workerDistinctLabel(worker.worker_id, worker.display_name, workers.map(item => ({ id: item.worker_id, name: item.display_name })), mode)} worker={worker} catalog={catalog} onReload={load} />)}<ScheduleRecovery /><SchoolSchedules catalog={catalog} workers={workers} onRefresh={load} /><Link to="/admin/planning">배정 추천과 일정 확인</Link></section></>}</div>
}
function WorkerEditor({ worker, catalog, onReload, displayLabel }: { displayLabel: string; worker: SchoolWorker; catalog: Catalog; onReload: () => Promise<void> }) {
  const { session } = useSession(); const [base, setBase] = useState(worker); const [catalogRevision, setCatalogRevision] = useState(catalog.revision); const [fields, setFields] = useState(worker.field_codes); const [department, setDepartment] = useState(worker.department_code ?? ''); const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null); const [saved, setSaved] = useState(false)
  async function save(event: FormEvent) { event.preventDefault(); if (!session || !reason.trim() || busy) return; setBusy(true); setError(null); setSaved(false); try { const result = await request<SchoolWorker>(`/admin/workers/${encodeURIComponent(worker.worker_id)}/profile`, { method: 'PATCH', body: { field_codes: fields, department_code: department || null, expected_revision: base.revision, expected_catalog_revision: catalogRevision, reason: reason.trim() } }, { csrf: session.csrf_token }); setBase(result); setReason(''); setSaved(true); await onReload() } catch (value) { setError(value) } finally { setBusy(false) } }
  const obsolete = fields.filter(code => !catalog.fields.some(item => item.code === code))
  return <details className="schedule-editor"><summary>{displayLabel}</summary><form onSubmit={event => void save(event)}><p>현재 저장된 소속: {catalog.departments.find(item => item.code === base.department_code)?.name ?? (base.department_code ? '이전 소속 확인 필요' : '없음')} · 분야: {base.field_codes.map(code => catalog.fields.find(item => item.code === code)?.name ?? '이전 분야 확인 필요').join(', ') || '없음'} · 직원 정보 번호 {base.revision}</p>{catalog.revision > 0 && base.catalog_revision !== catalog.revision && <p className="notice">이 직원 정보는 이전 학교 자료 기준이에요. 현재 소속과 분야를 확인하고 사유와 함께 다시 저장해 주세요. 기존 일정의 분야도 따로 확인해야 해요.</p>}{obsolete.length > 0 && <p className="notice">현재 자료에 없는 분야 {obsolete.length}개가 있어요. 활성 예약을 정리하고 기존 일정을 비활성화한 뒤 현재 분야로 다시 저장해 주세요.</p>}<label>소속<select value={department} onChange={event => setDepartment(event.target.value)}><option value="">선택 안 함</option>{department && !catalog.departments.some(item => item.code === department) && <option value={department}>이전 소속 확인 필요</option>}{catalog.departments.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label><fieldset><legend>업무 분야</legend>{[...obsolete.map((code, index) => ({ code, name: `이전 분야 ${index + 1} (확인 필요)` })), ...catalog.fields].map(item => <label key={item.code} className="checkbox-label"><input type="checkbox" checked={fields.includes(item.code)} onChange={event => setFields(event.target.checked ? [...fields, item.code] : fields.filter(code => code !== item.code))} />{item.name}</label>)}</fieldset><label>변경 사유<input value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} /></label><button className="button secondary" disabled={busy || !reason.trim()}>직원 정보 저장</button>{saved && <p role="status">직원 정보를 저장했어요.</p>}{Boolean(error) && <><ErrorNotice error={error} onReload={() => void onReload().catch(setError)} />{error instanceof ApiError && error.status === 409 && <p>입력은 유지돼요. 최신 자료와 직원 정보를 다시 확인한 뒤 저장해 주세요. 활성 예약이나 일정이 분야 변경을 막을 수 있어요.</p>}{(worker.revision !== base.revision || catalog.revision !== catalogRevision) && <><p>최신 직원 정보: 소속 {catalog.departments.find(item => item.code === worker.department_code)?.name ?? (worker.department_code ? '확인 필요' : '없음')} · 분야 {worker.field_codes.map(code => catalog.fields.find(item => item.code === code)?.name ?? '확인 필요').join(', ') || '없음'}</p><details><summary>처리 기록</summary><p>직원 정보 번호 {worker.revision} · 자료 번호 {catalog.revision}</p></details><button type="button" className="text-button" onClick={() => { setBase(worker); setCatalogRevision(catalog.revision); setError(null) }}>최신 정보를 확인하고 내 입력 유지</button></>}</>}</form></details>
}

function ScheduleRecovery() {
  const mode = useOptionalRuntime()?.mode ?? 'service'
  const { session } = useSession()
  const [schedules, setSchedules] = useState<Array<{ worker_id: string; display_name: string; revision: number; profile_revision: number; data: Record<string, unknown> | null }>>([])
  const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState(false)
  async function load() { try { setSchedules(await request('/worker-schedules')); setError(null) } catch (value) { setError(value) } }
  async function disable(schedule: typeof schedules[number]) { if (!session || !schedule.data) return; setBusy(true); setError(null); try { await request(`/worker-schedules/${encodeURIComponent(schedule.worker_id)}`, { method: 'PUT', body: { ...schedule.data, enabled: false, expected_revision: schedule.revision, expected_profile_revision: schedule.profile_revision }, key: crypto.randomUUID() }, { csrf: session.csrf_token }); await load() } catch (value) { setError(value) } finally { setBusy(false) } }
  return <details><summary>이전 일정 비활성화</summary><p>자료가 교체되어 이전 분야가 남은 경우 활성 예약을 먼저 정리한 뒤 일정을 비활성화하고 직원 정보와 일정을 다시 설정해 주세요.</p><button type="button" className="button secondary" onClick={() => void load()}>현재 일정 불러오기</button>{schedules.filter(item => item.data?.enabled).map(item => <p key={item.worker_id}>{workerDistinctLabel(item.worker_id, item.display_name, schedules.filter(value => value.data?.enabled).map(value => ({id: value.worker_id, name: value.display_name})), mode)} <button type="button" disabled={busy} onClick={() => void disable(item)}>일정 비활성화</button></p>)}{Boolean(error) && <ErrorNotice error={error} />}</details>
}


type LocationDataset = { revision: number; sha256: string; locations: { code: string; label: string }[] }
function SchoolSchedules({ catalog, workers, onRefresh }: { catalog: Catalog; workers: SchoolWorker[]; onRefresh: () => Promise<void> }) {
  const mode = useOptionalRuntime()?.mode ?? 'service'
  const [opened, setOpened] = useState(false)
  const [schedules, setSchedules] = useState<WorkerSchedule[]>([])
  const [capabilities, setCapabilities] = useState<ScheduleContext | null>(null)
  const [valid, setValid] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [sources, setSources] = useState<{ catalog: Catalog; workers: SchoolWorker[] } | null>(null)
  const generation = useRef(0)
  const savedSchedules = useRef(new Map<string, WorkerSchedule>())
  useEffect(() => {
    if (!opened) return
    const counter = generation
    const current = ++counter.current
    Promise.all([request<WorkerSchedule[]>('/worker-schedules'), request<LocationDataset>('/planning/location-dataset')]).then(([list, dataset]) => {
      if (current !== generation.current) return
      const rows = catalog.locations as { code?: string; name?: string; region?: string; aliases?: unknown; building_code?: unknown }[]
      const nodes = dataset.locations.map(location => {
        const matches = rows.filter(row => row.code === location.code && row.name === location.label && typeof row.region === 'string' && row.region.trim())
        const region = matches.length === 1 ? matches[0].region : undefined
        return region ? { code: location.code, label: location.label, region, aliases: typeof matches[0].aliases === 'string' ? matches[0].aliases.split('\n') : [], building_code: location.label.match(/\d+(?:-\d+)?/)?.[0] } : null
      })
      const coherent = Boolean(catalog.revision && catalog.dataset && catalog.dataset.revision === dataset.revision && catalog.dataset.hash === dataset.sha256 && nodes.length && nodes.every(Boolean) && new Set(dataset.locations.map(item => item.code)).size === nodes.length)
      setSchedules(list.map(item => {
        const saved = savedSchedules.current.get(item.worker_id)
        return saved && saved.revision > item.revision ? { ...saved, profile_revision: item.profile_revision } : item
      }))
      if (coherent) {
        const checked = nodes.filter((node): node is NonNullable<typeof node> => node !== null)
        setCapabilities({ data_kind: 'service', dataset_hash: dataset.sha256, dataset_revision: dataset.revision, regions: [...new Set(checked.map(node => node.region))], nodes: checked, max_offset_min: 1440 })
        setSources({ catalog, workers })
      }
      setValid(coherent)
      setLoading(false)
      setError(null)
    }).catch(value => { if (current === generation.current) { setValid(false); setLoading(false); setError(value) } })
    return () => { counter.current++ }
  }, [opened, catalog, workers])
  async function refresh() {
    generation.current++
    setValid(false)
    setLoading(true)
    try {
      await onRefresh()
      setOpened(true)
    } catch (value) {
      setLoading(false)
      setError(value)
    }
  }

  return (
    <section id="worker-schedules">
      <h3>직원 일정</h3>
      <button type="button" className="button secondary" onClick={() => void refresh()}>일정 불러오기</button>
      {opened && loading && <p role="status">일정을 불러오고 있어요.</p>}
      {opened && !valid && !loading && <p className="notice">현재 학교 자료와 일정 위치를 확인할 수 없어요. 자료와 직원 정보를 다시 확인해 주세요.</p>}
      {Boolean(error) && <ErrorNotice error={error} />}
      {capabilities && schedules.map(schedule => (
        <ScheduleEditor
          key={schedule.worker_id}
          value={schedule}
          displayLabel={workerDistinctLabel(schedule.worker_id, schedule.display_name, schedules.map(item => ({ id: item.worker_id, name: item.display_name })), mode)}
          profile={workers.find(worker => worker.worker_id === schedule.worker_id)}
          fields={catalog.fields}
          catalogRevision={catalog.revision}
          contextValid={valid && !loading && sources?.catalog === catalog && sources?.workers === workers}
          capabilities={capabilities}
          onSaved={updated => {
            savedSchedules.current.set(updated.worker_id, updated)
            setSchedules(previous => previous.map(item => item.worker_id === updated.worker_id ? updated : item))
          }}
        />
      ))}
    </section>
  )
}
