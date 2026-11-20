import type { components } from './schema'

export type Health = components['schemas']['HealthResponse']
export type Readiness = components['schemas']['ReadyResponse'] | components['schemas']['NotReadyResponse']

async function request(path: string, signal?: AbortSignal) {
  const timeout = AbortSignal.timeout(5_000)
  return fetch(`/api${path}`, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout, headers: { Accept: 'application/json' } })
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  const response = await request('/health', signal)
  if (!response.ok) throw new Error('health_unavailable')
  const data: unknown = await response.json()
  if (!data || typeof data !== 'object' || !('status' in data) || data.status !== 'ok' ||
      !('service' in data) || data.service !== 'baroai-api' ||
      !('environment' in data) || !['dev', 'test', 'prod'].includes(String(data.environment)) ||
      !('llm_mode' in data) || !['replay', 'live', 'unavailable'].includes(String(data.llm_mode))) throw new Error('invalid_health')
  return data as Health
}

export async function getReadiness(signal?: AbortSignal): Promise<Readiness> {
  const response = await request('/ready', signal)
  if (!response.ok && response.status !== 503) throw new Error('readiness_unavailable')
  const data: unknown = await response.json()
  if (data && typeof data === 'object' && 'status' in data && 'database' in data) {
    if (response.ok && data.status === 'ready' && data.database === 'ok') return data as Readiness
    if (response.status === 503 && data.status === 'not_ready' && data.database === 'unavailable') return data as Readiness
  }
  throw new Error('invalid_readiness')
}
