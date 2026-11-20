import type { HoursDraft } from './workingHours'

export function WorkerHoursFields({ draft, onChange }: { draft: HoursDraft; onChange: (value: HoursDraft) => void }) {
  return <div className="planning-fields">
    <label>근무 시작<input type="time" step="60" required value={draft.shift_start} onChange={event => onChange({ ...draft, shift_start: event.target.value })} /></label>
    <label>근무 종료<input type="time" step="60" required value={draft.shift_end} onChange={event => onChange({ ...draft, shift_end: event.target.value })} /></label>
    <label>점심 시작 (선택)<input type="time" step="60" value={draft.lunch_start} onChange={event => onChange({ ...draft, lunch_start: event.target.value })} /></label>
    <label>점심 종료 (선택)<input type="time" step="60" value={draft.lunch_end} onChange={event => onChange({ ...draft, lunch_end: event.target.value })} /></label>
  </div>
}
