import type { Ticket } from './types'
import { currentAssignmentCandidates, locationDraftScope, suggestedAssignmentPlace, useAssignmentCandidates, useAssignmentDataset, type LocationDraftControl } from './assignmentLocation'
import { ErrorNotice } from './shared'

export function AssignmentPlacePanel({ ticket, catalogRevision, draft, reason, setReason, disabled }: {
  ticket: Ticket
  catalogRevision: number | null | undefined
  draft: LocationDraftControl
  reason: string
  setReason: (value: string) => void
  disabled: boolean
}) {
  const query = useAssignmentCandidates(ticket, catalogRevision, !disabled)
  const dataset = useAssignmentDataset(ticket, catalogRevision, !disabled)
  const pending = query.isPending || query.isFetching || dataset.isPending || dataset.isFetching
  const result = currentAssignmentCandidates(ticket, query.data, catalogRevision)
  const scope = locationDraftScope(ticket, catalogRevision)
  const edited = draft.value?.scope === scope
  const suggested = suggestedAssignmentPlace(result)
  const value = edited ? draft.value?.code : !pending && !query.isError && !dataset.isError && dataset.data?.locations?.some(place => place.code === suggested) ? suggested : ''
  const selected = result?.candidates.find(candidate => candidate.code === value)
  const places = result?.candidates.filter(candidate => dataset.data?.locations?.some(place => place.code === candidate.code)) ?? []
  return <section className="content-card assignment-place-panel">
    <h2>배정 위치 확인</h2>
    {ticket.location_text && <p>신고자가 입력한 위치 · <strong>{ticket.location_text}</strong></p>}
    {pending && places.length === 0 && <p className="field-hint" role="status">등록 위치 후보를 불러오고 있어요.</p>}
    {query.isError && <ErrorNotice error={query.error} retry={() => void query.refetch()} />}
    {dataset.isError && <ErrorNotice error={dataset.error} retry={() => void dataset.refetch()} />}
    {result && !dataset.isError && dataset.data && <><p className="field-hint">먼저 장소를 고르고 사유를 남겨 주세요. 선택은 배정 폼에 반영돼요.</p>
      {!value && result.qr_text_conflict && <p className="notice">QR과 입력 위치가 달라요. 알맞은 장소를 골라 주세요.</p>}
      {!value && !result.qr_text_conflict && result.status === 'multiple' && <p className="field-hint">가능한 위치가 여러 곳이에요. 알맞은 장소를 골라 주세요.</p>}
      {places.length > 0 ? <div className="assignment-place-options" role="group" aria-label="배정 위치 후보">
        {places.map(candidate => <button type="button" className="button secondary assignment-place-option" key={candidate.code} aria-pressed={value === candidate.code} disabled={disabled || pending} onClick={() => {
          if (candidate.code !== value) setReason('')
          draft.onChange({ scope, code: candidate.code })
        }}>
          <strong>{candidate.label}</strong>
          {value === candidate.code && <small>{edited ? '선택됨' : '추천 위치'}</small>}
        </button>)}
      </div> : <p className="field-hint">일치하는 후보가 없어요. 배정 폼에서 등록 위치를 검색할 수 있어요.</p>}
    </>}
    {value && <div className="location-draft-reason">
      <p className="field-hint">{selected?.label ?? '검색에서 선택한 위치'}를 배정 폼에 반영했어요.</p>
      <label>위치 확인 사유<textarea rows={2} maxLength={1000} disabled={disabled} value={reason} onChange={event => setReason(event.target.value)} /></label>
      <div className="detail-controls">
        <button type="button" className="button secondary" disabled={disabled} onClick={() => setReason('신고 위치와 등록 위치가 일치해요')}>신고 위치와 일치</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => setReason('현장 근거로 위치를 확인했어요')}>현장 근거 확인</button>
      </div>
      <a href="#category-review">배정 폼에서 확인</a>
    </div>}
  </section>
}
