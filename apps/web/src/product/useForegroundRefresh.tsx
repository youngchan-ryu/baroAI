import { useCallback, useEffect, useState } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

type RefreshQuery = Pick<UseQueryResult<unknown, Error>, 'dataUpdatedAt' | 'isFetching' | 'isError' | 'refetch'>

/** One TanStack query owns every read. Mutations should use refetch() with its
 * default cancellation so an older in-flight snapshot cannot replace the result. */
export function useForegroundRefresh(query: RefreshQuery, options: { enabled?: boolean } = {}) {
  const enabled = options.enabled ?? true
  const { refetch, isFetching, isError, dataUpdatedAt } = query
  const [now, setNow] = useState(() => Date.now())
  const refresh = useCallback(() => {
    if (enabled && document.visibilityState === 'visible') void refetch({ cancelRefetch: false })
  }, [enabled, refetch])
  useEffect(() => {
    const tick = () => { setNow(Date.now()); refresh() }
    const onVisible = () => { if (document.visibilityState === 'visible') tick() }
    const timer = window.setInterval(tick, 5000)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); window.removeEventListener('focus', onVisible) }
  }, [refresh])
  return { refresh, enabled, isFetching, isError, dataUpdatedAt, stale: Boolean(dataUpdatedAt && (isError || now - dataUpdatedAt > 10000)) }
}

export function ForegroundRefreshStatus({ refresh }: { refresh: ReturnType<typeof useForegroundRefresh> }) {
  if (!refresh.stale && !refresh.isError) return null
  return <div className="queue-refresh-status" role="status"><span>정보가 오래됐을 수 있어요 · </span><button type="button" className="text-button" disabled={!refresh.enabled || refresh.isFetching} onClick={refresh.refresh}>다시 불러오기</button></div>
}
