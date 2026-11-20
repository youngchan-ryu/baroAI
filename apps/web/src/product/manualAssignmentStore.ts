export type ManualBody = { worker_id: string; reason: string; expected_assignment_id: string | null; expected_execution_revision: number | null; stop_current_visit?: boolean; override_reason?: string }
export type ManualPending = { ticketId: string; body: ManualBody; key: string }
type Entry = { command: ManualPending; busy: boolean }

/** Memory belongs to one validated session, never browser storage. */
export class ManualAssignmentStore {
  private owner: string | null = null
  private active = false
  private entries = new Map<string, Entry>()
  private listeners = new Set<() => void>()
  private revision = 0
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  snapshot = () => this.revision
  private notify() { this.revision++; this.listeners.forEach(listener => listener()) }
  activate(owner: string) { if (this.owner !== owner) this.discard(); this.owner = owner; this.active = true; this.notify() }
  lock() { this.active = false; this.notify() }
  discard() { this.entries.clear(); this.owner = null; this.active = false; this.notify() }
  get(ticketId: string) { return this.active ? this.entries.get(ticketId) : undefined }
  start(command: ManualPending) { const existing = this.entries.get(command.ticketId); if (!this.active || (existing && (existing.busy || existing.command !== command))) return false; this.entries.set(command.ticketId, { command, busy: true }); this.notify(); return true }
  finish(command: ManualPending, clear: boolean) {
    const existing = this.entries.get(command.ticketId)
    if (!existing || existing.command !== command) return false
    if (clear) this.entries.delete(command.ticketId)
    else this.entries.set(command.ticketId, { command, busy: false })
    this.notify(); return true
  }
}
