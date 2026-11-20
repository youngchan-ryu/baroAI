import { displayLocation } from './locationDisplay'
import { dateTime } from './dateTime'
import type { Ticket } from './types'

export type Period = 'all' | 'today' | '7' | '30' | 'custom'
export type Sort = 'urgent' | 'oldest' | 'newest'
const DAY = 86_400_000
const KST = 9 * 3_600_000

function dayStart(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, month, day] = value.split('-').map(Number)
  const stamp = Date.UTC(year, month - 1, day)
  const parsed = new Date(stamp)
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day ? stamp - KST : null
}

export function periodBounds(period: Period, from: string, to: string, now = Date.now()): { start: number; end: number } | null {
  if (period === 'all') return null
  if (period === 'custom') {
    const start = dayStart(from), last = dayStart(to)
    return start == null || last == null || start > last ? null : { start, end: last + DAY }
  }
  const today = dayStart(new Date(now + KST).toISOString().slice(0, 10))!
  const days = period === 'today' ? 1 : Number(period)
  return { start: today - (days - 1) * DAY, end: today + DAY }
}

export function matchesSearch(ticket: Ticket, query: string): boolean {
  const draft = ticket.ai_draft && !ticket.ai_draft.stale ? ticket.ai_draft : null
  const text = [ticket.public_no, ticket.text, draft?.summary, ticket.location_text, displayLocation(ticket), ticket.category_label, ticket.category_code, draft?.category_name, ticket.worker_name]
    .filter(Boolean).join(' ').toLocaleLowerCase()
  return query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean).every(term => text.includes(term))
}

export function completionAt(ticket: Ticket): string | null {
  const dates = (ticket.timeline ?? []).filter(event => event.type === 'report_reviewed' && event.to_status === 'completed' && event.from_status != null && event.from_status !== 'completed').map(event => Date.parse(event.at)).filter(Number.isFinite)
  return dates.length ? new Date(Math.max(...dates)).toISOString() : null
}
export function completionLabel(ticket: Ticket): string {
  const at = completionAt(ticket)
  return at ? `${dateTime(at, 'day')} 완료` : '완료 시각 확인 필요'
}
