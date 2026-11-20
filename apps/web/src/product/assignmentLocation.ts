import { useQuery } from '@tanstack/react-query'
import type { components } from '../api/schema'
import { request } from './api'
import { useSession } from './session'
import type { Ticket } from './types'

type CandidateResult = components['schemas']['CatalogLocationCandidates']
type Dataset = components['schemas']['ServiceLocationDataset']
export type PlaceDraft = { scope: string; code: string }
export type LocationDraftControl = { value: PlaceDraft | null; onChange: (value: PlaceDraft) => void }
export const locationDraftScope = (ticket: Ticket, catalogRevision: number | null | undefined) =>
  JSON.stringify([ticket.id, ticket.location?.version, catalogRevision])

export function useAssignmentCandidates(ticket: Ticket, catalogRevision: number | null | undefined, enabled: boolean) {
  const { cacheScope } = useSession()
  return useQuery({
    queryKey: ['atomic-candidates', cacheScope, ticket.id, ticket.location?.version, catalogRevision],
    queryFn: () => request<CandidateResult>(`/tickets/${ticket.id}/catalog-location-candidates`),
    enabled, retry: false, staleTime: 10_000, refetchOnWindowFocus: false,
  })
}

export function useAssignmentDataset(ticket: Ticket, catalogRevision: number | null | undefined, enabled: boolean) {
  const { cacheScope } = useSession()
  return useQuery({
    queryKey: ['atomic-location-dataset', cacheScope, ticket.id, catalogRevision],
    queryFn: () => request<Dataset>('/planning/location-dataset'),
    enabled, retry: false, refetchOnWindowFocus: false,
  })
}

export function currentAssignmentCandidates(
  ticket: Ticket, result: CandidateResult | undefined, catalogRevision: number | null | undefined,
) {
  return result && result.catalog_revision === catalogRevision && result.location_version === ticket.location?.version
    && Array.isArray(result.candidates) ? result : undefined
}

export function suggestedAssignmentPlace(result: CandidateResult | undefined): string {
  return result?.status === 'one' && result.candidates.length === 1 && !result.qr_text_conflict
    ? result.candidates[0].code : ''
}
