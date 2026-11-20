import { needsSafetyWarning, safetyUrgencies, urgencyLabel } from './safetyDisplay'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice } from './shared'
import type { MergeTicket } from './duplicateTypes'
import type { Ticket } from './types'

type Safety = NonNullable<Ticket['safety']>
export function LinkedSafetySummary({ ticket }: { ticket: Ticket }) {
  const { t } = useTranslation('product')
  const safety = ticket.safety as Safety | undefined
  if (!safety || ((safety.linked_rule_floor == null || safety.linked_rule_floor === 3) && !safety.linked_requires_review)) return null
  const values = safetyUrgencies(safety)
  return <section className="notice linked-safety"><h3>{t('linkedSafetyTitle')}</h3><p>{t('linkedSafetyHint')}</p><dl className="safety-values"><div><dt>{t('linkedOwnAutomatic')}</dt><dd>{urgencyLabel(values.ownAutomatic)}</dd></div><div><dt>{t('linkedOwnEffective')}</dt><dd>{urgencyLabel(values.ownCurrent)}</dd></div><div><dt>{t('linkedFloor')}</dt><dd>{values.linked == null ? t('linkedNoFloor') : urgencyLabel(values.linked)}</dd></div><div><dt>{t('linkedGroupEffective')}</dt><dd>{urgencyLabel(values.current)}</dd></div></dl>{safety.linked_requires_review && <p>{t('linkedReviewNeeded')}</p>}{needsSafetyWarning(ticket) && <p>{t('emergency')}</p>}</section>
}
export function LinkedSafetyReview({ ticket, onUpdate }: { ticket: Ticket; onUpdate: () => void }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const [snapshot, setSnapshot] = useState(ticket as MergeTicket)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  const safety = ticket.safety as Safety | undefined
  if (session?.user.role !== 'dispatcher' || !safety?.linked_requires_review) return null
  const stale = snapshot.merge_version !== (ticket as MergeTicket).merge_version
  async function reload() { setBusy(true); setError(null); try { setSnapshot(await api.ticket(ticket.id) as MergeTicket); key.current = null; onUpdate() } catch (value) { setError(value) } finally { setBusy(false) } }
  async function review() { if (!session || busy || stale || !reason.trim()) return; const body = { expected_merge_version: snapshot.merge_version, reason: reason.trim() }; key.current = persistentKey(key.current, body); setBusy(true); setError(null); try { await request<Ticket>(`/tickets/${ticket.id}/linked-safety-review`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); onUpdate() } catch (value) { setError(value) } finally { setBusy(false) } }
  return <section className="content-card"><h2>{t('linkedReviewTitle')}</h2><p>{t('linkedReviewHint')}</p>{stale && <p className="notice">{t('duplicateStale')}</p>}<button type="button" className="text-button" disabled={busy} onClick={() => void reload()}>{t('linkedReload')}</button><label>{t('linkedReviewReason')}<textarea value={reason} onChange={event => setReason(event.target.value)} maxLength={1000} rows={3} disabled={busy} /></label><button type="button" className="button secondary" disabled={busy || stale || !reason.trim()} onClick={() => void review()}>{t('linkedReviewSubmit')}</button>{Boolean(error) && <ErrorNotice error={error} />}</section>
}
