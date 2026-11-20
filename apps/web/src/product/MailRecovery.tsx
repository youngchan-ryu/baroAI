import { useEffect, useRef, useState } from 'react'
import { ApiError, request } from './api'
import { ErrorNotice } from './shared'

type Candidate = { id: string; state: string; snapshot_hash: string; legacy: boolean; ticket_public_no: string; relationship: 'direct' | 'merged' }
type Pending = { item: Candidate; key: string; acknowledged: boolean }

type Props = { ticketId: string; reportId: string; reportLabel: string; csrf: string }
const noAccess = (value: unknown) => value instanceof ApiError && [403, 404].includes(value.status)

export function MailRecovery(props: Props) {
  return <MailRecoveryReport key={JSON.stringify([props.ticketId, props.reportId])} {...props} />
}

function MailRecoveryReport({ ticketId, reportId, reportLabel, csrf }: Props) {
  const [items, setItems] = useState<Candidate[] | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [initialError, setInitialError] = useState(false)
  const [message, setMessage] = useState('')
  const pendingRef = useRef<Pending | null>(null)
  const base = `/tickets/${encodeURIComponent(ticketId)}/reports/${encodeURIComponent(reportId)}/mail-recovery`
  useEffect(() => {
    let active = true
    // The server determines both authorization and whether this report has a recoverable intent.
    void request<Candidate[]>(base).then(value => { if (active) setItems(value) }).catch((value: unknown) => {
      if (active && !noAccess(value)) setInitialError(true)
    })
    return () => { active = false }
  }, [base])
  function clearIfUnavailable(value: unknown) {
    if (!noAccess(value)) return false
    pendingRef.current = null; setPending(null); setItems(null); setMessage(''); setInitialError(false)
    return true
  }
  async function load() {
    setBusy(true); setError(null); setAcknowledged({})
    try { setItems(await request<Candidate[]>(base)); setInitialError(false); setMessage('') } catch (value) { if (!clearIfUnavailable(value)) setError(value) } finally { setBusy(false) }
  }
  async function submit(item: Candidate) {
    if (busy) return
    const current = pendingRef.current ?? { item, key: crypto.randomUUID(), acknowledged: Boolean(acknowledged[item.id]) }
    pendingRef.current = current; setPending(current); setBusy(true); setError(null)
    try {
      await request(`${base}/${encodeURIComponent(current.item.id)}`, { method: 'POST', key: current.key, body: { expected_state: current.item.state, expected_snapshot_hash: current.item.snapshot_hash, acknowledge_duplicate: current.acknowledged } }, { csrf })
      pendingRef.current = null; setPending(null); setItems(null); setAcknowledged({})
      setMessage('알림 요청을 다시 접수했어요. 전송 결과는 아래에 표시돼요.')
    } catch (value) { if (!clearIfUnavailable(value)) setError(value) } finally { setBusy(false) }
  }
  if (initialError && !items && !pending && !message) return <section className="content-card" role="alert"><p>회신 알림 상태를 불러오지 못했어요.</p><button type="button" className="button secondary" disabled={busy} onClick={() => void load()}>{busy ? '확인 중…' : '알림 상태 다시 확인'}</button>{Boolean(error) && <ErrorNotice error={error} />}</section>
  if ((!items || !items.length) && !pending && !message) return null
  return <section className="content-card"><h2>승인 회신 알림 복구</h2><p>{reportLabel} · 보고 ID {reportId}</p><p>이전 알림이 실패했거나 결과를 확인할 수 없어요. 새 알림 요청을 만들며, 실제 전송 여부는 별도로 확인해야 해요. 신고자 주소는 표시하지 않아요.</p>
    {!pending && <button type="button" className="button secondary" disabled={busy} onClick={() => void load()}>알림 상태 새로고침</button>}
    {message && <p role="status">{message}</p>}
    {items && !items.length && !pending && <p>현재 복구할 알림이 없어요.</p>}
    {items?.map((item, index) => (!pending || pending.item.id === item.id) && <div key={item.id}><p>{index + 1}번 요청 · 접수번호 {item.ticket_public_no} · {item.relationship === 'direct' ? '직접 접수' : '병합 구독'} · {item.state === 'unknown' ? '전송 결과 불명' : item.state === 'failed' ? '전송 실패' : item.state === 'expired' ? '기한 만료' : '이전 근거 없음'}{item.legacy && ' · 이전 원장'}</p>
      {item.state === 'unknown' && <label><input type="checkbox" checked={pending?.acknowledged ?? Boolean(acknowledged[item.id])} disabled={busy || Boolean(pending)} onChange={event => setAcknowledged(previous => ({ ...previous, [item.id]: event.target.checked }))} />이전 메일이 도착했을 수 있고 새 요청으로 중복 수신될 수 있음을 확인했어요.</label>}
      {pending && <><p role="status">요청 결과를 확인하지 못했어요. 같은 요청으로 다시 확인하거나 최신 상태를 확인해 주세요.</p><button type="button" className="button secondary" disabled={busy} onClick={() => { pendingRef.current = null; setPending(null); void load() }}>최신 상태 확인</button></>}
      <button type="button" className="button secondary" disabled={busy || (!pending && item.state === 'unknown' && !acknowledged[item.id])} onClick={() => void submit(item)}>{pending ? '같은 요청으로 결과 확인' : '새 알림 요청'}</button></div>)}
    {Boolean(error) && <ErrorNotice error={error} />}
  </section>
}
