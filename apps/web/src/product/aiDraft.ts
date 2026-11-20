import type { Ticket } from './types'

const analysisEvents = new Set([
  'analysis_succeeded',
  'analysis_matched_replay',
  'analysis_unmatched',
  'analysis_failed',
  'analysis_drafted',
  'analysis_live_failed',
])

/** Latest intake-analysis event type, ignoring unrelated workflow events. */
function latestAnalysisEvent(ticket: Pick<Ticket, 'timeline'>): string | null {
  const events = ticket.timeline ?? []
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (analysisEvents.has(events[index].type)) return events[index].type
  }
  return null
}

/** True only when the most recent analysis attempt was a live model call that failed. */
export function liveAnalysisFailed(ticket: Pick<Ticket, 'ai_status' | 'timeline'>): boolean {
  return ticket.ai_status === 'failed' && latestAnalysisEvent(ticket) === 'analysis_live_failed'
}
