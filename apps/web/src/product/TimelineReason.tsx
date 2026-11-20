import { urgencyLabel } from './safetyDisplay'
import { useTranslation } from 'react-i18next'

function parseUrgencyAudit(reason: string) {
  try {
    const value: unknown = JSON.parse(reason)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const record = value as Record<string, unknown>
    const validUrgency = (input: unknown): input is number => typeof input === 'number' && Number.isInteger(input) && input >= 0 && input <= 3
    if (!validUrgency(record.previous) || !validUrgency(record.effective) || !validUrgency(record.automatic) ||
        typeof record.reason !== 'string' || !record.reason.trim() || record.reason.length > 1000 ||
        typeof record.assessment_version !== 'number' || !Number.isInteger(record.assessment_version) || record.assessment_version < 1) return null
    return { previous: record.previous, effective: record.effective, automatic: record.automatic, reason: record.reason }
  } catch { return null }
}

function parseLocationAudit(reason: string) {
  try {
    const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    const value: unknown = JSON.parse(reason)
    if (!object(value) || !['confirm', 'answer', 'defer'].includes(String(value.action)) ||
        typeof value.reason !== 'string' || !value.reason.trim() || value.reason.length > 1000 ||
        !object(value.before) || !object(value.before.location) ||
        !object(value.after) || !object(value.after.location)) return null
    const location = value.after.location
    if (!['pending', 'suggested', 'needs_confirmation', 'confirmed', 'staff_review'].includes(String(location.state)) ||
        typeof location.version !== 'number' || !Number.isInteger(location.version) || location.version < 1) return null
    if ((value.action === 'confirm' && location.state !== 'confirmed') ||
        (value.action === 'defer' && location.state !== 'staff_review') ||
        (value.action === 'answer' && location.state === 'confirmed')) return null
    let selected: string | null = null
    if (location.state === 'confirmed') {
      if (!object(location.selected) || typeof location.selected.label !== 'string' ||
          !location.selected.label.trim() || location.selected.label.length > 300 ||
          typeof location.selected.code !== 'string' || !location.selected.code.startsWith('DEMO-')) return null
      selected = location.selected.label
    } else if (location.selected != null) return null
    return { action: String(value.action), reason: value.reason, selected }
  } catch { return null }
}

function parseEditAudit(reason: string) {
  try {
    const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    const value: unknown = JSON.parse(reason)
    const category = (input: unknown): input is string => typeof input === 'string' && (/^[A-Z][A-Z_]{0,49}$/.test(input) || /^[A-Z0-9][A-Z0-9:_-]{0,29}$/.test(input))
    if (!object(value) || typeof value.reason !== 'string' || !value.reason.trim() || value.reason.length > 1000 ||
        !category(value.before_category) || !category(value.after_category) ||
        !object(value.before_location) || !object(value.after_location)) return null
    const location = value.after_location
    if (!object(value.routing) || value.routing.status !== 'suggested' || value.routing.delivery_status !== 'not_sent' || value.version !== location.version) return null
    if (!['pending', 'suggested', 'needs_confirmation', 'confirmed', 'staff_review'].includes(String(location.state)) ||
        typeof location.version !== 'number' || !Number.isInteger(location.version) || location.version < 1) return null
    let selected: string | null = null
    if (location.state === 'confirmed') {
      if (!object(location.selected) || typeof location.selected.label !== 'string' ||
          !location.selected.label.trim() || location.selected.label.length > 300 ||
          typeof location.selected.code !== 'string' || !(location.selected.code.startsWith('DEMO-') || (['service', 'catalog'].includes(String(location.source_type)) && /^[A-Z0-9][A-Z0-9:_-]{0,29}$/.test(location.selected.code)))) return null
      selected = location.selected.label
    } else if (location.selected != null) return null
    const label = (input: unknown) => typeof input === 'string' && input.trim() && input.length <= 300 ? input : null
    return { reason: value.reason, before: value.before_category, after: value.after_category, beforeLabel: label(value.before_category_label), afterLabel: label(value.after_category_label), selected }
  } catch { return null }
}

function fieldOverrideReason(reason: string | null): string | null {
  try {
    const value: unknown = JSON.parse(reason ?? '')
    return value && typeof value === 'object' && 'reason' in value && typeof value.reason === 'string' && value.reason.trim() && value.reason.length <= 1000 ? value.reason : null
  } catch { return null }
}

export function TimelineReason({ type, reason }: { type: string; reason: string | null }) {
  const { t } = useTranslation('product')
  if (type === 'assignment_field_override') {
    const value = fieldOverrideReason(reason)
    return value ? <p>예외 배정 사유: {value}</p> : <p>상세 사유는 담당자에게 표시돼요.</p>
  }
  if (type === 'edited') {
    const audit = reason ? parseEditAudit(reason) : null
    if (!audit) return <p>{t('editAuditUnavailable')}</p>
    const categoryLabel = (code: string, snapshot: string | null) => snapshot ?? t(`categoryAuditLabels.${code}`, { defaultValue: `분류 코드 ${code}` })
    return <div><p>{t('editAuditSummary')}</p><p>{t('editAuditCategory', { before: categoryLabel(audit.before, audit.beforeLabel), after: categoryLabel(audit.after, audit.afterLabel) })}</p><p>{t('locationAuditReason', { reason: audit.reason })}</p><p>{audit.selected ? t('locationAuditSelected', { location: audit.selected }) : t('locationUnknown')}</p></div>
  }
  if (type === 'location_updated') {
    const audit = reason ? parseLocationAudit(reason) : null
    if (!audit) return <p>{t('locationAuditUnavailable')}</p>
    return <div><p>{t(`locationAuditAction.${audit.action}`)}</p><p>{t('locationAuditReason', { reason: audit.reason })}</p>{audit.selected && <p>{t('locationAuditSelected', { location: audit.selected })}</p>}</div>
  }
  if (type === 'urgency_overridden') {
    const audit = reason ? parseUrgencyAudit(reason) : null
    if (!audit) return <p>{t('urgencyAuditUnavailable')}</p>
    return <div className="urgency-audit"><p>{t('urgencyAuditChange', { previous: urgencyLabel(audit.previous), effective: urgencyLabel(audit.effective) })}</p><p>{t('urgencyAuditAutomatic', { automatic: urgencyLabel(audit.automatic) })}</p><p>{t('urgencyAuditReason', { reason: audit.reason })}</p></div>
  }
  return reason ? <p>{reason}</p> : null
}
