import { MailDeliveryStatus } from './MailDeliveryStatus'
import { useOptionalRuntime } from './runtime'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { persistentKey, request } from './api'
import { useSession } from './session'
import { ErrorNotice, StatusBadge } from './shared'
import type { MergeTicket } from './duplicateTypes'
import type { Ticket } from './types'

export function MergedProgressPanel({ ticket, onUpdate }: { ticket: Ticket; onUpdate: () => void }) {
  const { t } = useTranslation('product')
  const { session } = useSession()
  const runtime = useOptionalRuntime()
  const progress = (ticket as MergeTicket).merged_progress
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const key = useRef<{ fingerprint: string; key: string } | null>(null)
  if (!progress || session?.user.role !== 'reporter') return null
  async function subscribe() {
    if (!session || !progress || progress.revision == null || busy) return
    const body = { expected_merge_revision: progress.revision, subscribed: !progress.subscribed }
    key.current = persistentKey(key.current, body); setBusy(true); setError(null)
    try { await request<Ticket>(`/tickets/${ticket.id}/merge-subscription`, { method: 'POST', body, key: key.current.key }, { csrf: session.csrf_token }); onUpdate() } catch (value) { setError(value) } finally { setBusy(false) }
  }
  return <section className="content-card"><h2>{t('mergedProgressTitle')}</h2><p>{t('mergedProgressHint')}</p>{progress.target_status && <StatusBadge status={progress.target_status} />}{progress.message && <p>{progress.message}</p>}<p>{t(runtime?.mode === 'service' ? progress.subscribed ? 'mergedServiceSubscribed' : 'mergedServiceNotSubscribed' : progress.subscribed ? 'mergedSubscribed' : 'mergedNotSubscribed')}</p><button type="button" className="button secondary" disabled={busy || progress.revision == null} onClick={() => void subscribe()}>{t(runtime?.mode === 'service' ? progress.subscribed ? 'mergedServiceUnsubscribe' : 'mergedServiceSubscribe' : progress.subscribed ? 'mergedUnsubscribe' : 'mergedSubscribe')}</button><p className="field-hint">{t(runtime?.mode === 'service' ? 'mergedServiceDeliveryHint' : 'mergedScreenOnly')}</p><MailDeliveryStatus status={progress.delivery_status} />{Boolean(error) && <ErrorNotice error={error} />}<button type="button" className="text-button" disabled={busy} onClick={onUpdate}>{t('refresh')}</button></section>
}
