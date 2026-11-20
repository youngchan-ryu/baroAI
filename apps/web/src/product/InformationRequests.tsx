import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, persistentKey, request } from './api'
import { useOptionalRuntime } from './runtime'
import { useSession } from './session'
import { ErrorNotice, Loading, Photos } from './shared'
import { UploadField, useUploads } from './UploadField'
import type { Photo, Ticket } from './types'
import './detailSpacing.css'

type InformationRequest = { id: string; question: string; status: 'open' | 'answered' | 'cancelled'; answer: string | null; mail_status: string; photos: Photo[] }
type Key = { fingerprint: string; key: string } | null
const mailLabel: Record<string, string> = {
  provider_accepted: '메일 제공자가 접수했어요. 수신 여부는 확인되지 않았어요.',
  non_provider_accepted: '로컬 또는 모의 메일로 처리됐어요. 실제 수신은 확인되지 않았어요.',
  demo_not_sent: '시연에서는 메일을 보내지 않아요.', not_sent: '메일을 보내지 않았어요.',
  pending: '메일 발송 대기 중이에요.', queued: '메일 발송 대기 중이에요.', claimed: '메일 발송 준비 중이에요.', sending: '메일 전송 중이에요.',
  failed: '메일 발송에 실패했어요.', uncertain: '메일 전송 결과를 확인할 수 없어요.', unknown: '메일 전송 결과를 확인할 수 없어요.',
  cancelled: '메일 전송이 취소됐어요.', expired: '메일 발송 기한이 지났어요.',
}
const normalizeQuestion = (value: string) => value.normalize().trim().replace(/\s+/g, ' ')

export function InformationRequests({ ticket, staff = false, onUpdate, onAttention, settingsTarget }: { ticket: Ticket; staff?: boolean; onUpdate: () => void; onAttention?: (value: boolean) => void; settingsTarget?: HTMLElement | null }) {
  const { session, cacheScope } = useSession()
  const runtime = useOptionalRuntime()
  const [confirmedConsent, setConfirmedConsent] = useState<boolean | null>(null)
  const client = useQueryClient()
  const path = `/tickets/${encodeURIComponent(ticket.id)}/information-requests`
  const query = useQuery({ queryKey: ['information', cacheScope, ticket.id], queryFn: ({ signal }) => request<InformationRequest[]>(path, { signal }), retry: false, enabled: Boolean(session) })
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [pendingAction, setPendingAction] = useState<{ suffix: string; body: unknown; key: string } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<Key>(null)
  const consent = confirmedConsent ?? ticket.information_mail_allowed
  const [pendingConsent, setPendingConsent] = useState<{ allowed: boolean; key: string } | null>(null)
  const needsAttention = Boolean(session && (query.isError || (!query.isPending && !Array.isArray(query.data)) || error || pendingAction || busy || question.trim()))
  useEffect(() => { onAttention?.(needsAttention) }, [needsAttention, onAttention])
  async function submit(suffix: string, body: unknown, answerKey?: string) {
    if (!session || busy) return false
    setBusy(true); setError(null)
    try {
      key.current = persistentKey(key.current, { suffix, body })
      const actionKey = answerKey ?? key.current.key
      if (!answerKey) setPendingAction({ suffix, body, key: actionKey })
      await request(`${path}${suffix}`, { method: 'POST', body, key: actionKey }, { csrf: session.csrf_token })
      setPendingAction(null); key.current = null; setQuestion('')
      await query.refetch(); onUpdate(); void client.invalidateQueries({ queryKey: ['tickets', cacheScope] }); return true
    } catch (value) { setError(value); if (value instanceof ApiError && [400, 403, 404, 409, 422].includes(value.status)) { key.current = null; setPendingAction(null); void query.refetch(); return true } return false }
    finally { setBusy(false) }
  }
  async function setConsent(allowed: boolean) {
    if (!session || busy) return
    setBusy(true); setError(null)
    try {
      const operation = pendingConsent ?? { allowed, key: crypto.randomUUID() }
      setPendingConsent(operation)
      const result = await request<{ information_mail_allowed: boolean }>(`/tickets/${ticket.id}/information-mail-consent`, { method: 'POST', body: { allowed: operation.allowed }, key: operation.key }, { csrf: session.csrf_token })
      setConfirmedConsent(result.information_mail_allowed); setPendingConsent(null); onUpdate()
    } catch (value) { setError(value); if (value instanceof ApiError && [400, 403, 404, 409, 422].includes(value.status)) { setPendingConsent(null); onUpdate() } } finally { setBusy(false) }
  }
  if (!session) return null
  if (query.isPending) return <Loading />
  if (query.isError) return <section className="content-card"><h2>추가 정보 요청</h2><ErrorNotice error={query.error} retry={() => void query.refetch()} /></section>
  if (!Array.isArray(query.data)) return <section className="content-card"><h2>추가 정보 요청</h2><p role="status">요청 목록을 확인할 수 없어요.</p><button type="button" className="text-button" onClick={() => void query.refetch()}>다시 확인</button></section>
  const items = query.data
  const open = items.find(item => item.status === 'open')
  const followup = ticket.ai_followup
  const answeredIds = new Set(followup?.answers.filter(item => item.answer.trim()).map(item => item.question_id))
  const answeredQuestions = new Set(items.filter(item => item.status === 'answered' && item.answer?.trim()).map(item => normalizeQuestion(item.question)))
  const suggestions = followup?.questions.filter(item => !answeredIds.has(item.id) && !answeredQuestions.has(normalizeQuestion(item.question))) ?? []
  const settings = !staff && consent != null && <section className="content-card"><div className="notification-setting"><label htmlFor="information-mail-toggle">알림 설정 · 추가 질문 메일</label><input id="information-mail-toggle" type="checkbox" role="switch" checked={consent} disabled={busy || Boolean(pendingAction) || Boolean(pendingConsent)} onChange={event => void setConsent(event.target.checked)} /></div>{pendingConsent && <button type="button" className="text-button" disabled={busy} onClick={() => void setConsent(pendingConsent.allowed)}>메일 동의 변경 다시 확인</button>}<p className="field-hint">질문은 이 화면에서도 확인할 수 있어요.</p>{Boolean(error) && <ErrorNotice error={error} />}</section>
  return <>
    {(staff || items.length > 0) && <section className="content-card information-panel"><h2>추가 정보 요청</h2>
    {items.map(item => <article className="information-item" key={item.id}><h3>{item.status === 'open' ? '답변 대기' : item.status === 'answered' ? '답변 도착' : '취소된 요청'}</h3><p className="ticket-body">{item.question}</p>{staff && <p className="field-hint">{mailLabel[item.mail_status] ?? '메일 상태를 확인할 수 없어요.'}</p>}{item.answer && <><p className="ticket-body">{item.answer}</p><Photos photos={item.photos} /></>}
      {staff && item.status === 'open' && <><button type="button" className="button secondary" disabled={busy || Boolean(pendingAction)} onClick={() => void submit(`/${item.id}/cancel`, {})}>요청 취소</button><p className="field-hint">전송 중인 메일의 회수는 보장되지 않아요.</p></>}
      {!staff && item.status === 'open' && <ReporterAnswer key={item.id} itemId={item.id} csrf={session.csrf_token} service={runtime?.mode === 'service'} busy={busy} submit={submit} />}
    </article>)}
    {staff && !open && items.length < 3 && <div className="staff-question-form">
      {suggestions.length > 0 && <div className="question-suggestions" role="group" aria-label="AI 추천 질문">
        {suggestions.map(item => <button type="button" className="button secondary question-suggestion" key={item.id} aria-pressed={question === item.question} disabled={busy || Boolean(pendingAction)} onClick={() => setQuestion(item.question)}>AI 초안 · {item.question}{followup?.source === 'replay' ? ' (재생)' : ''}</button>)}
      </div>}
      <label htmlFor="information-question">신고자에게 질문</label>
      <textarea id="information-question" value={question} rows={3} maxLength={500} placeholder={suggestions.length ? '추천 질문을 선택하거나 직접 적어 주세요' : '직접 적어 주세요'} disabled={busy || Boolean(pendingAction)} onChange={event => setQuestion(event.target.value)} />
      <div className="detail-controls">
        <button type="button" className="button secondary" disabled={busy || Boolean(pendingAction) || !question.trim()} onClick={() => void submit('', { question: question.trim() })}>추가 정보 요청</button>
      </div>
      <p className="field-hint">질문을 보내면 저장돼요. 메일 발송 여부는 따로 표시됩니다.</p>
    </div>}
    {pendingAction && <><p className="field-hint">요청 결과를 확인하지 못했어요. 같은 요청을 다시 확인해 주세요.</p><button type="button" className="button secondary" disabled={busy} onClick={() => void submit(pendingAction.suffix, pendingAction.body, pendingAction.key)}>요청 다시 확인</button></>}{staff && items.length >= 3 && !open && <p>추가 정보 요청 한도에 도달했어요.</p>}{Boolean(error) && <ErrorNotice error={error} />}
  </section>}
  {settingsTarget === undefined ? settings : settingsTarget && settings ? createPortal(settings, settingsTarget) : null}
  </>
}

function ReporterAnswer({ itemId, csrf, service, busy, submit }: { itemId: string; csrf: string; service: boolean; busy: boolean; submit: (suffix: string, body: unknown, key?: string) => Promise<boolean> }) {
  const [answer, setAnswer] = useState('')
  const [analyze, setAnalyze] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [pending, setPending] = useState<{ itemId: string; answer: string; upload_ids: string[]; key: string; analyze_with_ai?: boolean } | null>(null)
  const uploads = useUploads()
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  async function send() {
    if (busy || preparing) return
    if (pending) {
      if (pending.itemId !== itemId) return
      const done = await submit(`/${pending.itemId}/answer`, { answer: pending.answer, upload_ids: pending.upload_ids, ...(pending.analyze_with_ai !== undefined ? { analyze_with_ai: pending.analyze_with_ai } : {}) }, pending.key)
      if (done && mounted.current) setPending(null)
      return
    }
    if (!answer.trim() || !uploads.ready || uploads.preparing || uploads.protecting) return
    setPreparing(true); setError(null)
    try {
      const ids = await uploads.upload({ csrf })
      if (!mounted.current) return
      const value = { itemId, answer: answer.trim(), upload_ids: ids, key: crypto.randomUUID(), ...(service ? { analyze_with_ai: analyze } : {}) }
      setPending(value)
      const done = await submit(`/${value.itemId}/answer`, { answer: value.answer, upload_ids: ids, ...(value.analyze_with_ai !== undefined ? { analyze_with_ai: value.analyze_with_ai } : {}) }, value.key)
      if (done && mounted.current) setPending(null)
    } catch (value) { if (mounted.current) setError(value) }
    finally { if (mounted.current) setPreparing(false) }
  }
  const frozen = busy || preparing || Boolean(pending)
  return <div className="information-answer"><label htmlFor="information-answer">답변</label><textarea id="information-answer" value={answer} maxLength={2000} disabled={frozen} onChange={event => setAnswer(event.target.value)} /><UploadField uploads={uploads} auth={{ csrf }} disabled={frozen} autoPrepare />{service && <label><input type="checkbox" checked={analyze} disabled={frozen} onChange={event => setAnalyze(event.target.checked)} /> 답변을 AI 분석에도 사용 (선택). 연락처 등 개인정보는 입력하지 말아 주세요.</label>}<button type="button" className="button primary" disabled={busy || preparing || (!pending && (!answer.trim() || !uploads.ready || uploads.preparing || uploads.protecting))} onClick={() => void send()}>{pending ? '답변 다시 보내기' : '답변 보내기'}</button>{Boolean(error) && <ErrorNotice error={error} />}</div>
}
