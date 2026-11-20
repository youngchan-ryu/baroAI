import { useTranslation } from 'react-i18next'
import { liveAnalysisFailed } from './aiDraft'
import { useOptionalRuntime } from './runtime'
import { safetyUrgencies, urgencyLabel } from './safetyDisplay'
import type { Catalog, Ticket } from './types'
import './aiDraft.css'
import { Photos } from './shared'

type Props = {
  ticket: Ticket
  catalog?: Catalog
  /** Element id of the human review section the button scrolls to. */
  reviewTargetId?: string
}

const pendingStatuses = ['queued', 'running', 'retry_wait']
const imageSkipReasons = {
  not_available: '분석할 수 있는 승인된 사진이 없었어요.',
  vision_unconfigured: '사진 분석을 사용할 수 없었어요.',
  preparation_failed: '사진 사본을 준비하지 못했어요.',
  followup_text_only: '추가 답변 분석에는 사진을 사용하지 않아요.',
}

export function AiDraftCard({ ticket, catalog, reviewTargetId = 'category-review' }: Props) {
  const { t } = useTranslation('product')
  const runtime = useOptionalRuntime()
  const draft = ticket.ai_draft

  const evidence = <div className="ai-evidence"><details><summary>신고 원문 전체 보기</summary><p>{ticket.text}</p></details><p className="ai-original">{ticket.text}</p>{ticket.photos?.length > 0 && <Photos photos={ticket.photos} />}</div>
  if (!draft) {
    if (ticket.ai_status === 'replayed' && ticket.timeline?.some(event => event.type === 'analysis_matched_replay')) return <section className="content-card ai-draft-card"><p className="eyebrow-small">AI 초안 · 재생</p><p>{t('analysisHint.replayed')}</p>{evidence}</section>
    if (liveAnalysisFailed(ticket)) {
      return (
        <section className="content-card"><p className="ai-draft-line ai-draft-line-failed" role="status">{t('aiDraftLiveFailed')}</p>{evidence}</section>
      )
    }
    const preparing = runtime?.llm_mode === 'live' && pendingStatuses.includes(ticket.ai_status)
    return <section className="content-card"><p className="ai-draft-line">{t(preparing ? 'aiDraftPreparing' : 'aiDraftNone')}</p>{evidence}</section>
  }

  const categoryLabel =
    draft.category_name ??
    catalog?.categories.find(item => item.code === draft.category_code)?.label ??
    t(`categoryAuditLabels.${draft.category_code}`, { defaultValue: `분류 코드 ${draft.category_code}` })
  const automatic = ticket.safety ? safetyUrgencies(ticket.safety).ownAutomatic : null
  const raisedByRules = !draft.stale && automatic != null && automatic < draft.ai_urgency
  const source = draft.reused ? '재사용' : ticket.ai_mode === 'live' ? '실호출' : ticket.ai_mode === 'replay' ? '재생' : '출처 확인 필요'
  const imageCount = draft.image_count
  const imageReason = draft.image_skip_reason && imageSkipReasons[draft.image_skip_reason]

  function scrollToReview() {
    const target = document.getElementById(reviewTargetId)
    if (!target) return
    target.scrollIntoView({ behavior: 'smooth', block: 'start' })
    const focusable = target.querySelector<HTMLElement>('select, input, textarea')
    focusable?.focus({ preventScroll: true })
  }

  return (
    <section className="content-card ai-draft-card" aria-labelledby="ai-draft-title">
      <p className="eyebrow-small ai-draft-eyebrow" id="ai-draft-title">
        {t('aiDraftTitle')} · {source}
      </p>
      {evidence}<p className="ai-draft-summary">{draft.summary}</p>
      <dl className="ai-draft-facts">
        <div>
          <dt>{t('aiDraftCategory')}</dt>
          <dd>{categoryLabel}</dd>
        </div>
        <div>
          <dt>{t('aiDraftUrgency')}</dt>
          <dd>
            {urgencyLabel(draft.ai_urgency)}
            {draft.urgency_reason && <span className="ai-draft-reason">{draft.urgency_reason}</span>}
          </dd>
        </div>
      </dl>
      {draft.reused && (
        <p className="ai-draft-raised">같은 내용의 이전 AI 초안을 다시 썼어요.</p>
      )}
      {ticket.ai_mode === 'live' && (
        <p className="ai-draft-line">
          {imageCount == null ? '초안의 사진 분석 여부를 확인할 수 없어요.' : <>
            {draft.reused && '이전 초안에서 '}
            {imageCount > 0 ? `사진 ${imageCount}장을 함께 분석했어요` : '글만 분석했어요'}
            {imageCount === 0 && imageReason && `. ${imageReason}`}
          </>}
        </p>
      )}
      {draft.stale && (
        <p className="ai-draft-raised" role="status">
          신고 내용이나 승인된 사진이 바뀌어 이전 AI 초안이에요. 현재 내용을 직접 확인해 주세요.
        </p>
      )}
      {!draft.stale && draft.more_urgent && (
        <p className="ai-draft-raised" role="status">
          AI 제안이 현재 적용 긴급도보다 긴급해요. 확인해 주세요.
        </p>
      )}
      {raisedByRules && (
        <p className="ai-draft-raised">
          {t('aiDraftRaisedByRules', { level: urgencyLabel(automatic) })}
        </p>
      )}
      {!ticket.assignment_id && <button type="button" className="text-button ai-draft-action" onClick={scrollToReview}>
        {t('aiDraftReview')}
      </button>}
      <p className="ai-draft-footnote">
        {t('aiDraftFootnote')}
        {draft.model && <span className="ai-draft-model"> · {draft.model}</span>}
      </p>
    </section>
  )
}
