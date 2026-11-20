import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useRuntime } from './runtime'
import { api, ApiError, advanceAuthenticationEpoch } from './api'
import { WorkerDraftRegistry } from './workerDraft'
import { MemoryDraft } from './memoryDraft'
import { ManualAssignmentStore } from './manualAssignmentStore'
import type { Persona, Session } from './types'

type SessionContextType = { manualAssignments: ManualAssignmentStore; draftStore: MemoryDraft; workerDrafts: WorkerDraftRegistry; cacheScope: string; acceptSession: (value: Session) => Promise<void>; session: Session | null; explicitLogout: boolean; clearExplicitLogout: () => void; loading: boolean; switching: boolean; error: unknown; login: (persona: Persona) => Promise<void>; logout: (onSuccess?: () => void) => Promise<void>; reload: () => void }
const Context = createContext<SessionContextType | null>(null)
export function SessionProvider({ children }: { children: ReactNode }) {
  const runtime = useRuntime()
  const [workerDrafts] = useState(() => new WorkerDraftRegistry())
  const [draftStore] = useState(() => new MemoryDraft())
  const [manualAssignments] = useState(() => new ManualAssignmentStore())
  const [session, setSession] = useState<Session | null>(null)
  const [explicitLogout, setExplicitLogout] = useState(false)
  const [loading, setLoading] = useState(true)
  const [switching, setSwitching] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const client = useQueryClient()
  const authGeneration = useRef(0)
  const channel = useRef<BroadcastChannel | null>(null)
  function nextAuthGeneration() { advanceAuthenticationEpoch(); return ++authGeneration.current }
  function validate(value: Session) {
    if (value.auth_mode !== runtime.auth_method || value.data_scope !== runtime.mode) { draftStore.discard(); workerDrafts.discard(); manualAssignments.discard(); throw new Error('session_scope_mismatch') }
    const owner = `${runtime.mode}:${runtime.auth_method}:${value.user.id}:${value.user.role}`
    draftStore.activate(owner); workerDrafts.activate(owner); manualAssignments.activate(owner)
    return value
  }
  function begin(discard = false) {
    const generation = nextAuthGeneration()
    if (discard) { draftStore.discard(); workerDrafts.discard(); manualAssignments.discard() } else { draftStore.lock(); workerDrafts.lock(); manualAssignments.lock() }
    setExplicitLogout(false); setLoading(true); setSwitching(true); setSession(null); setError(null)
    const cancelled = client.cancelQueries()
    client.clear()
    return { generation, cancelled }
  }
  async function recheck(discard = false) {
    const { generation, cancelled } = begin(discard)
    try {
      await cancelled
      if (generation !== authGeneration.current) return
      const value = await api.me()
      if (generation === authGeneration.current) { setSession(validate(value)); setError(null) }
    } catch (value) {
      if (generation === authGeneration.current) { if (value instanceof ApiError && value.status === 401) manualAssignments.discard(); setSession(null); setError(value instanceof ApiError && value.status === 401 ? null : value) }
    } finally { if (generation === authGeneration.current) { setLoading(false); setSwitching(false) } }
  }
  useEffect(() => {
    const expired = () => {
      nextAuthGeneration(); draftStore.lock(); workerDrafts.lock(); manualAssignments.discard(); setLoading(false); setSwitching(false); setSession(null); setError(null)
      void client.cancelQueries(); client.clear()
    }
    const assignmentEnded = (event: Event) => { if (event instanceof CustomEvent && typeof event.detail === 'string') workerDrafts.deny(event.detail) }
    const visibility = () => { if (document.visibilityState === 'visible') void recheck() }
    const currentChannel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('baro-auth-change')
    channel.current = currentChannel
    if (currentChannel) currentChannel.onmessage = event => { if (event.data === 'changed' || event.data === 'logout') void recheck(event.data === 'logout') }
    window.addEventListener('baro:assignment-ended', assignmentEnded)
    window.addEventListener('baro:unauthorized', expired)
    document.addEventListener('visibilitychange', visibility)
    // Initial and later rechecks must use the same synchronous privacy boundary.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void recheck()
    return () => { nextAuthGeneration(); currentChannel?.close(); channel.current = null; window.removeEventListener('baro:unauthorized', expired); window.removeEventListener('baro:assignment-ended', assignmentEnded); document.removeEventListener('visibilitychange', visibility) }
  // Runtime and stores are immutable for this provider mount.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, draftStore, workerDrafts, manualAssignments])
  async function acceptSession(value: Session) {
    manualAssignments.discard()
    const { generation, cancelled } = begin()
    try {
      await cancelled
      if (generation !== authGeneration.current) throw new Error('authentication_changed')
      setSession(validate(value)); channel.current?.postMessage('changed')
    } finally { if (generation === authGeneration.current) { setLoading(false); setSwitching(false) } }
  }
  async function login(persona: Persona) {
    if (runtime.mode !== 'demo') throw new Error('demo_unavailable')
    manualAssignments.discard()
    const { generation, cancelled } = begin()
    try {
      await cancelled
      if (generation !== authGeneration.current) throw new Error('authentication_changed')
      const value = await api.login(persona)
      if (generation !== authGeneration.current) throw new Error('authentication_changed')
      setSession(validate(value)); channel.current?.postMessage('changed')
    } catch (value) { if (generation === authGeneration.current) setError(value); throw value }
    finally { if (generation === authGeneration.current) { setLoading(false); setSwitching(false) } }
  }
  async function logout(onSuccess?: () => void) {
    if (!session) return
    const previous = session
    const { generation, cancelled } = begin(true)
    try {
      await cancelled
      if (generation !== authGeneration.current) throw new Error('authentication_changed')
      await api.logout(previous.csrf_token)
      if (generation !== authGeneration.current) throw new Error('authentication_changed')
      setExplicitLogout(true)
      channel.current?.postMessage('logout')
      onSuccess?.()
    } catch (value) { if (generation === authGeneration.current) await recheck(); throw value }
    finally { if (generation === authGeneration.current) { setLoading(false); setSwitching(false) } }
  }
  return <Context.Provider value={{ draftStore, workerDrafts, manualAssignments, cacheScope: `${runtime.mode}:${runtime.auth_method}:${session?.user.id ?? 'anonymous'}:${session?.user.role ?? 'anonymous'}`, acceptSession, session, explicitLogout, clearExplicitLogout: () => setExplicitLogout(false), loading, switching, error, login, logout, reload: () => { void recheck() } }}>{children}</Context.Provider>
}
// This hook shares the provider with routes.
// eslint-disable-next-line react-refresh/only-export-components
export function useSession() { const value = useContext(Context); if (!value) throw new Error('SessionProvider required'); return value }
