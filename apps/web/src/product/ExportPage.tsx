import { dateTime } from './dateTime'
import './exportPage.css'
import { useEffect, useRef, useState } from 'react'
import { api, ApiError, persistentKey, request } from './api'
import { useSession } from './session'
import { useRuntime } from './runtime'
import { ErrorNotice } from './shared'
import type { Ticket } from './types'

// Dormant CSV UI types are independent of the active API schema.
type Selection = { ticket_ids?: string[]; filters?: { from_date: string; through_date: string; statuses?: Ticket['status'][] } | null }
type Preview = { data_scope: 'demo' | 'service'; snapshot_hash: string; snapshot_at: string; columns: string[]; ticket_ids: string[]; rows: string[][]; warnings: string[] }
type Generated = { id: string; data_scope: 'demo' | 'service'; download_path: string; row_count: number; expires_at: string; content_sha256: string; expired: boolean }
type Draft = { ticket_id: string; export_description: string; export_location: string }
type Candidate = { ticket_id: string; public_no: string; status: string; category_code: string }
const columnLabels: Record<string, string> = {
  schema_version: '파일 형식', data_scope: '자료 환경', snapshot_at: '미리보기 기준 시각', snapshot_hash: '내용 근거 확인값', ticket_created_at: '접수 생성 시각', ticket_updated_at: '접수 갱신 시각', institution_mapping: '기관 매핑 상태', delivery_status: '전송 상태', public_no: '접수번호', status: '현재 업무 상태', category_code: '상세 분류', domain: '분야', suggested_department_code: '제안 부서', department_status: '부서 판단 상태', location_state: '위치 확인 단계', location_source: '위치 사전 출처', export_description: '담당자 작성 설명', export_location: '담당자 작성 위치', text_source: '문구 작성 출처', ai_urgency: 'AI 제안 긴급도', own_rule_floor: '원문 안전 규칙', own_automatic_urgency: '자체 자동 긴급도', linked_rule_floor: '연결 안전 기준', own_effective_urgency: '자체 적용 긴급도', manual_override_applied: '사람 조정 적용 여부', effective_urgency: '최종 적용 긴급도', safety_review_required: '안전 재확인 필요', emergency_banner: '긴급 안내 여부',
}
const messages: Record<string, string> = {
  export_snapshot_stale: '접수 내용이 바뀌었어요. 작성 내용은 보존했습니다. 미리보기를 다시 확인해 주세요.',
  export_selection_changed: '선택 대상이 바뀌었어요. 작성 내용은 보존했습니다. 대상을 다시 확인해 주세요.',
  export_limit_exceeded: '한 번에 최대 50건을 선택해 주세요.',
  export_selection_empty: '선택 조건에 맞는 접수가 없어요.',
  export_key_unconfigured: '파일 암호화 설정이 준비되지 않았어요. 담당자에게 확인해 주세요.',
  export_expired: '파일 보관 기간 24시간이 지났어요. 최신 내용으로 다시 준비해 주세요.',
  export_rate_limited: '15분에 최대 5회 생성할 수 있어요. 잠시 후 다시 시도해 주세요.',
}

export function ExportPage() {
  const { session } = useSession()
  const runtime = useRuntime()
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [error, setError] = useState<unknown>(null)
  useEffect(() => { const controller = new AbortController(); api.tickets(controller.signal).then(v => setTickets(v.items)).catch(e => { if (!controller.signal.aborted) setError(e) }); return () => controller.abort() }, [])
  return <>{Boolean(error) && <ErrorNotice error={error} />}{session && <ExportWorkflow key={session.user.id} csrf={session.csrf_token} tickets={tickets} service={runtime.mode === 'service'} />}</>
}

export function ExportWorkflow({ csrf, tickets, service = false }: { csrf: string; tickets: Ticket[]; service?: boolean }) {
  const [ids, setIds] = useState<string[]>([])
  const [mode, setMode] = useState<'selected' | 'filtered'>(service ? 'filtered' : 'selected')
  const [from, setFrom] = useState('')
  const [through, setThrough] = useState('')
  const [selection, setSelection] = useState<Selection | null>(null)
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [preview, setPreview] = useState<Preview | null>(null)
  const [confirmed, setConfirmed] = useState<string[]>([])
  const [reason, setReason] = useState('')
  const [generated, setGenerated] = useState<Generated | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [stale, setStale] = useState(false)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const invalidate = () => { setPreview(null); setConfirmed([]); setGenerated(null); setStale(false) }
  async function select() {
    setBusy(true); setError(null)
    const next: Selection = mode === 'selected' ? { ticket_ids: ids } : { filters: { from_date: from, through_date: through, statuses: [] } }
    try {
      const result = await request<{ items: Candidate[] }>('/admin/exports/select', { method: 'POST', body: next }, { csrf })
      setSelection(next); setCandidates(result.items)
      setDrafts(result.items.map(item => drafts.find(d => d.ticket_id === item.ticket_id) ?? { ticket_id: item.ticket_id, export_description: '', export_location: '' }))
      invalidate()
    } catch (e) { setError(e) } finally { setBusy(false) }
  }
  async function inspect() {
    if (!selection) return
    setBusy(true); setError(null)
    try { const result = await request<Preview>('/admin/exports/preview', { method: 'POST', body: { selection, rows: drafts } }, { csrf }); setPreview(result); setConfirmed([]); setGenerated(null); setStale(false) } catch (e) { setError(e) } finally { setBusy(false) }
  }
  async function generate() {
    if (!selection || !preview) return
    setBusy(true); setError(null)
    const body = { selection, rows: drafts, expected_snapshot_hash: preview.snapshot_hash, expected_snapshot_at: preview.snapshot_at, confirmed_ticket_ids: preview.ticket_ids.filter(id => confirmed.includes(id)), reason }
    key.current = persistentKey(key.current, body)
    try { setGenerated(await request<Generated>('/admin/exports', { method: 'POST', body, key: key.current.key }, { csrf })) } catch (e) { setError(e); if (e instanceof ApiError && e.status === 409) setStale(true) } finally { setBusy(false) }
  }
  async function download() {
    if (!generated) return
    setBusy(true); setError(null)
    try {
      const response = await fetch(`/api${generated.download_path}`, { cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(15_000) })
      if (!response.ok) { const value = await response.json().catch(() => null); if (response.status === 401) window.dispatchEvent(new Event('baro:unauthorized')); throw new ApiError(value?.error?.code ?? 'request_failed', response.status) }
      const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = `baroai-${generated.data_scope}-${generated.id}.csv`; link.click(); URL.revokeObjectURL(url)
    } catch (e) { setError(e); if (e instanceof ApiError && e.status === 410) setGenerated(value => value ? { ...value, expired: true } : null) } finally { setBusy(false) }
  }
  return <section className="stack export-workflow"><h1>기관 전달용 CSV {service && '(선택)'}</h1>{service && <p>기관에서 파일을 요청할 때만 준비하는 수동 전달용 파일이에요. 자동으로 전송되지 않아요.</p>}<p>담당자가 작성한 전달용 문구로 파일을 만듭니다. 기관 매핑은 미검증이며 파일 생성·다운로드는 실제 이관이나 발송이 아닙니다.</p><p>원문·이메일·사진·작업 링크·보고 원문·내부 승인 사유는 기본 파일에 포함하지 않습니다. 개인정보 자동 검출은 완전하지 않으므로 모든 셀을 직접 확인해 주세요.</p>
    {error instanceof ApiError && messages[error.code] ? <p role="alert">{messages[error.code]}</p> : Boolean(error) && <ErrorNotice error={error} />}
    <fieldset className="content-card export-stage" disabled={busy}><legend>1. 대상 선택 · 최대 50건</legend><label>선택 방식<select value={mode} onChange={e => { setMode(e.target.value as typeof mode); invalidate(); setSelection(null) }}><option value="selected">접수 직접 선택</option><option value="filtered">기간으로 선택</option></select></label>
      {mode === 'selected' ? tickets.map(ticket => <label key={ticket.id}><input type="checkbox" checked={ids.includes(ticket.id)} onChange={e => { setIds(e.target.checked ? [...ids, ticket.id] : ids.filter(id => id !== ticket.id)); invalidate(); setSelection(null) }} />{ticket.public_no} · {ticket.status}</label>) : <><label>접수 시작일 · UTC<input type="date" value={from} onChange={e => { setFrom(e.target.value); invalidate(); setSelection(null) }} /></label><label>접수 종료일 · UTC, 최대 31일<input type="date" value={through} onChange={e => { setThrough(e.target.value); invalidate(); setSelection(null) }} /></label></>}
      <button className="button secondary" onClick={() => void select()} disabled={mode === 'selected' ? ids.length === 0 || ids.length > 50 : !from || !through}>대상 확인</button>
    </fieldset>
    {selection && <fieldset className="content-card export-stage" disabled={busy}><legend>2. 전달 문구 직접 작성</legend><p>신고 원문에 포함된 개인정보나 식별자가 그대로 전달되지 않도록 필요한 설명과 위치를 따로 작성해 주세요.</p>{drafts.map((draft, index) => <div key={draft.ticket_id}><h2>{candidates[index]?.public_no}</h2><a href={`/admin/t/${draft.ticket_id}`} target="_blank" rel="noreferrer">접수 원문 별도 확인</a><label>전달용 설명 {index + 1}<textarea maxLength={1000} value={draft.export_description} onChange={e => { setDrafts(drafts.map(d => d.ticket_id === draft.ticket_id ? { ...d, export_description: e.target.value } : d)); invalidate() }} /></label><label>전달용 위치 {index + 1}<input maxLength={200} value={draft.export_location} onChange={e => { setDrafts(drafts.map(d => d.ticket_id === draft.ticket_id ? { ...d, export_location: e.target.value } : d)); invalidate() }} /></label></div>)}<p>원문은 자동 복사하지 않습니다. 문구는 줄바꿈 없이 작성해 주세요. 이메일·인증 코드/작업 링크를 쓰지 마세요. 자유 위치 문구는 등록 위치 확정과 다릅니다.</p><button className="button secondary" disabled={drafts.some(d => !d.export_description.trim() || !d.export_location.trim())} onClick={() => void inspect()}>모든 파일 값 미리보기</button></fieldset>}
    {preview && <fieldset className="content-card export-stage" disabled={busy}><legend>3. 실제 파일 값 확인 · {preview.data_scope === 'demo' ? '합성 시연' : '서비스'} 자료</legend>{preview.warnings.map(w => <p key={w}>{w}</p>)}<p>긴급도는 0~3이며 작을수록 우선합니다. 빈 값은 확인된 판단이 없음을 뜻합니다. suggested는 제안, not_sent는 미전송이며 업무 상태 transferred도 이번 파일의 전송 결과를 뜻하지 않습니다.</p><div style={{ overflowX: 'auto' }}><table><caption>CSV 전체 컬럼과 실제 셀 값 · 수식 방어 접두사 포함</caption><thead><tr>{preview.columns.map(c => <th key={c}>{columnLabels[c] ?? c}<br /><small>{c}</small></th>)}</tr></thead><tbody>{preview.rows.map((row, i) => <tr key={preview.ticket_ids[i]}>{row.map((cell, j) => <td key={j}>{cell || '빈 값'}</td>)}</tr>)}</tbody></table></div>{preview.ticket_ids.map((id, i) => <label key={id}><input type="checkbox" checked={confirmed.includes(id)} onChange={e => setConfirmed(e.target.checked ? [...confirmed, id] : confirmed.filter(value => value !== id))} />{i + 1}행 모든 값·개인정보 제외 확인</label>)}<p>파일 생성 사유는 누가 어떤 목적으로 파일을 만들었는지 감사 기록으로 남기기 위해 필요해요.</p><label>파일 생성 사유<input maxLength={1000} value={reason} onChange={e => { setReason(e.target.value); setGenerated(null) }} /></label><button className="button primary" disabled={stale || confirmed.length !== preview.ticket_ids.length || !reason.trim()} onClick={() => void generate()}>전달 준비 파일 생성 · 미전송</button>{stale && <p>작성 내용은 유지됩니다. 위 미리보기 버튼으로 최신 근거를 다시 확인해 주세요.</p>}</fieldset>}
    {generated && <section className="content-card export-stage" aria-label="생성 파일"><h2>파일 생성됨 · 미전송</h2><p>생성 시점의 고정 파일 · 기관 매핑 미검증 · 24시간 보관 · {generated.row_count}건</p><p>보관 만료: {dateTime(generated.expires_at, 'full')}</p><p>파일 확인값: {generated.content_sha256}</p><button className="button primary" disabled={busy || generated.expired} onClick={() => void download()}>CSV 다운로드</button><p>다운로드한 파일은 관리 책임에 따라 보관해 주세요. 서버 만료는 내려받은 파일을 삭제하지 않습니다.</p></section>}
  </section>
}
