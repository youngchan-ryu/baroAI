"""Report semantics and public templates. Free narrative never becomes public copy."""

import re

from fastapi import HTTPException

from app.schemas import ReportStructure, ReportView

REPLIES = {
    "completed": "담당자가 작업 완료 보고를 확인했어요.",
    "temporary": "임시 조치 보고를 확인했어요. 추가 확인이 필요해요.",
    "revisit": "재방문 필요 보고를 확인했어요. 방문 일정은 정해지지 않았어요.",
    "needs_review": "작업 보고를 확인했어요. 해결 여부는 추가 확인이 필요해요.",
}


def conflicts(result, raw, structure=None):
    if result != "completed":
        return False
    pieces = [raw]
    if structure:
        if structure.revisit_needed or structure.residual_issue:
            return True
        pieces += structure.actions + structure.materials_used
    return bool(
        re.search(
            r"미해결|해결.{0,4}(?:못|안)|안\s*됐|임시|재방문|추가\s*(?:작업|확인)|남아|남았|미완료",
            "\n".join(pieces),
        )
    )


def confirm_structure(report, payload):
    if payload.confirmed_result not in {report.result, "needs_review"}:
        raise HTTPException(422, "report_result_escalation_forbidden")
    structure = payload.structure.model_copy()
    if (
        conflicts(report.result, report.text, structure)
        and payload.confirmed_result != "needs_review"
    ):
        raise HTTPException(422, "report_completion_conflict")
    # Only the current server-stored draft can establish AI provenance.
    incoming = structure.model_dump(exclude={"source"})
    draft = dict(report.draft_data)
    draft_source = draft.pop("source", None)
    matching_sources = {"drafted": "live", "replayed": "replay"}
    expected_source = matching_sources.get(report.analysis_status)
    structure.source = (
        expected_source
        if expected_source and draft_source == expected_source and incoming == draft
        else "manual"
    )
    return structure


def report_view(report, photos, *, internal):
    approved = report.approved_snapshot
    result = approved.get("confirmed_result", report.confirmed_result or report.result)
    return ReportView(
        id=report.id,
        created_at=report.created_at if internal else None,
        supersedes_report_id=report.supersedes_report_id if internal else None,
        correction_requested_at=report.correction_requested_at if internal else None,
        correction_reason=report.correction_reason if internal else None,
        result=result if not internal else report.result,
        text=report.text if internal else report.reply_text or REPLIES[result],
        reviewed=report.reviewed,
        photos=photos,
        revision=report.revision,
        analysis_status=report.analysis_status if internal else "approved",
        draft=ReportStructure.model_validate(report.draft_data)
        if internal and report.draft_data
        else None,
        confirmed=ReportStructure.model_validate(report.confirmed_data)
        if internal and report.confirmed_data
        else None,
        confirmed_result=report.confirmed_result,
        worker_confirmed_revision=report.worker_confirmed_revision,
        warnings=["completion_conflict"]
        if internal and conflicts(report.result, report.text)
        else [],
        reply_text=report.reply_text,
        reply_preview=REPLIES[report.confirmed_result]
        if internal and report.confirmed_result
        else None,
        publication_status="screen_published" if report.reviewed and approved else "not_published",
        legacy_migrated=report.legacy_migrated,
    )
