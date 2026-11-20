import type { Ticket } from './types'

export function needsSafetyWarning(ticket: Ticket): boolean {
  const safety = ticket.safety
  if (!safety) return false
  return Boolean(safety.emergency_banner) || Math.min(safety.effective_urgency ?? 3, safety.linked_rule_floor ?? 3) <= 1
}

export type SafetyProjection = Pick<NonNullable<Ticket['safety']>, 'own_automatic_urgency' | 'own_effective_urgency' | 'linked_rule_floor' | 'linked_requires_review' | 'rule_floor' | 'ai_urgency' | 'automatic_urgency' | 'effective_urgency'> & { assessment_status?: string; override_reason?: string | null; override_applied?: boolean }
export function urgencyLabel(value: number | null | undefined): string {
  return value === 0 ? '긴급' : value === 1 ? '우선' : value === 2 ? '일반' : value === 3 ? '경미' : '긴급도 확인 전'
}
export function safetyUrgencies(safety: SafetyProjection) {
  const hasRule = safety.rule_floor != null && safety.rule_floor < 3
  const autoKnown = hasRule || safety.ai_urgency != null
  const rawAuto = safety.own_automatic_urgency ?? safety.automatic_urgency
  const ownAutomatic = autoKnown ? ((safety.assessment_status === 'live_priority' || (safety.assessment_status == null && safety.own_automatic_urgency != null)) ? rawAuto : Math.min(rawAuto, hasRule ? safety.rule_floor : 3)) : null
  const ownCurrent = (safety.override_reason || safety.override_applied)
    ? safety.own_effective_urgency ?? (safety.linked_rule_floor < 3 ? null : safety.effective_urgency)
    : ownAutomatic
  const linked = safety.linked_rule_floor != null && safety.linked_rule_floor < 3 ? safety.linked_rule_floor : null
  const current = linked == null ? ownCurrent : Math.min(ownCurrent ?? 3, linked)
  return { ownAutomatic, ownCurrent, linked, current }
}
