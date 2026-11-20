import { useState, useSyncExternalStore, type SetStateAction } from 'react'
import { MemoryDraft, type DraftToken } from './memoryDraft'

class Atom<T> {
  private listeners = new Set<() => void>()
  constructor(private value: T) {}
  snapshot = () => this.value
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  set(value: SetStateAction<T>) { this.value = typeof value === 'function' ? (value as (previous: T) => T)(this.value) : value; this.listeners.forEach(listener => listener()) }
}
export class WorkerFormDraft {
  readonly uploads = new MemoryDraft()
  private atoms = new Map<string, Atom<unknown>>()
  constructor(readonly key: string, private changed: () => void = () => {}) {}
  atom<T>(key: string, initial: () => T): Atom<T> {
    if (!this.atoms.has(key)) this.atoms.set(key, new Atom<unknown>(initial()))
    return this.atoms.get(key) as Atom<T>
  }
  read<T>(key: string): T | undefined { return this.atoms.get(key)?.snapshot() as T | undefined }
  set<T>(token: DraftToken, key: string, value: SetStateAction<T>, initial: () => T) {
    if (!this.uploads.current(token)) return
    this.atom(key, initial).set(value); this.changed()
  }
  activate(owner: string) { this.uploads.activate(`${owner}:${this.key}`) }
  lock() { this.uploads.lock(); this.atoms.get('busy')?.set(false); this.changed() }
  discard() { this.uploads.discard(); this.atoms.clear(); this.changed() }
}
export class AssignmentDrafts {
  private forms = new Map<string, WorkerFormDraft>()
  accessEnded = false
  private owner: string | null = null
  private verified = false
  private revision = 0
  private listeners = new Set<() => void>()
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  snapshot = () => this.revision
  private changed = () => { this.revision++; this.listeners.forEach(listener => listener()) }
  get(key: string) {
    if (!this.forms.has(key)) {
      const form = new WorkerFormDraft(key, this.changed)
      if (this.verified && this.owner) form.activate(this.owner)
      this.forms.set(key, form)
    }
    return this.forms.get(key)!
  }
  pending(prefix: string) { return [...this.forms.values()].find(form => form.key.startsWith(prefix) && form.read('pending')) }
  report(cycle: string) {
    return this.pending('report:') ?? [...this.forms.values()].find(form => form.key.startsWith('report:') && !form.read('saved') && Boolean(form.uploads.snapshot().text)) ?? this.get(`report:${cycle}`)
  }
  verify(owner: string) { this.accessEnded = false; this.owner = owner; this.verified = true; this.forms.forEach(form => form.activate(owner)); this.changed() }
  deny() { this.discard(); this.accessEnded = true; this.changed() }
  lock() { this.verified = false; this.forms.forEach(form => form.lock()); this.changed() }
  discard() { this.verified = false; this.forms.forEach(form => form.discard()); this.forms.clear(); this.changed() }
}
export class WorkerDraftRegistry {
  private owner: string | null = null
  private active = false
  private assignments = new Map<string, AssignmentDrafts>()
  activate(owner: string) {
    if (owner !== this.owner) this.discard()
    this.owner = owner; this.active = true
  }
  lock() { this.active = false; this.assignments.forEach(value => value.lock()) }
  discard() { this.lock(); this.assignments.forEach(value => value.discard()); this.assignments.clear(); this.owner = null }
  get(id: string) { if (!this.assignments.has(id)) this.assignments.set(id, new AssignmentDrafts()); return this.assignments.get(id)! }
  verify(id: string) { if (this.active && this.owner) this.get(id).verify(`${this.owner}:assignment:${id}`) }
  deny(id: string) { this.assignments.get(id)?.deny() }
}
export function useWorkerField<T>(provided: WorkerFormDraft | undefined, key: string, initial: () => T): [T, (value: SetStateAction<T>) => void] {
  const [local] = useState(() => { const form = new WorkerFormDraft('local'); form.activate('local'); return form })
  const form = provided ?? local
  const atom = form.atom(key, initial)
  const value = useSyncExternalStore(atom.subscribe, atom.snapshot, atom.snapshot)
  const token = form.uploads.token()
  return [value, next => form.set(token, key, next, initial)]
}

export function immutableRequest<T>(value: T): T {
  const copy = JSON.parse(JSON.stringify(value)) as T
  const freeze = (item: unknown) => { if (item && typeof item === 'object') { Object.values(item).forEach(freeze); Object.freeze(item) } }
  freeze(copy)
  return copy
}
