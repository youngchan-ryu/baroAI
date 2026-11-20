import type { components } from '../api/schema'
import type { Ticket } from './types'
export type ReportStructure = components['schemas']['ReportStructure']
export type Report = components['schemas']['ReportView']
export function approvedReports(ticket: Ticket): Report[] { return ticket.approved_reports ?? [] }
export function requiresReportReconfirmation(report: Report): boolean { return report.legacy_migrated && !report.reviewed && report.confirmed_result !== 'needs_review' && (report.warnings ?? []).includes('completion_conflict') }
export function workerConfirmed(report: Report): boolean { return !requiresReportReconfirmation(report) && (Boolean(report.worker_confirmed_revision) || report.analysis_status === 'worker_confirmed') }
