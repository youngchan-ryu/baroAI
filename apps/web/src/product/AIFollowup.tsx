import { useRef, useState } from 'react'
import { ApiError, request } from './api'
import { useOptionalRuntime } from './runtime'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import type { Ticket } from './types'
import './detailSpacing.css'

type Followup = NonNullable<Ticket['ai_followup']>
type Response = { analyze_with_ai?: boolean; action: 'answer' | 'skip'; answers: { question_id: 'q1' | 'q2'; answer: string }[] }
export function AIFollowup({ ticket, staff = false, onUpdate }: { ticket: Ticket; staff?: boolean; onUpdate: () => void }) {
  const { session } = useSession()
  const runtime = useOptionalRuntime()
  const [analyze, setAnalyze] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const pending = useRef<{ body: Response; key: string } | null>(null)
  const [retry, setRetry] = useState(false)
  const [sent, setSent] = useState(false)
  const followup: Followup | null | undefined = ticket.ai_followup
  if (!session || !followup || !followup.questions.length) return null
  const questionSource = followup.reused ? '재사용' : followup.source === 'replay' ? '재생' : followup.reused === false ? '실호출' : null
  if (staff) return followup.status === 'answered' ? <section className="content-card"><h2>신고자 추가 답변</h2><p className="field-hint">AI 질문{questionSource && ` · ${questionSource}`}</p>{followup.answers.map(answer => <div key={answer.question_id}><p>{followup.questions.find(q => q.id === answer.question_id)?.question}</p><p className="ticket-body">{answer.answer}</p></div>)}</section> : null
  if (followup.status !== 'pending' || sent) return null
  async function send(action: 'answer' | 'skip') {
    if (!session || busy || !followup) return
    const answers = followup.questions.flatMap(q => values[q.id]?.trim() ? [{ question_id: q.id, answer: values[q.id].trim() }] : [])
    if (!pending.current) {
      if (action === 'answer' && !answers.length) return
      pending.current = { body: { action, answers: action === 'skip' ? [] : answers, ...(action === 'answer' && runtime?.mode === 'service' ? { analyze_with_ai: analyze } : {}) }, key: crypto.randomUUID() }
    }
    setBusy(true); setError(null)
    try {
      await request(`/tickets/${ticket.id}/ai-followup`, { method: 'POST', body: pending.current.body, key: pending.current.key }, { csrf: session.csrf_token })
      pending.current = null; setRetry(false); setSent(true); onUpdate()
    } catch (value) {
      setError(value)
      if (value instanceof ApiError && [400, 403, 404, 409, 422].includes(value.status)) { pending.current = null; setRetry(false); onUpdate() }
      else setRetry(true)
    } finally { setBusy(false) }
  }
  return <section className="content-card followup-card">
    <h2>조금만 더 알려 주시면 확인에 도움이 돼요</h2>
    <p className="field-hint">신고는 이미 접수됐어요. 답변은 선택 사항이에요. {questionSource === '재사용' || questionSource === '재생' ? `AI 질문 · ${questionSource}` : 'AI가 제안한 질문이에요.'}</p>
    {followup.questions.map(q => <div className="followup-question" key={q.id}>
      <label htmlFor={`followup-${q.id}`}>{q.question}</label>
      {q.options.length > 0 && <div className="followup-choices" role="group" aria-label={`${q.question} 빠른 답변`}>
        {q.options.map(option => <button className="button secondary followup-choice" type="button" key={option} disabled={busy || retry} aria-pressed={values[q.id] === option} onClick={() => setValues(previous => ({ ...previous, [q.id]: option }))}>{option}</button>)}
      </div>}
      <input id={`followup-${q.id}`} placeholder={q.options.length ? '선택하거나 직접 적어 주세요' : '답변을 적어 주세요'} maxLength={200} value={values[q.id] ?? ''} disabled={busy || retry} onChange={event => setValues({ ...values, [q.id]: event.target.value })} />
    </div>)}
    {runtime?.mode === 'service' && <label className="followup-consent"><input type="checkbox" checked={analyze} disabled={busy || retry} onChange={event => setAnalyze(event.target.checked)} /><span>답변을 AI 분석에도 사용 (선택). 연락처 등 개인정보는 입력하지 말아 주세요.</span></label>}
    <div className="detail-controls">{retry
      ? <button type="button" className="button primary" disabled={busy} onClick={() => void send(pending.current?.body.action ?? 'answer')}>응답 다시 확인</button>
      : <><button type="button" className="button primary" disabled={busy || !Object.values(values).some(v => v.trim())} onClick={() => void send('answer')}>답변 보내기</button><button type="button" className="button secondary" disabled={busy} onClick={() => void send('skip')}>건너뛰기</button></>}
    </div>
    {Boolean(error) && <ErrorNotice error={error} />}
  </section>
}
