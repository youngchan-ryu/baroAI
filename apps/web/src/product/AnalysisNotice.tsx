import { useOptionalRuntime } from './runtime'
import { useTranslation } from 'react-i18next'
import type { Ticket } from './types'
import { liveAnalysisFailed } from './aiDraft'

export function AnalysisNotice({ ticket, onRefresh, refreshing = false }: { ticket: Ticket; onRefresh?: () => void; refreshing?: boolean }) {
  const runtime = useOptionalRuntime()
  const { t } = useTranslation('product')
  const matched = ticket.ai_status === 'replayed' && ticket.timeline?.some(event => event.type === 'analysis_matched_replay')
  const liveFailed = liveAnalysisFailed(ticket)
  const status = matched ? 'replayed' : liveFailed ? 'live_failed' : ['queued', 'running', 'retry_wait', 'failed', 'superseded', 'drafted'].includes(ticket.ai_status)
    ? ticket.ai_status
    : 'unknown'
  const live = ticket.ai_mode === 'live' || status === 'drafted' || liveFailed || (runtime?.llm_mode === 'live' && status !== 'replayed')
  const reused = status === 'drafted' && (ticket.ai_reused || ticket.ai_draft?.reused)
  const modeLabel = reused ? 'AI 초안 재사용 · 담당자 확인 필요' : t(live ? 'analysisLiveMode' : 'analysisReplayMode')
  if (runtime?.mode === 'public_demo' && status === 'unknown') return <aside className="notice" aria-label={t('analysisTitle')}><h2>AI 정리 전</h2><p>신고 원문의 안전 규칙을 적용했으며 담당자가 내용을 확인해요.</p></aside>
  if (runtime?.mode === 'service') return <aside className="notice" aria-label={t('analysisTitle')}><h2>{t(`serviceAnalysisState.${status}`)}</h2><p>{t(`serviceAnalysisHint.${status}`)}</p>{onRefresh && <button type="button" className="text-button" disabled={refreshing} onClick={onRefresh}>{t('refresh')}</button>}<details><summary>{t('analysisDisclosure')}</summary><p>{modeLabel}</p><p>{t(`analysisHint.${status}`)}</p></details></aside>
  return <aside className="notice replay-notice" aria-label={t('analysisTitle')}><span className="eyebrow-small">{modeLabel} · {t('human')}</span><h2>{t(`analysisState.${status}`)}</h2><p>{t(`analysisHint.${status}`)}</p>{onRefresh && <button type="button" className="text-button" disabled={refreshing} onClick={onRefresh}>{t('refresh')}</button>}</aside>
}
