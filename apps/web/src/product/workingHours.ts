import type { components } from '../api/schema'

export type WorkingHours = components['schemas']['WorkerHours']

export type HoursDraft = {
  shift_start: string
  shift_end: string
  lunch_start: string
  lunch_end: string
}

export function hoursDraft(hours?: WorkingHours | null): HoursDraft {
  return {
    shift_start: hours?.shift_start ?? '',
    shift_end: hours?.shift_end ?? '',
    lunch_start: hours?.lunch_start ?? '',
    lunch_end: hours?.lunch_end ?? '',
  }
}

export function formatOffset(minutes: number) {
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
}

export function offsetMinutes(value: string) {
  if (!isClockTime(value) && value !== '24:00') return NaN
  const [hours, minutes] = value.split(':').map(Number)
  return hours * 60 + minutes
}

export function parseHours(draft: HoursDraft): WorkingHours | null {
  const { shift_start, shift_end, lunch_start, lunch_end } = draft
  if (![shift_start, shift_end].every(isClockTime) || shift_start >= shift_end) return null
  if (!lunch_start && !lunch_end) return { shift_start, shift_end, lunch_start: null, lunch_end: null }
  if (![lunch_start, lunch_end].every(isClockTime) ||
    !(shift_start < lunch_start && lunch_start < lunch_end && lunch_end < shift_end)) return null
  return draft
}

function isClockTime(time: string) {
  return /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(time)
}
