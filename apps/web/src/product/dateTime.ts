const zone = 'Asia/Seoul'
const parts = (value: string | number, options: Intl.DateTimeFormatOptions) => {
  const stamp = typeof value === 'number' ? value : Date.parse(value)
  if (!Number.isFinite(stamp) || Math.abs(stamp) > 8.64e15) return null
  const items = new Intl.DateTimeFormat('ko-KR', { timeZone: zone, ...options }).formatToParts(stamp)
  return (key: Intl.DateTimeFormatPartTypes) => items.find(item => item.type === key)?.value ?? ''
}
export function dateKey(value: string): string {
  const p = parts(value, { year: 'numeric', month: '2-digit', day: '2-digit' })
  return p ? `${p('year')}-${p('month')}-${p('day')}` : ''
}
export function dateTime(value: string | number, style: 'short' | 'full' | 'day' | 'clock' = 'short', now = Date.now()): string {
  const p = parts(value, { year: 'numeric', month: 'numeric', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
  if (!p) return '시각 확인 필요'
  const current = parts(now, { year: 'numeric' })
  const prefix = style === 'full' || p('year') !== current?.('year') ? `${p('year')}. ` : ''
  if (style === 'clock') return `${p('hour')}:${p('minute')}`
  const day = `${prefix}${p('month')}/${p('day')}(${p('weekday')})`
  return style === 'day' ? day : `${day} ${p('hour')}:${p('minute')}`
}
// Calendar comparisons use Seoul civil days, independent of the browser timezone.
export function displayDate(value: string | number, now = Date.now()): string {
  const p = parts(value, { year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
  const current = parts(now, { year: 'numeric', month: '2-digit', day: '2-digit' })
  if (!p || !current) return '시각 확인 필요'
  const civil = (x: NonNullable<typeof p>) => Date.UTC(Number(x('year')), Number(x('month')) - 1, Number(x('day'))) / 86400000
  const day = civil(p), today = civil(current)
  const clock = `${p('hour')}:${p('minute')}`
  if (day === today) return `오늘 ${clock}`
  const monday = today - ((today + 3) % 7 + 7) % 7
  if (day >= monday && day < monday + 7) return `${Number(p('month'))}/${Number(p('day'))}(${p('weekday')}) ${clock}`
  return `${p('year')}.${p('month')}.${p('day')}`
}
export function relativeTime(value: string | number, now = Date.now()): string {
  const timestamp = typeof value === 'number' ? value : Date.parse(value)
  if (!Number.isFinite(timestamp) || Math.abs(timestamp) > 8.64e15) return '시각 확인 필요'
  if (timestamp > now) return displayDate(value, now)
  const minutes = Math.floor((now - timestamp) / 60000)
  return minutes < 1 ? '방금 전' : minutes < 60 ? `${minutes}분 전` : minutes < 1440 ? `${Math.floor(minutes / 60)}시간 전` : `${Math.floor(minutes / 1440)}일 전`
}
export function waiting(value: string, now = Date.now()): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return '대기 시간 확인 필요'
  const minutes = Math.floor(Math.max(0, now - timestamp) / 60000)
  return minutes < 60 ? `${minutes}분 대기` : minutes < 1440 ? `${Math.floor(minutes / 60)}시간 대기` : `${Math.floor(minutes / 1440)}일 대기`
}
// Display thresholds are assumptions for highlighting, not service deadlines.
export const WAIT_NOTICE_MINUTES = [30, 240, 1440, 4320] as const
export function isLongWait(value: string, urgency: number | null | undefined, now = Date.now()): boolean {
  const timestamp = Date.parse(value)
  return urgency != null && Number.isInteger(urgency) && urgency >= 0 && urgency <= 3 && Number.isFinite(timestamp) && now >= timestamp && now - timestamp >= WAIT_NOTICE_MINUTES[urgency] * 60000
}
