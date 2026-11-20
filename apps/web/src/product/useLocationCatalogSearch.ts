import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { request } from './api'
import type { SearchLocation } from './locationSearchUtils'

export function useLocationCatalogSearch(items: SearchLocation[], csrf: string | undefined, cacheScope: string, catalogRevision: number | null | undefined) {
  const [query, setQuery] = useState('')
  const result = useQuery({
    queryKey: ['location-catalog-search', cacheScope, query],
    queryFn: () => request<{ revision: number | null; locations: SearchLocation[] }>('/locations/catalog', { method: 'POST', body: { query: query.trim() } }, { csrf }),
    enabled: Boolean(csrf && query.trim() && query.trim().length <= 100),
    retry: false,
  })
  const current = catalogRevision != null && result.data?.revision === catalogRevision
  const matches = new Map((current ? result.data?.locations ?? [] : []).map(item => [item.code, item]))
  return { items: items.map(item => {
    const match = matches.get(item.code)
    return match ? { ...item, region: match.region, aliases: [...(item.aliases ?? []), query] } : item
  }), setQuery, status: !query.trim() ? 'idle' as const : result.isFetching ? 'loading' as const : result.isError ? 'error' as const : !current ? 'unavailable' as const : 'ready' as const }
}
