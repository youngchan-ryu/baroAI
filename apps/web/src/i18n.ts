import i18n from 'i18next'
import { productCopy, protectionCopy } from './product/copy'
import { initReactI18next } from 'react-i18next'

void i18n.use(initReactI18next).init({
  lng: 'ko', fallbackLng: 'ko', interpolation: { escapeValue: false },
  resources: { ko: { product: { ...productCopy, ...protectionCopy }, translation: {
    brand: '바로AI', eyebrow: '시설 업무 지원 서비스', stage: 'M1 · 개발 기반',
    title: '연결부터, 차근차근.', intro: '접수부터 회신까지 이어질 바로AI의 개발 연결 상태를 확인해요.',
    api: 'API 서버', apiDescription: '웹과 서버의 실제 연결을 확인해요.',
    database: '데이터베이스', databaseDescription: '서버와 저장소의 준비 상태를 별도로 확인해요.',
    checking: '확인 중', connected: '연결됨', ready: '준비됨', failed: '연결 확인 필요', unavailable: '저장소 확인 필요',
    retry: '다시 확인', refreshing: '다시 확인 중…', retryHint: '로컬 서버 실행 상태를 확인한 뒤 다시 시도해 주세요.',
    dbHint: 'API 연결과 별개로 데이터베이스가 준비되지 않았어요. 데이터베이스 실행 상태를 확인해 주세요.',
    replay: 'AI 재생 모드', live: 'AI 실호출 설정', aiUnavailable: 'AI 분석 이용 불가', aiUnavailableHint: 'AI 설정이 준비되지 않아 접수를 일시 중단했어요.', unknown: 'AI 모드 확인 전',
    replayHint: '미리 준비한 응답을 사용하는 설정이에요. 실제 AI 호출 결과가 아니에요.',
    liveHint: '서버의 실호출 설정을 표시해요. 이 연결 검사는 AI를 호출하지 않아요.',
    unknownHint: '서버 연결 후 AI 설정을 표시해요.',
    principle: 'AI 초안 + 사람 확인', scope: '개발 연결 상태를 진단하는 화면이에요. 시연 기능은 처음 화면에서 시작해 주세요.',
    environment: '환경', dev: '개발', test: '테스트', prod: '운영', footer: '팀 BRIDGE · 바로AI 개발 환경', notFound: '페이지를 찾을 수 없어요.', home: '연결 상태로 돌아가기',
  } } },
})
export default i18n
