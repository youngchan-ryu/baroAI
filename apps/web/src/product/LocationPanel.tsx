import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CampusMap } from './CampusMap'
import { ApiError, persistentKey, request } from './api'
import { useSession } from './session'
import { useOptionalRuntime } from './runtime'
import { ErrorNotice } from './shared'
import type { Ticket } from './types'
import type { LocationView } from './locationTypes'

function ReportLocationMap({ location }: { location: LocationView }) {
  const confirmed = location.state === 'confirmed'
  const candidates = confirmed ? (location.selected ? [location.selected] : []) : location.candidates
  const points = (candidates ?? []).flatMap(item =>
    item.latitude == null || item.longitude == null ? [] :
      [{ latitude: item.latitude, longitude: item.longitude, label: item.label }],
  )
  if (!points.length) return null
  return <div className="report-location-map">
    <h3>{confirmed ? '확인된 신고 위치 지도' : '신고 위치 후보 지도'}</h3>
    {!confirmed && <p className="field-hint">표시된 건물은 후보예요. 담당자가 위치를 확인해요.</p>}
    <CampusMap points={points} />
  </div>
}

export function LocationSummary({ location, inputText }: { location: LocationView; inputText?: string }) {
  const { t } = useTranslation('product')
  const unregistered = location.source_type === 'unregistered'
  return <><p className="muted-label">{t(unregistered ? 'unregisteredLocation' : ['service', 'catalog'].includes(location.source_type) ? (location.state === 'confirmed' ? 'serviceLocationConfirmed' : 'serviceLocationCandidate') : 'syntheticLocation')}</p><p>{t(`locationState.${location.state}`, { defaultValue: t('locationUnknown') })}</p><details><summary>{t('processingDetails')}</summary><p>{t(unregistered ? 'locationInputVersion' : 'locationDictionaryVersion')}: {location.dictionary_version}</p></details>{unregistered && !location.selected && inputText && <p className="ticket-body"><strong>{t('locationInputLabel')}: </strong>{inputText}</p>}{location.selected && <p><strong>{location.selected.label}</strong></p>}{location.question && <p>{location.question}</p>}{location.state === 'confirmed' && (location.issues ?? []).length > 0 && <p className="field-hint">{t('locationPastIssues')}</p>}{location.state !== 'confirmed' && (location.issues ?? []).length > 0 && <div className="notice"><strong>{t('locationIssues')}</strong><ul>{(location.issues ?? []).map(issue => <li key={issue}>{t(`locationIssue.${issue}`, { defaultValue: t('locationIssueUnknown') })}</li>)}</ul></div>}</>
}

export function LocationPanel({ ticket, onUpdate, staff = false, readOnly = false }: { ticket: Ticket; onUpdate: () => void; staff?: boolean; readOnly?: boolean }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const runtime = useOptionalRuntime()
  const [answer, setAnswer] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const location = ticket.location
  if (!location) return null
  const demoCatalog = !staff && runtime?.mode === 'demo' && ['unregistered', 'catalog'].includes(location.source_type)
  const editable = !demoCatalog && !readOnly && (staff ? ['received', 'confirmed'].includes(ticket.status) : ticket.status === 'received' && location.state !== 'confirmed' && location.state !== 'staff_review')
  async function act(action: 'confirm' | 'answer' | 'defer', candidate_code?: string) {
    if (!session || !location || busy) return
    const body = { action, expected_version: location.version, ...(candidate_code ? { candidate_code } : {}), ...(action === 'answer' ? { answer: answer.trim() } : {}), ...(staff ? { reason: reason.trim() } : {}) }
    key.current = persistentKey(key.current, body)
    setBusy(true); setError(null)
    try { await request<Ticket>(`/tickets/${ticket.id}/location`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); setAnswer(''); onUpdate() }
    catch (value) { setError(value) } finally { setBusy(false) }
  }
  if (demoCatalog && (location.can_correct_text || location.source_type === 'catalog') && location.state !== 'confirmed' && !location.selected) return <section className="content-card location-panel"><h2>신고 위치</h2><p>입력한 위치: {ticket.location_text || '미입력'}</p>{(location.candidates ?? []).length > 0 && <><p>학교 자료 후보 · 담당자 확인 대기</p><ul>{(location.candidates ?? []).map(candidate => <li key={candidate.code}>{candidate.label}</li>)}</ul></>}<ReportLocationMap location={location} />{(location.candidates ?? []).length === 0 && <p>담당자가 위치를 확인해요.</p>}{location.can_correct_text && !readOnly && <fieldset disabled={busy}><label>위치 수정<textarea rows={2} maxLength={300} value={answer} onChange={event => setAnswer(event.target.value)} /></label><p className="field-hint">수정한 위치는 담당자가 확인해요. 신고자는 최대 2회 수정할 수 있어요.</p><button type="button" className="button secondary" disabled={!answer.trim()} onClick={() => void act('answer')}>수정한 위치 보내기</button></fieldset>}{Boolean(error) && <><ErrorNotice error={error} /><button type="button" className="button secondary" onClick={onUpdate}>{t('refresh')}</button></>}</section>
  return <section className="content-card location-panel"><h2>{t(location.source_type === 'unregistered' ? 'locationInputTitle' : 'locationCheck')}</h2>{staff ? <LocationSummary location={location} inputText={ticket.location_text} /> : <><p>입력한 위치: {ticket.location_text || '미입력'}</p>{location.selected && <p>확인된 위치: {location.selected.label}</p>}{location.question && <p>{location.question}</p>}</>}{!staff && <ReportLocationMap location={location} />}{editable && <fieldset disabled={busy}><legend>{t(staff ? 'staffLocationHint' : 'locationConfirmHint')}</legend>{staff && <label>{t('locationReason')}<textarea rows={2} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label>}<div className="location-candidates">{(location.candidates ?? []).map(candidate => <button type="button" className="button secondary" key={candidate.code} disabled={staff && !reason.trim()} onClick={() => void act('confirm', candidate.code)}>{candidate.label}<small>{candidate.sources.map(source => t(`locationSource.${source}`, { defaultValue: t('locationSourceOther') })).join(' · ')}</small></button>)}</div>{(staff || location.can_answer) && <><label>{t('locationAnswer')}<textarea rows={2} maxLength={300} value={answer} onChange={event => setAnswer(event.target.value)} /></label><p className="field-hint">{t('locationAnswerCount', { count: location.clarification_count })}</p><button type="button" className="button secondary" disabled={!answer.trim() || (staff && !reason.trim())} onClick={() => void act('answer')}>{t('locationSendAnswer')}</button></>}<button type="button" className="text-button" disabled={staff && !reason.trim()} onClick={() => void act('defer')}>{t('locationDefer')}</button></fieldset>}{Boolean(error) && <><ErrorNotice error={error} />{error instanceof ApiError && error.status === 409 && <p className="field-hint">{t('locationInputKept')}</p>}<button type="button" className="button secondary" onClick={onUpdate}>{t('refresh')}</button></>}{staff && ticket.routing && <div className="routing-summary"><h3>{t('routingTitle')}</h3><p>{ticket.routing.department_label}</p><p>{ticket.routing.reason}</p>{(ticket.routing.co_review ?? []).length > 0 && <p>{t('routingCoReview')}: {(ticket.routing.co_review ?? []).map(item => item.label).join(' · ')}</p>}<strong className="muted-label">{t('routingNotSent')}</strong><details><summary>{t('processingDetails')}</summary><p>{t('routingRulesVersion')}: {ticket.routing.rules_version}</p></details></div>}</section>
}
