import { useSyncExternalStore } from 'react'
import type { Photo } from './types'
import type { GpsInput, LocationView } from './locationTypes'
import type { PhotoProtectionDraft } from './PhotoProtection'

export type DraftToken = { owner: string; generation: number }
export type Submission = { body: Readonly<{ text: string; location_text: string; upload_ids: readonly string[]; loc_hint?: string; gps?: GpsInput; information_mail_allowed?: boolean }>; key: string }
export type DraftData = {
  photoCoordinatesAllowed: boolean; informationMailAllowed: boolean; text: string; location: string; hint: string | null; gps?: GpsInput; gpsState: string
  selectedBuilding: {code:string; label:string} | null; buildingDetail: string | null; locationTouched: boolean; appliedRooms: string[]
  preview?: LocationView; previewFingerprint?: string
  files: File[]; photos: Map<File, Photo>; uploadKeys: Map<File, string>; abandonedUploads: Set<File>; protection: Map<File, PhotoProtectionDraft>
  uploadCount: number; protectionCount: number; sending: boolean; sealed: boolean; photosValidated: boolean
  pending: Submission | null; completedId: string | null; completedPublicNo: string | null
}
function empty(owner: string | null = null): DraftData {
  return { photoCoordinatesAllowed: owner?.startsWith('service:') ?? false, informationMailAllowed: owner?.startsWith('service:') ?? false, text: '', location: '', hint: null, gpsState: '', selectedBuilding: null, buildingDetail: null, locationTouched: false, appliedRooms: [], files: [], photos: new Map(), uploadKeys: new Map(), abandonedUploads: new Set(), protection: new Map(), uploadCount: 0, protectionCount: 0, sending: false, sealed: false, photosValidated: true, pending: null, completedId: null, completedPublicNo: null }
}
export class MemoryDraft {
  private owner: string | null = null
  private unlocked = false
  private generation = 0
  private value = empty()
  private listeners = new Set<() => void>()
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  snapshot = () => this.value
  private emit() { for (const listener of this.listeners) listener() }
  activate(owner: string) {
    if (owner !== this.owner) { this.value = empty(owner); this.generation++; this.owner = owner }
    if (!this.unlocked) this.generation++
    this.unlocked = true; this.emit()
  }
  lock() {
    this.unlocked = false; this.generation++
    this.value = { ...this.value, uploadCount: 0, protectionCount: 0, sending: false, gpsState: this.value.gpsState === 'gpsPending' ? '' : this.value.gpsState, photosValidated: this.value.photos.size === 0 }
    this.emit()
  }
  discard() { this.lock(); this.owner = null; this.value = empty(); this.emit() }
  token(): DraftToken { return { owner: this.owner ?? '', generation: this.generation } }
  current(token: DraftToken) { return this.unlocked && token.owner === this.owner && token.generation === this.generation }
  update(token: DraftToken, change: Partial<DraftData> | ((value: DraftData) => Partial<DraftData>)) {
    if (!this.current(token)) return false
    this.value = { ...this.value, ...(typeof change === 'function' ? change(this.value) : change) }; this.emit(); return true
  }
  edit(token: DraftToken, change: Partial<Pick<DraftData, 'text' | 'location' | 'selectedBuilding' | 'buildingDetail' | 'informationMailAllowed' | 'hint' | 'gps' | 'gpsState'>>) {
    if (this.value.pending || this.value.sending || this.value.sealed) return
    const next = { ...this.value, ...change }
    const locationChanged = locationFingerprint(next) !== locationFingerprint(this.value)
    this.update(token, { ...change, ...('location' in change ? { locationTouched: true } : {}), ...(locationChanged ? { preview: undefined, previewFingerprint: undefined } : {}) })
  }
  reset(token: DraftToken) {
    if (!this.current(token)) return
    this.generation++; this.value = empty(this.owner); this.emit()
  }
}
export function useMemoryDraft(store: MemoryDraft) { return useSyncExternalStore(store.subscribe, store.snapshot, store.snapshot) }
export function locationFingerprint(value: Pick<DraftData, 'text' | 'location' | 'hint' | 'gps'>) {
  return JSON.stringify([value.text, value.location, value.hint, value.gps ?? null])
}
