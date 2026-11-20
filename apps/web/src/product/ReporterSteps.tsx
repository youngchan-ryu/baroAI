import { useTranslation } from 'react-i18next'
import { MailDeliveryStatus } from './MailDeliveryStatus'
import { approvedReports } from './reportTypes'
import type { Ticket } from './types'

const labels = ['접수됨', '내용 정리', '담당자 확인', '작업 배정', '결과 회신']

export function ReporterSteps({ ticket }: { ticket: Ticket }) {
  const { t } = useTranslation('product')
  const { status, ai_status: ai, reply_text: reply } = ticket
  const rejected = status === 'rejected'
  const activeAnalysis = ['queued', 'running', 'retry_wait'].includes(ai)
  const step = status === 'completed' ? (reply ? 4 : 3) : ['assigned', 'in_progress'].includes(status) ? 3 : status === 'confirmed' ? 2 : activeAnalysis ? 1 : 2
  const message = rejected ? '신고가 반려됐어요. 상세 내용을 확인해 주세요.'
    : status === 'completed' ? (reply ? '처리 결과가 도착했어요.' : '처리가 종료됐어요. 회신은 아직 도착하지 않았어요.')
    : ['assigned', 'in_progress'].includes(status) ? '작업자가 배정됐어요.'
    : status === 'confirmed' ? '담당자가 확인했어요.'
    : ai === 'retry_wait' ? '정리를 다시 시도하고 있어요. 접수는 보존됐어요.'
    : ai === 'replayed' ? '미리 준비한 예시로 내용 정리를 마쳤어요(재생). 담당자 확인을 기다려요.'
    : ai === 'drafted' ? 'AI가 내용 정리를 마쳤어요. 담당자 확인을 기다려요.'
    : activeAnalysis ? '내용을 정리하고 있어요.' : '담당자가 원문을 직접 확인해요.'
  const latestApproved = approvedReports(ticket).filter(report => report.reviewed).at(-1) ?? (ticket.latest_report?.reviewed ? ticket.latest_report : null)
  return <section className="content-card reporter-steps" aria-label="신고 진행 단계"><h2>신고 진행 단계</h2><ol>{labels.map((label, index) => <li key={label} className={!rejected && index === step ? 'current' : !rejected && index < step ? 'done' : ''} aria-current={!rejected && index === step ? 'step' : undefined}>{label}</li>)}</ol><p role="status">{message}</p>{ai === 'replayed' && status !== 'received' && <p>내용 정리는 미리 준비한 예시를 사용했어요(재생).</p>}{reply && <><h3>처리 결과</h3><p className="ticket-body">{reply}</p><p className="field-hint">{t('reportScreenPublished')}</p><MailDeliveryStatus status={latestApproved?.delivery_status} /></>}</section>
}
