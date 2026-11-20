import { CampusMap } from './CampusMap'
import './reporterActions.css'
import { AIFollowup } from './AIFollowup'
import { InformationRequests } from './InformationRequests'
import { LocationSearch } from './LocationSearch'
import { needsSafetyWarning } from './safetyDisplay'
import { ReporterSteps } from './ReporterSteps'
import { useForegroundRefresh } from './useForegroundRefresh'
import { useRuntime } from './runtime'
import { MergedProgressPanel } from './MergedProgress'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, Navigate, useParams, useSearchParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, ApiError, publicIntakeError, request } from './api'
import type { Ticket } from './types'
import { useSession } from './session'
import { ErrorNotice, Loading, StatusBadge, TicketCard, TicketContent, TicketDates, TicketReceiptReference } from './shared'
import { PhotoProtection } from './PhotoProtection'
import { LocationPanel, LocationSummary } from './LocationPanel'
import type { LocationView } from './locationTypes'
import { locationFingerprint, useMemoryDraft } from './memoryDraft'
import { UploadField, useUploads } from './UploadField'

export function ReportPage() {
  const { t } = useTranslation('product')
  const runtime = useRuntime()
  const isPublic = runtime.mode === 'public_demo'
  const liveAnalysis = runtime.llm_mode === 'live'
  const showDemoPrivacyHint = runtime.mode === 'demo' && liveAnalysis
  const photoCapability = liveAnalysis ? runtime.photo_ai_capability ?? 'manual' : 'manual'
  const { session, cacheScope, draftStore } = useSession()
  const data = useMemoryDraft(draftStore)
  const token = draftStore.token()
  const { text, location, gps, gpsState, preview, selectedBuilding } = data
  const verifyGpsPreview = runtime.mode === 'service' || (runtime.mode === 'demo' && Boolean(gps))
  const [params] = useSearchParams()
  const hint = isPublic ? null : params.get('loc')
  const room = isPublic ? null : params.get('room')
  const search = useQuery({ queryKey: ['report-location-search', cacheScope, location], queryFn: () => request<{ locations: { code: string; label: string; building_code: string; region: string }[] }>('/locations/catalog', { method: 'POST', body: { query: location.trim() } }, { csrf: session?.csrf_token }), enabled: !isPublic && Boolean(session && location.trim()) && location.trim().length <= 100, retry: false })
  const [verifiedPreview, setVerifiedPreview] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [validation, setValidation] = useState(false)
  const mounted = useRef(true)
  const resolveAttempt = useRef(0)
  const gpsAttempt = useRef(0)
  const uploads = useUploads(draftStore, true)
  const client = useQueryClient()
  const current = () => draftStore.current(token)
  const busy = data.sending
  const frozen = busy || Boolean(data.pending) || Boolean(data.completedId)
  useEffect(() => {
    mounted.current = true
    if (draftStore.snapshot().gpsState === 'gpsPending') draftStore.edit(draftStore.token(), { gpsState: '' })
    return () => { mounted.current = false; resolveAttempt.current++; gpsAttempt.current++ }
  }, [draftStore])
  useEffect(() => { if (hint !== draftStore.snapshot().hint) draftStore.edit(token, { hint }) }, [hint, draftStore, token.owner, token.generation]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const preview = data.preview
    if (!verifyGpsPreview || !preview?.gps_comparison || !session) return
    let active = true
    const fingerprint = data.previewFingerprint
    const previewGps = preview.gps_comparison
    const revision = previewGps.catalog_revision ?? 0
    const previewKey = `${fingerprint ?? ''}:${revision}`
    async function checkCatalog() {
      try {
        const currentCatalog = await request<{catalog_revision: number | null}>('/catalog')
        if (active && draftStore.snapshot().previewFingerprint === fingerprint) {
          const measured = draftStore.snapshot().gps?.timestamp_ms
          const expired = previewGps.quality === 'ready' && (measured == null || Date.now() - measured > 300000)
          if ((currentCatalog.catalog_revision ?? 0) !== revision || expired) draftStore.update(token, { preview: undefined, previewFingerprint: undefined })
          else setVerifiedPreview(previewKey)
        }
      } catch {
        if (active && draftStore.snapshot().previewFingerprint === fingerprint) draftStore.update(token, { preview: undefined, previewFingerprint: undefined })
      }
    }
    void checkCatalog()
    const timer = window.setInterval(() => void checkCatalog(), 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [data.preview, data.previewFingerprint, verifyGpsPreview, session, draftStore, token.owner, token.generation]) // eslint-disable-line react-hooks/exhaustive-deps

  const photoCandidates = Array.from(uploads.photos.values()).flatMap(photo =>
    (photo as typeof photo & { location_candidate_details?: {code:string;label:string;latitude:number|null;longitude:number|null}[] }).location_candidate_details ?? [])
  const candidatePoints = photoCandidates.flatMap(candidate => candidate.latitude != null && candidate.longitude != null
    ? [{latitude:candidate.latitude, longitude:candidate.longitude, label:`사진 위치 후보: ${candidate.label}`}] : [])
  const qrCandidate = hint && preview && data.previewFingerprint === locationFingerprint(data)
    ? preview.candidates?.find(candidate => candidate.code === hint && (candidate.sources.includes('qr_hint') || candidate.sources.includes('hint'))) : undefined
  const qrPoint = qrCandidate as (typeof qrCandidate & {latitude?:number|null;longitude?:number|null})
  const qrPoints = qrPoint?.latitude != null && qrPoint.longitude != null
    ? [{latitude:qrPoint.latitude, longitude:qrPoint.longitude, label:`QR 위치 후보: ${qrPoint.label}`}] : []
  useEffect(() => {
    if (!qrCandidate || !hint || !room || !/^[0-9A-Za-z가-힣-]{1,20}$/.test(room)) return
    const snapshot = draftStore.snapshot()
    const key = JSON.stringify([hint, room])
    if (snapshot.sending || snapshot.pending || snapshot.sealed || snapshot.completedId || snapshot.appliedRooms.includes(key)) return
    draftStore.update(token, {
      appliedRooms: [...snapshot.appliedRooms, key],
      ...(!snapshot.locationTouched && !snapshot.location.trim() ? { location: `${room}호`, preview: undefined, previewFingerprint: undefined } : {}),
    })
  }, [qrCandidate, hint, room, draftStore, token.owner, token.generation]) // eslint-disable-line react-hooks/exhaustive-deps

  function getGps() {
    if (frozen) return
    const attempt = ++gpsAttempt.current
    draftStore.edit(token, { gps: undefined, gpsState: 'gpsPending' })
    if (!navigator.geolocation) { draftStore.edit(token, { gpsState: 'gpsUnavailable' }); return }
    navigator.geolocation.getCurrentPosition(position => {
      if (current() && mounted.current && attempt === gpsAttempt.current) draftStore.edit(token, { gps: { latitude: position.coords.latitude, longitude: position.coords.longitude, accuracy: position.coords.accuracy, timestamp_ms: position.timestamp }, gpsState: 'gpsReady' })
    }, () => {
      if (current() && mounted.current && attempt === gpsAttempt.current) draftStore.edit(token, { gps: undefined, gpsState: 'gpsUnavailable' })
    }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 0 })
  }
  async function resolve() {
    if (!session || frozen) return
    const snapshot = draftStore.snapshot()
    const fingerprint = locationFingerprint(snapshot)
    const attempt = ++resolveAttempt.current

    try {
      const result = await request<LocationView>('/locations/resolve', { method: 'POST', body: { text: snapshot.text, location_text: snapshot.location, ...(snapshot.hint ? { loc_hint: snapshot.hint } : {}), ...(snapshot.gps ? { gps: snapshot.gps } : {}) } }, { csrf: session.csrf_token })
      if (current() && mounted.current && attempt === resolveAttempt.current && locationFingerprint(draftStore.snapshot()) === fingerprint) { setVerifiedPreview(null); draftStore.update(token, { preview: result, previewFingerprint: fingerprint }) }
    } catch (value) { if (current() && mounted.current && attempt === resolveAttempt.current && locationFingerprint(draftStore.snapshot()) === fingerprint) setError(value) }
  }
  useEffect(() => {
    resolveAttempt.current++
    if (isPublic || !session || frozen || (!text.trim() && !location.trim() && !hint && !gps)) return
    const timer = window.setTimeout(() => void resolve(), 600)
    return () => { window.clearTimeout(timer); resolveAttempt.current++ }
  }, [text, location, hint, gps, isPublic, session, frozen, token.owner, token.generation]) // eslint-disable-line react-hooks/exhaustive-deps
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!session || !current() || draftStore.snapshot().sending || data.completedId) return
    const snapshot = draftStore.snapshot()
    if (!snapshot.text.trim()) { setValidation(true); return }
    if (!snapshot.pending && (!uploads.ready || uploads.preparing || uploads.protecting)) return
    draftStore.update(token, { sending: true }); setError(null); setValidation(false)
    try {
      let submission = snapshot.pending
      if (!submission) {
        const upload_ids = isPublic ? [] : await uploads.upload({ csrf: session.csrf_token })
        if (!current()) return
        const body = Object.freeze({ text: snapshot.text.trim(), location_text: snapshot.location.trim(), upload_ids: Object.freeze([...upload_ids]), ...(!isPublic && snapshot.hint ? { loc_hint: snapshot.hint } : {}), ...(!isPublic && snapshot.gps ? { gps: { ...snapshot.gps } } : {}), ...(!isPublic ? { information_mail_allowed: snapshot.informationMailAllowed } : {}) })
        submission = { body, key: crypto.randomUUID() }
        draftStore.update(token, { pending: submission })
      }
      const ticket = await request<Ticket>('/tickets', { method: 'POST', body: submission.body, key: submission.key }, { csrf: session.csrf_token })
      if (!current()) return
      if (!ticket.id || typeof ticket.id !== 'string') throw new Error('missing_ticket_id')
      draftStore.update(token, { completedId: ticket.id, completedPublicNo: ticket.public_no, pending: null })
      await client.invalidateQueries({ queryKey: ['tickets', cacheScope] })
    } catch (value) {
      if (current()) {
        if (value instanceof ApiError && [400, 413, 415, 422].includes(value.status)) draftStore.update(token, { pending: null })
        if (mounted.current) setError(value)
      }
    } finally { draftStore.update(token, { sending: false }) }
  }
  if (data.completedId) return <Navigate to={`/t/${data.completedId}`} replace />
  return <><div className="page-heading report-heading"><h1>{t('reportTitle')}</h1><p>{t('reportLead')}</p></div>{data.pending && <p className="notice" role="status">{t('draftPendingHint')}</p>}<form className="content-card report-form" onSubmit={event => void submit(event)}><fieldset disabled={frozen}><label htmlFor="report-text">{t('textLabel')}</label><textarea id="report-text" aria-describedby={showDemoPrivacyHint ? 'report-text-privacy' : undefined} value={text} onChange={event => draftStore.edit(token, { text: event.target.value })} placeholder={t('textPlaceholder')} maxLength={4000} rows={3} required />
      {showDemoPrivacyHint && <p className="field-hint" id="report-text-privacy">실명·연락처는 적지 마세요.</p>}
      {runtime.mode !== 'service' && <div className="report-examples">{['복도 천장에서 물이 떨어져요', '강의실 프로젝터가 켜지지 않아요'].map(example => <button className="example-chip" type="button" key={example} onClick={() => draftStore.edit(token, { text: example })} aria-label={`예시로 채우기: ${example}`}>예시: {example}</button>)}</div>}{!isPublic && <>{runtime.mode === 'service' && <label><input type="checkbox" checked={data.photoCoordinatesAllowed} disabled={frozen || uploads.files.length > 0 || uploads.preparing} onChange={event => draftStore.update(token, { photoCoordinatesAllowed: event.target.checked })} /> 사진의 촬영 위치를 저장해 담당자가 이 신고 처리에 사용하도록 동의해요 <span className="optional-label">(선택)</span></label>}{runtime.mode === 'service' && <p className="field-hint">사진을 추가하기 전에 선택해 주세요. 촬영 위치는 사진 보관기간 동안 저장되며 이 신고를 볼 수 있는 담당자가 처리에 사용해요. 원하지 않으면 체크를 해제해 주세요. 사진을 추가한 뒤에는 모두 삭제해야 선택을 바꿀 수 있어요. 신고에 첨부하지 않은 사진의 위치는 짧은 시간 뒤 삭제돼요.</p>}<p className="field-hint">{photoCapability === 'available' ? '외부 AI 처리를 승인한 가림 사본은 AI가 분석할 수 있어요. 담당자도 확인해요.' : photoCapability === 'restricted' ? '시연에서는 승인된 합성 사진만 AI가 분석할 수 있어요. 담당자도 확인해요.' : '사진은 담당자가 직접 확인해요.'}</p><UploadField autoPrepare uploads={uploads} auth={{ csrf: session?.csrf_token }} disabled={frozen} />{photoCandidates.length > 0 && <div className="photo-location-candidates"><p>사진 위치로 찾은 건물 후보예요. 선택하면 위치 설명에 추가돼요. 담당자가 확인해요.</p>{photoCandidates.map((candidate, index) => <button type="button" className="button secondary" disabled={frozen} key={`${candidate.code}:${index}`} onClick={() => { const detail = data.buildingDetail ?? location; draftStore.edit(token, { selectedBuilding: {code:candidate.code, label:candidate.label}, buildingDetail: detail, location: [candidate.label, detail].filter(Boolean).join(' ').slice(0, 300) }) }}>{candidate.label} 선택</button>)}</div>}</>}{isPublic ? <><label htmlFor="report-location">위치 <span className="optional-label">(선택)</span></label><input id="report-location" value={location} onChange={event => draftStore.edit(token, { location: event.target.value })} placeholder={t('locationPlaceholder')} maxLength={300} /><div className="location-input-actions"><button className="text-button" type="button" disabled={frozen || gpsState === 'gpsPending'} onClick={getGps}>현재 위치로 찾기</button>{gps && <button className="text-button" type="button" onClick={() => { gpsAttempt.current++; draftStore.edit(token, { gps: undefined, gpsState: '' }) }}>{t('gpsRemove')}</button>}</div>{gpsState && <p role="status" className="field-hint">{gpsState === 'gpsReady' ? '현재 위치는 이 지도에만 표시돼요. 신고에는 전송하지 않아요.' : t(gpsState)}</p>}{gps && <CampusMap points={[{ latitude: gps.latitude, longitude: gps.longitude, accuracy: gps.accuracy, label: '현재 위치' }]} />}</> : selectedBuilding ? <div className="selected-building"><p>선택한 건물: <strong>{selectedBuilding.label}</strong> <button type="button" className="text-button" onClick={() => { draftStore.edit(token, { selectedBuilding: null, buildingDetail: data.buildingDetail ?? '', location: '' }) }}>건물 변경</button></p><label htmlFor="report-location-detail">호실·상세 위치 <span className="optional-label">(선택)</span></label><input id="report-location-detail" value={data.buildingDetail ?? ''} onChange={event => draftStore.edit(token, { buildingDetail: event.target.value, location: [selectedBuilding.label, event.target.value].filter(Boolean).join(' ').slice(0, 300) })} placeholder="예: 3층 복도 또는 301호" maxLength={300-selectedBuilding.label.length} /></div> : <LocationSearch placeholder="예: 301동 3층 복도" label={<>위치 <span className="optional-label">(선택)</span></>} items={search.isFetching ? [] : search.data?.locations ?? []} disabled={frozen} searchStatus={!location.trim() ? 'idle' : !session || location.trim().length > 100 ? 'unavailable' : search.isFetching ? 'loading' : search.isError ? 'error' : search.data ? 'ready' : 'unavailable'} value="" textValue={location} onTextChange={value => draftStore.edit(token, { location: value.slice(0, 300) })} onQuery={() => {}} remoteOnly onChange={code => { const selected = search.data?.locations.find(item => item.code === code); if (selected) { draftStore.edit(token, { selectedBuilding: {code:selected.code, label:selected.label}, buildingDetail: data.buildingDetail ?? '', location: [selected.label, data.buildingDetail].filter(Boolean).join(' ').slice(0, 300) }) } }} />}</fieldset>{qrCandidate && <p className="field-hint">QR 위치: {qrCandidate.label} · 담당자 확인 필요{room && /^[0-9A-Za-z가-힣-]{1,20}$/.test(room) && <> · QR에 적힌 호실: {room}</>}</p>}{!isPublic && <><div className="location-details"><div className="location-input-actions"><button className="text-button" type="button" disabled={frozen || gpsState === 'gpsPending'} onClick={getGps}>현재 위치로 찾기</button>{gps && <button className="text-button" type="button" disabled={frozen} onClick={() => { gpsAttempt.current++; draftStore.edit(token, { gps: undefined, gpsState: '' }) }}>{t('gpsRemove')}</button>}</div>{gpsState && <p role="status" className="field-hint">{t(gpsState)}</p>}{(gps || candidatePoints.length > 0 || qrPoints.length > 0) && <CampusMap points={[...candidatePoints, ...qrPoints, ...(gps ? [{ latitude: gps.latitude, longitude: gps.longitude, accuracy: gps.accuracy, label: '현재 위치' }] : [])]} />}{preview && data.previewFingerprint === locationFingerprint(data) && hint === data.hint && (!verifyGpsPreview || preview.gps_comparison == null || verifiedPreview === `${data.previewFingerprint ?? ''}:${preview.gps_comparison.catalog_revision ?? 0}`) && <div className="notice">{verifyGpsPreview && preview.gps_comparison ? <GpsPreview location={preview} /> : <><LocationSummary location={preview} inputText={location} /><ul>{preview.candidates?.map(candidate => <li key={candidate.code}>{candidate.label}{candidate.sources.some(source => source === 'qr_hint' || source === 'hint') && ' · QR'}</li>)}</ul>{preview.issues?.includes('qr_text_conflict') && <p>QR과 입력한 위치가 달라요. 담당자가 확인해요.</p>}</>}</div>}</div></>}{runtime.mode === 'service' && <label><input type="checkbox" checked={data.informationMailAllowed} disabled={frozen} onChange={event => draftStore.edit(token, { informationMailAllowed: event.target.checked })} /> 이 신고의 추가 질문을 학교 이메일로 받기 <span className="optional-label">(선택)</span></label>}{runtime.mode === 'service' && <p className="field-hint">원하지 않으면 체크를 해제해 주세요. 접수 후에도 신고 상세에서 변경할 수 있어요.</p>}{validation && <p className="field-error" role="alert">{t('textRequired')}</p>}{Boolean(error) && (isPublic && publicIntakeError(error) ? <p className="field-error" role="alert">{publicIntakeError(error)}</p> : <ErrorNotice error={error} />)}{liveAnalysis && <p className="field-hint">
      {t('liveSubmitDisclosure')}
      {photoCapability === 'available' && <> {t('liveSubmitDisclosurePhoto')}</>}
      {photoCapability === 'restricted' && <> {t('liveSubmitDisclosureSyntheticPhoto')}</>}
    </p>}
    <p className="field-hint">{t('draftMemoryHint')}</p><div className="form-actions"><button className="button primary" disabled={busy || (!data.pending && (!uploads.ready || uploads.preparing || uploads.protecting))} type="submit">{t(busy ? 'submitting' : 'submit')} <span aria-hidden="true">→</span></button></div></form></>
}
function GpsPreview({ location }: { location: LocationView }) {
  const gps = location.gps_comparison
  if (!gps) return null
  const points = gps.points ?? []
  return <><p>참고용 위치예요. 담당자가 확인해요. (GPS 원좌표는 저장하지 않아요)</p>{Boolean(location.candidates?.length) && <ul>{location.candidates?.map(candidate => { const point = points.find(item => item.code === candidate.code); return <li key={candidate.code}>{candidate.label}{candidate.sources.includes('qr_hint') && ' · QR'}{candidate.sources.includes('current_gps') && ' · 현재 위치 근처'} · {point ? `약 ${point.distance_m}m` : '거리 비교 불가'}</li> })}</ul>}{location.issues?.includes('qr_text_conflict') && <p>QR과 입력한 위치가 달라요. 담당자가 확인해요.</p>}{location.issues?.includes('signal_conflict') && <p>위치 신호가 서로 달라요. 후보를 확인해 주세요.</p>}</>
}
export function TicketListPage({ admin = false }: { admin?: boolean }) {
  const runtime = useRuntime()
  const { t } = useTranslation('product')
  const { cacheScope } = useSession()
  const [group, setGroup] = useState<'all' | 'active' | 'completed' | 'closed'>('all')
  const [search, setSearch] = useState('')
  const query = useQuery({ queryKey: ['tickets', cacheScope], queryFn: ({ signal }) => api.tickets(signal), refetchOnWindowFocus: true })
  const items = query.data?.items ?? []
  const keyword = search.trim().toLocaleLowerCase()
  const filtered = admin ? items : items.filter(ticket => {
    const closed = ticket.status === 'rejected' || ticket.status === 'cancelled'
    const completed = ticket.status === 'completed'
    const matchesGroup = group === 'all' || (group === 'closed' && closed) ||
      (group === 'completed' && completed) || (group === 'active' && !closed && !completed)
    return matchesGroup && `${ticket.public_no} ${ticket.text}`.toLocaleLowerCase().includes(keyword)
  })
  return <>
    <div className="page-heading heading-with-action">
      <div><p className="product-eyebrow">{t(admin ? 'dispatcher' : 'service')}</p>
        <h1>{t(admin ? 'queueTitle' : 'myTitle')}</h1>
        <p>{t(admin ? runtime.mode === 'service' ? 'serviceQueueLead' : 'queueLead' : 'myLead')}</p></div>
      <Link className="button primary" to={admin ? '/admin/planning' : '/report'}>{admin ? t('planningTitle') : `+ ${t('newReport')}`}</Link>
    </div>
    {query.isPending ? <Loading /> : query.isError ? <ErrorNotice error={query.error} retry={() => void query.refetch()} /> : !items.length ? <section className="empty-state"><div className="empty-icon" aria-hidden="true">⌑</div><h2>{t(admin ? 'queueEmpty' : 'noTickets')}</h2>{!admin && <p>{t('noTicketsHint')}</p>}</section> : <>
      <div className="list-toolbar"><span>{t('total', { count: filtered.length })}</span><button className="text-button" disabled={query.isFetching} onClick={() => void query.refetch()}>{t('refresh')}</button></div>
      {!admin && <div className="queue-tools reporter-queue-tools">
        <label>신고 검색<input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="접수 번호 또는 내용" /></label>
        <nav className="queue-filters" aria-label="신고 상태">{([['all', '전체'], ['active', '진행 중'], ['completed', '완료'], ['closed', '반려·취소']] as const).map(([value, label]) => <button type="button" key={value} aria-pressed={group === value} onClick={() => setGroup(value)}>{label}</button>)}</nav>
      </div>}
      {filtered.length ? <div className="ticket-list">{filtered.map(ticket => <TicketCard key={ticket.id} ticket={ticket} admin={admin} />)}</div> : <section className="empty-state">
        <h2>조건에 맞는 신고가 없어요</h2>
        <p>검색어나 신고 상태를 바꿔 보세요.</p>
        <button type="button" className="button secondary" onClick={() => { setSearch(''); setGroup('all') }}>조건 초기화</button>
      </section>}
    </>}
  </>
}
export function TicketPage() {
  const runtime = useRuntime()
  const { id = '' } = useParams()
  const [settingsTarget, setSettingsTarget] = useState<HTMLDivElement | null>(null)
  const { t } = useTranslation('product')
  const { session, cacheScope, draftStore } = useSession()
  const query = useQuery({ queryKey: ['ticket', cacheScope, id], queryFn: ({ signal }) => api.ticket(id, signal), refetchOnWindowFocus: false })
  useForegroundRefresh(query, { enabled: Boolean(id && session && query.data && !['completed', 'rejected'].includes(query.data.status)) })
  if (query.isPending) return <Loading />
  if (query.isError) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  return <><Link className="back-link" to="/me">← {t('back')}</Link><div className="page-heading ticket-heading"><div><p className="product-eyebrow">{t('details')}</p><h1>{query.data.text.split('\n')[0].slice(0, 80)}</h1><TicketReceiptReference value={query.data.public_no} /></div><StatusBadge status={query.data.status} /></div>{needsSafetyWarning(query.data) && <p className="notice notice-error">{t('emergency')}</p>}<TicketDates ticket={query.data} /><div className="detail-layout reporter-detail"><div className="detail-main">{draftStore.snapshot().completedId === id && <p className="notice notice-success">{t('draftSubmitted')}</p>}<ReporterSteps ticket={query.data} />{runtime.mode !== 'public_demo' && query.data.status === 'received' && <AIFollowup key={`fq:${cacheScope}:${id}`} ticket={query.data} onUpdate={() => void query.refetch()} />}{runtime.mode !== 'public_demo' && <InformationRequests key={`${cacheScope}:${id}`} ticket={query.data} settingsTarget={settingsTarget} onUpdate={() => void query.refetch()} />}<button className="text-button" onClick={() => void query.refetch()} disabled={query.isFetching}>{t('refresh')}</button><MergedProgressPanel ticket={query.data} onUpdate={() => void query.refetch()} />{runtime.mode !== 'public_demo' && <LocationPanel ticket={query.data} onUpdate={() => void query.refetch()} />}{runtime.mode === 'service' ? <details className="content-card customer-details"><summary>{t('photoPrivacyMore')}</summary>{query.data.photos.map(photo => <PhotoProtection key={photo.id} photo={photo} auth={{csrf:session?.csrf_token}} onUpdate={()=>void query.refetch()} disabled={query.isFetching} onBusy={()=>{}} />)}</details> : query.data.photos.map(photo=><PhotoProtection key={photo.id} photo={photo} auth={{csrf:session?.csrf_token}} onUpdate={()=>void query.refetch()} disabled={query.isFetching} onBusy={()=>{}}/>)}<TicketContent reporter ticket={query.data} onRefresh={() => void query.refetch()} refreshing={query.isFetching} /><div className="notification-slot" ref={setSettingsTarget} /></div><aside className="detail-side content-card"><h2>{t('peopleCheck')}</h2><p className="muted">{runtime.mode === 'public_demo' ? '담당자가 신고를 확인해요.' : t(runtime.mode === 'service' ? 'servicePeopleCheckHint' : 'syntheticNote')}</p></aside></div><div className="reporter-next-action"><Link className="button secondary" to="/report" onClick={() => draftStore.reset(draftStore.token())}>{t('draftStartNew')}</Link></div></>
}
