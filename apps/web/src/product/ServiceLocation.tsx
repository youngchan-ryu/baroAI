import { useLocationCatalogSearch } from './useLocationCatalogSearch'
import { LocationSearch } from './LocationSearch'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, request } from './api'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import type { Ticket } from './types'
import type { components } from '../api/schema'

type Dataset = components['schemas']['ServiceLocationDataset']
type CatalogCandidates = components['schemas']['CatalogLocationCandidates']


export function ServiceLocation({ ticket, onLoadLatest, onUpdate, disabled = false, catalogRevision }: { ticket: Ticket; onLoadLatest: () => Promise<Ticket>; onUpdate: () => void; disabled?: boolean; catalogRevision?: number | null }) {
  const { t } = useTranslation('product')
  const { session, cacheScope } = useSession()
  const [suggestions, setSuggestions] = useState<CatalogCandidates | null>(null)
  const [candidateCode, setCandidateCode] = useState('')
  const [candidateReason, setCandidateReason] = useState('')
  const [candidateError, setCandidateError] = useState<unknown>(null)
  const [candidateBusy, setCandidateBusy] = useState(false)
  const [candidateBlocked, setCandidateBlocked] = useState(false)
  const [dataset, setDataset] = useState<Dataset | null>(null)
  const [locationVersion, setLocationVersion] = useState<number | null>(null)
  const [code, setCode] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [blocked, setBlocked] = useState(false)
  const locationSearch = useLocationCatalogSearch((dataset?.locations ?? []).filter(item => typeof item.code === 'string' && typeof item.label === 'string').map(item => ({ code: item.code, label: item.label })), session?.csrf_token, cacheScope, catalogRevision ?? suggestions?.catalog_revision)
  const stale = locationVersion !== null && locationVersion !== ticket.location?.version
  const editable = ['received', 'confirmed'].includes(ticket.status)
  async function load() {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const latest = await onLoadLatest()
      const current = await request<Dataset>('/planning/location-dataset')
      setDataset(current); setLocationVersion(latest.location?.version ?? null); setCode(''); setBlocked(false)
    } catch (value) { setError(value); setDataset(null); setBlocked(true) } finally { setBusy(false) }
  }
  async function confirm() {
    if (!session || !dataset || locationVersion === null || !code || !reason.trim() || busy || blocked || stale) return
    setBusy(true); setError(null)
    try {
      await request<Ticket>(`/tickets/${ticket.id}/service-location`, { method: 'POST', body: { expected_location_version: locationVersion, dataset_hash: dataset.sha256, dataset_revision: dataset.revision, code, reason: reason.trim() } }, { csrf: session.csrf_token })
      setDataset(null); setCode(''); setReason(''); setLocationVersion(null); onUpdate()
    } catch (value) { setError(value); setBlocked(true) } finally { setBusy(false) }
  }
  async function loadCandidates() {
    if (candidateBusy) return
    setCandidateBusy(true); setCandidateError(null); setCandidateCode('')
    try {
      await onLoadLatest()
      const current = await request<CatalogCandidates>(`/tickets/${ticket.id}/catalog-location-candidates`)
      setSuggestions(current); setCandidateBlocked(false)
    } catch (value) { setSuggestions(null); setCandidateError(value); setCandidateBlocked(true) }
    finally { setCandidateBusy(false) }
  }
  async function confirmCandidate() {
    if (!session || !suggestions?.catalog_revision || !candidateCode || !candidateReason.trim() || candidateBusy || candidateBlocked || suggestions.location_version !== ticket.location?.version) return
    setCandidateBusy(true); setCandidateError(null)
    try {
      await request(`/tickets/${ticket.id}/catalog-location`, { method: 'POST', body: { catalog_revision: suggestions.catalog_revision, expected_location_version: suggestions.location_version, code: candidateCode, reason: candidateReason.trim() } }, { csrf: session.csrf_token })
      setSuggestions(null); setCandidateCode(''); setCandidateReason(''); onUpdate()
    } catch (value) { setCandidateError(value); setCandidateBlocked(true) }
    finally { setCandidateBusy(false) }
  }
  if (!session || session.user.role !== 'dispatcher' || !editable) return null
  return <><section className="content-card"><h2>신고 내용의 등록 위치 후보</h2><p>신고 내용과 학교 자료의 이름·별칭이 일치하는 후보예요. 담당자가 직접 확인해요. 이 후보 확인은 계획 자료를 사용하는 정식 배정 위치 확인을 대신하지 않아요.</p><button className="button secondary" type="button" disabled={candidateBusy} onClick={() => void loadCandidates()}>위치 후보 확인</button>{suggestions?.status === 'unavailable' && <p>사용할 학교 위치 자료가 없어요. 수동 확인과 배정을 진행할 수 있어요.</p>}{suggestions?.status === 'no_match' && <p>일치하는 후보가 없어요. 실제 장소가 없다는 뜻은 아니에요. 수동으로 확인해 주세요.</p>}{(suggestions as (CatalogCandidates & { qr_text_conflict?: boolean }) | null)?.qr_text_conflict && <p role="status">QR과 입력한 위치가 달라요. 위치를 직접 확인해 주세요.</p>}{suggestions?.status === 'multiple' && <p>일치하는 후보가 여러 개예요. 위치를 직접 확인해 주세요.</p>}{Boolean(suggestions?.candidates.length) && <><label>위치 후보 선택<select value={candidateCode} disabled={candidateBusy || candidateBlocked || suggestions?.location_version !== ticket.location?.version} onChange={event => setCandidateCode(event.target.value)}><option value="">선택해 주세요</option>{suggestions?.candidates.map((item, index) => <option key={item.code} value={item.code}>{item.label}{suggestions.candidates.filter(candidate => candidate.label === item.label && candidate.floor === item.floor).length > 1 && ` · 후보 ${index + 1}`}{(item as typeof item & { sources?: string[] }).sources?.includes('qr_hint') && ' · QR'}{(item as typeof item & { sources?: string[] }).sources?.includes('current_gps') && ' · 현재 위치 근처'}{(item as typeof item & { sources?: string[] }).sources?.includes('photo_gps') && ' · 사진 위치'}{item.floor && ` · ${item.floor}층`}</option>)}</select></label><label>후보 확인 사유<textarea value={candidateReason} rows={2} maxLength={1000} onChange={event => setCandidateReason(event.target.value)} /></label><button className="button primary" type="button" disabled={candidateBusy || candidateBlocked || !candidateCode || !candidateReason.trim() || suggestions?.location_version !== ticket.location?.version} onClick={() => void confirmCandidate()}>후보 위치 확정</button></>}{(candidateBlocked || (suggestions && suggestions.location_version !== ticket.location?.version)) && <p role="status">신고와 학교 자료를 다시 확인해 주세요.</p>}{Boolean(candidateError) && <ErrorNotice error={candidateError} />}</section><section id="service-location" className="content-card"><h2>배정에 사용할 등록 위치 확인</h2><p>{t('serviceLocationHint')} 계획 자료를 사용하는 배정에서는 여기에서 위치를 선택해 확인해 주세요.</p><p>위치를 확인하거나 변경한 뒤에는 <a href="#category-review">분류와 담당 부서</a>를 다시 확인하고 사유와 함께 저장해 주세요.</p><button className="button secondary" type="button" disabled={busy} onClick={() => void load()}>{t('serviceLocationLoad')}</button>{dataset && <><details><summary>처리 기록</summary><p>{t('serviceLocationVersion')}: {dataset.version} · {t('serviceLocationGeneration')}: {dataset.revision}</p></details><LocationSearch label={t('serviceLocationChoose')} items={locationSearch.items} searchStatus={locationSearch.status} onQuery={locationSearch.setQuery} value={code} disabled={disabled || busy || blocked || stale} onChange={setCode} /><label>{t('locationReason')}<textarea value={reason} rows={2} maxLength={1000} disabled={busy} onChange={event => setReason(event.target.value)} /></label><button className="button secondary" type="button" disabled={busy || blocked || stale || !code || !reason.trim()} onClick={() => void confirm()}>{t('serviceLocationConfirm')}</button></>}{(stale || blocked) && <p className="notice" role="status">{t('serviceLocationRetry')}</p>}{error instanceof ApiError && error.status === 503 && <p className="notice">{t('serviceLocationUnavailable')}</p>}{Boolean(error) && <ErrorNotice error={error} />}</section></>
}
