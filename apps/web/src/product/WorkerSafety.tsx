import { safetyUrgencies, urgencyLabel, type SafetyProjection } from './safetyDisplay'
import './workerSafety.css'

export function WorkerSafety({ safety, compact = false }: { safety: SafetyProjection | null | undefined; compact?: boolean }) {
  const values = safety ? safetyUrgencies(safety) : null
  const current = values?.current ?? null
  const emphasis = current === 0 ? ' urgent' : current === 1 ? ' priority' : ''
  if (compact && current !== 0 && current !== 1) return null
  if (compact) return <span className={`worker-safety-badge${emphasis}`}>{urgencyLabel(current)}</span>
  return <section className={`worker-safety${emphasis}`} aria-label="작업 안전 판단">
    <strong>현재 적용 긴급도 · {urgencyLabel(current)}</strong>
    {current === 0 && <p>긴급 위험 신호가 있어요. 현장 안전을 먼저 확인해 주세요.</p>}
    {current === 1 && <p>우선 확인이 필요한 위험 신호가 있어요.</p>}
    {current === null && <p>아직 긴급도 판단이 없어요. 원문과 현장 상황을 확인해 주세요.</p>}
    {values && <p>이 신고 자체 · {urgencyLabel(values.ownCurrent)}{values.linked !== null && <> · 연결 신고 위험 기준 · {urgencyLabel(values.linked)}</>}</p>}
    {(safety?.override_reason || safety?.override_applied) && <p>담당자가 이 신고의 긴급도를 조정했어요.</p>}
    {safety?.linked_requires_review && <p>연결 신고의 위험 신호를 담당자가 확인해야 해요.</p>}
  </section>
}
