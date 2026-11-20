import type { Ticket } from './types'

export function displayLocation(ticket: Ticket): string {
  return ticket.location?.state === 'confirmed' && ticket.location.selected
    ? ticket.location.selected.label
    : ticket.location_text
}
