import { OperationsPage } from './product/OperationsPage'
import { SchoolSettingsPage } from './product/SchoolSettingsPage'
import { InsertionPage } from './product/InsertionPage'
import { PlanningPage } from './product/PlanningPage'
import { RuntimeProvider, useRuntime } from './product/runtime'
import { LoginPage, ServiceHomePage } from './product/ServicePages'
import { StaffLoginPage, StaffRegisterPage, StaffApplicationsPage } from './product/StaffPages'
import { PublicInvitePage } from './product/PublicInvitePage'
import { PublicHomePage } from './product/PublicHomePage'
import { Link, Navigate, Route, Routes } from 'react-router'
import { useTranslation } from 'react-i18next'
import { ConnectionPage } from './ConnectionPage'
import { SessionProvider } from './product/session'
import { Shell } from './product/Shell'
import { Protected } from './product/shared'
import { HomePage } from './product/HomePage'
import { ReportPage, TicketListPage, TicketPage } from './product/ReporterPages'
import { AdminPage } from './product/AdminPage'
import { AdminQueue } from './product/AdminQueue'
import { WorkerPage, WorkListPage, WorkDetailPage } from './product/WorkerPage'
import './product/product.css'

export function App() { return <RuntimeProvider><SessionProvider><AppRoutes /></SessionProvider></RuntimeProvider> }
function AppRoutes() {
  const runtime = useRuntime()
  const { t } = useTranslation('product')
  return <Routes>{runtime.mode === 'demo' && <Route path="/connection" element={<ConnectionPage />} />}<Route element={<Shell />}><Route path="/" element={runtime.mode === 'demo' ? <HomePage /> : runtime.mode === 'prototype' ? <LoginPage /> : runtime.mode === 'public_demo' ? <PublicHomePage /> : <ServiceHomePage />} />{runtime.mode === 'demo' ? <Route path="/demo" element={<HomePage />} /> : <Route path="/login" element={runtime.mode === 'public_demo' ? <PublicHomePage /> : <LoginPage />} />}{runtime.mode === 'service' && runtime.staff_password_auth && <><Route path="/staff/login" element={<StaffLoginPage />} /><Route path="/staff/register" element={<StaffRegisterPage />} /><Route path="/admin/staff-applications" element={<Protected role="dispatcher"><StaffApplicationsPage /></Protected>} /></>}{runtime.mode === 'public_demo' && <Route path="/staff-invite" element={<PublicInvitePage />} />}<Route path="/report" element={<Protected role="reporter"><ReportPage /></Protected>} /><Route path="/me" element={<Protected role="reporter"><TicketListPage /></Protected>} /><Route path="/t/:id" element={<Protected role="reporter"><TicketPage /></Protected>} /><Route path="/admin/planning/:planId/insertions" element={<Protected role="dispatcher"><InsertionPage /></Protected>} /><Route path="/admin/planning/:planId?" element={<Protected role="dispatcher"><PlanningPage /></Protected>} /><Route path="/admin/export" element={<Protected role="dispatcher"><Navigate to="/admin/queue" replace /></Protected>} /><Route path="/admin/settings" element={<Protected role="dispatcher"><SchoolSettingsPage /></Protected>} /><Route path="/admin/operations" element={<Protected role="dispatcher"><OperationsPage /></Protected>} /><Route path="/admin/queue" element={<Protected role="dispatcher"><AdminQueue /></Protected>} /><Route path="/admin/t/:id" element={<Protected role="dispatcher"><AdminPage /></Protected>} />{<><Route path="/work" element={<Protected role="worker"><WorkListPage /></Protected>} /><Route path="/work/:assignmentId" element={<Protected role="worker"><WorkDetailPage /></Protected>} /></>}<Route path="/w" element={<WorkerPage />} /><Route path="*" element={<section className="empty-state"><h1>{t('forbidden')}</h1><Link to="/">{t('home')}</Link></section>} /></Route></Routes>
}
