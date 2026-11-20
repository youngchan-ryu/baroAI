import { needsSafetyWarning, safetyUrgencies, urgencyLabel } from './safetyDisplay'
import { LinkedSafetySummary } from './LinkedSafety'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { request } from './api'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import type { Ticket } from './types'

export function SafetyPanel(props: { ticket: Ticket; onUpdate?: (ticket: Ticket) => void }) {
  return <SafetyPanelEditor key={props.ticket.id} {...props} />
}

function SafetyPanelEditor({ ticket, onUpdate }: { ticket: Ticket; onUpdate?: (ticket: Ticket) => void }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const [urgency, setUrgency] = useState<number | ''>(() => ticket.safety ? safetyUrgencies(ticket.safety).ownCurrent ?? '' : '')
  const [reason, setReason] = useState('')
  const [editVersion, setEditVersion] = useState(() => ticket.safety?.assessment_version)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [saved, setSaved] = useState(false)
  const safety = ticket.safety
  const stale = safety?.assessment_version !== editVersion
  const editor = useRef<HTMLDetailsElement>(null)
  const attention = Boolean(stale || busy || error || reason.trim() || (safety && urgency !== (safetyUrgencies(safety).ownCurrent ?? '')))
  useEffect(() => { if (attention && editor.current) editor.current.open = true }, [attention])
  async function submit() {
    if (!safety || stale || !session || urgency === '' || !reason.trim()) return
    setBusy(true); setError(null); setSaved(false)
    try {
      const updated = await request<Ticket>(`/tickets/${ticket.id}/urgency`, { method: 'POST', body: { effective_urgency: urgency, reason: reason.trim(), expected_assessment_version: editVersion } }, { csrf: session.csrf_token })
      onUpdate?.(updated); setSaved(true); setReason('')
    } catch (value) { setError(value) } finally { setBusy(false) }
  }
  const values = safety && safetyUrgencies(safety)
  return <section className="content-card safety-panel"><h2>{t('safetyTitle')}</h2><LinkedSafetySummary ticket={ticket} />
    {!safety ? <p className="notice">{t('safetyPending')}</p> : <>
      {safety.assessment_status === 'pending' && <p className="notice">{t('safetyPending')}</p>}
      {needsSafetyWarning(ticket) && <div className="notice notice-error"><p>{t('emergency')}</p></div>}
      {(safety.rule_disagreement ?? (safety.assessment_status === 'live_priority' && safety.ai_urgency != null && safety.rule_floor < safety.ai_urgency)) && <p className="notice">AI 판단과 원문 위험 신호가 달라요. 원문을 확인해 주세요.</p>}
      <dl className="safety-values"><div><dt>자동 긴급도</dt><dd>{urgencyLabel(values?.ownAutomatic)}</dd></div><div><dt>현재 적용 긴급도</dt><dd>{urgencyLabel(values?.current)}{safety.override_reason ? ' · 사람 조정 반영' : ''}</dd></div><div><dt>{t('aiUrgency')}</dt><dd>{safety.ai_urgency == null ? t('modelNotRun') : urgencyLabel(safety.ai_urgency)}</dd></div><div><dt>{t('ruleFloor')}</dt><dd>{safety.rule_floor < 3 ? urgencyLabel(safety.rule_floor) : '적중 없음'}</dd></div></dl>
      {safety.override_reason && <div className="notice"><strong>{t('override')}</strong><p>{safety.override_reason}</p></div>}
      <h3>{t('ruleSignals')}</h3>{safety.rule_hits?.length ? <ul className="safety-signals">{safety.rule_hits.map((hit, index) => <li key={`${hit.rule_id}-${index}`}><q>{Array.from(safety.source_text ?? ticket.text).slice(hit.start, hit.end).join('')}</q><span>{t(`context.${hit.context}`, { defaultValue: hit.context })}</span><small>{urgencyLabel(hit.suggested_urgency)}</small></li>)}</ul> : <p className="field-hint">{t('noSignals')}</p>}
      <details className="safety-details"><summary>처리 기록</summary><p>{t('rulesVersion')}: {safety.rules_version}</p><p>{t('assessment')}: {safety.rule_guard_kept ? '안전 규칙 · 원문 위험 신호 유지' : t(`assessmentState.${safety.assessment_status}`, { defaultValue: t('pendingReview') })}</p>{safety.rule_hits?.map((hit, index) => <code key={`${hit.rule_id}-${index}`}>{hit.rule_id} </code>)}</details>
    </>}
    {session?.user.role === 'dispatcher' && onUpdate && safety && safety.assessment_status !== 'pending' && <details ref={editor} onToggle={event => { if (attention && !event.currentTarget.open) event.currentTarget.open = true }}><summary>다른 작업 · 긴급도 직접 조정</summary><fieldset disabled={busy}><h3>{t('urgencyEdit')}</h3>{stale && <div className="notice" role="status"><p>안전 판단이 바뀌었어요. 현재 위험을 다시 확인해 주세요. 작성한 선택과 사유는 유지돼요.</p><button type="button" className="text-button" onClick={() => setEditVersion(safety.assessment_version)}>최신 내용 불러오기</button></div>}<label>적용 긴급도<select value={urgency} onChange={event => setUrgency(event.target.value === '' ? '' : Number(event.target.value))}><option value="" disabled>긴급도를 선택해 주세요</option>{[0, 1, 2, 3].map(level => <option key={level} value={level}>{urgencyLabel(level)}</option>)}</select></label><label>{t('urgencyReason')}<textarea value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} rows={2} /></label><button type="button" className="button secondary" disabled={stale || urgency === '' || !reason.trim()} onClick={() => void submit()}>{t('urgencySave')}</button></fieldset>{saved && <p role="status">{t('urgencyChanged')}</p>}{Boolean(error) && <ErrorNotice error={error} />}</details>}
  </section>
}
