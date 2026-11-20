import { useTranslation } from 'react-i18next'
const states = ['not_sent', 'local_saved', 'queued', 'claimed', 'sending', 'retry_wait', 'accepted', 'unknown', 'failed', 'expired', 'superseded'] as const
export function MailDeliveryStatus({ status }: { status?: string | null }) {
  const { t } = useTranslation('product')
  const state = states.find(value => value === status)
  return <p className="field-hint mail-delivery-status">{t(`mailDelivery.${state ?? 'unavailable'}`)}</p>
}
