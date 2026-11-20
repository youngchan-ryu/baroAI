import hashlib
import hmac
import json
import math
import re
import uuid
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.catalog import CATEGORY_MAP, EVENT_LABELS
from app.models import (
    Assignment,
    CompletionReport,
    IdempotencyRecord,
    ImageVariant,
    ProcessingAttempt,
    ProcessingJob,
    SchoolCatalogActive,
    SchoolCatalogVersion,
    Ticket,
    TicketEvent,
    TicketMerge,
    Upload,
    User,
)
from app.protection import managed_db_present, photo_view
from app.reporting import report_view
from app.schemas import (
    AIDraftView,
    EventView,
    LocationCandidate,
    LocationView,
    RoutingView,
    SafetyView,
    TicketView,
)
from app.security import Principal, assignment_current, current_assignment_clause, now

# Live-path events; labels live here until the shared catalog adopts them.
LIVE_EVENT_LABELS = {
    "ai_followup_proposed_live": "AI 초안 · 실호출 추가 질문",
    "ai_followup_proposed_reused": "AI 초안 · 재사용 추가 질문",
    "ai_followup_proposed_replay": "AI 초안 · 재생 추가 질문",
    "ai_followup_answered": "신고자 추가 답변",
    "ai_followup_skipped": "신고자가 추가 질문 건너뜀",
    "information_mail_accepted": "추가 정보 안내 메일 제공자 접수",
    "information_mail_consent": "추가 정보 요청 메일 동의 변경",
    "information_requested": "추가 정보 요청",
    "information_answered": "신고자 추가 답변",
    "information_cancelled": "추가 정보 요청 취소",
    "analysis_drafted": "AI 초안 작성 · 사람 확인 필요",
    "analysis_drafted_late": "담당자 확인 후 AI 초안 도착 · 사람 확인 필요",
    "analysis_reused": "같은 내용의 이전 AI 초안 재사용 · 사람 확인 필요",
    "report_analysis_reused": "같은 내용의 이전 AI 보고 초안 재사용 · 작업자 확인 필요",
    "analysis_skipped_short": "짧은 신고 · 직접 확인 필요",
    "report_analysis_skipped_short": "짧은 작업 보고 · 직접 확인 필요",
    "analysis_live_failed": "AI 분석 실패 · 사람 확인 필요",
    "report_analysis_drafted": "AI 보고 초안 · 작업자 확인 필요",
    "report_analysis_live_failed": "AI 보고 분석 실패 · 수동 확인 필요",
    "urgency_override_released": "새 위험 신호로 긴급도 조정 해제",
    "photo_coordinates_viewed": "담당자 사진 위치 조회",
}


def event_label(event_type, default=None):
    return EVENT_LABELS.get(event_type) or LIVE_EVENT_LABELS.get(
        event_type, event_type if default is None else default
    )


# Event types are historical evidence; a user's current role is not.
_REPORTER_EVENTS = {
    "created",
    "received",
    "location_answered",
    "location_corrected",
    "information_answered",
    "ai_followup_answered",
    "ai_followup_skipped",
    "information_mail_consent",
}
_WORKER_EVENTS = {
    "started",
    "departed",
    "visit_ended",
    "reported",
    "report_confirmed",
}
_DISPATCHER_EVENTS = {
    "photo_coordinates_viewed",
    "worker_credential_issued",
    "service_insertion_approved",
    "mail_recovery_requested",
    "information_requested",
    "information_cancelled",
    "edited",
    "confirmed",
    "assigned",
    "reassigned",
    "report_reviewed",
    "report_correction_requested",
    "report_dismissed",
    "urgency_overridden",
    "assignment_field_override",
    "remaining_confirmed",
    "visit_stopped",
    "urgent_plan_assigned",
    "urgent_plan_unassigned",
    "plan_cancelled",
    "dispatch_plan_cancelled",
    "merged",
    "merge_received",
    "merge_cancelled",
    "merge_source_restored",
    "merge_safety_reviewed",
    "duplicate_dismissed",
}
_AI_LIVE_EVENTS = {
    "ai_followup_proposed_live",
    "analysis_drafted",
    "analysis_drafted_late",
    "analysis_live_failed",
    "report_analysis_drafted",
    "report_analysis_live_failed",
}
_AI_REPLAY_EVENTS = {
    "ai_followup_proposed_replay",
    "analysis_matched_replay",
    "analysis_unmatched",
    "analysis_failed",
    "report_analysis_succeeded",
    "report_analysis_failed",
}
_AI_UNKNOWN_EVENTS = {
    "analysis_succeeded", "analysis_reused", "report_analysis_reused",
    "ai_followup_proposed_reused",
}
_SYSTEM_EVENTS = {
    "analysis_skipped_short",
    "report_analysis_skipped_short",
    "information_mail_accepted",
}


def event_actor(event, names, dispatcher, reporter_id=None):
    kind = event.event_type
    if kind == "urgency_override_released":
        return "rule", "안전 규칙"
    if kind in {"analysis_live_failed", "report_analysis_live_failed"}:
        return "ai", "AI 처리 · 실호출 경로"
    if kind in _AI_LIVE_EVENTS:
        return "ai", "AI 초안 · 실호출"
    if kind in _AI_REPLAY_EVENTS:
        return "ai", "AI 초안 · 재생"
    if kind in _AI_UNKNOWN_EVENTS:
        return "ai", "AI 초안 · 방식 미확인"
    if kind in _SYSTEM_EVENTS:
        return "system", "시스템"
    if kind == "location_updated" and event.actor_id and reporter_id:
        if event.actor_id == reporter_id:
            return "reporter", "신고자"
        # Location updates are restricted to the owner or a dispatcher.
        name = names.get(event.actor_id) if dispatcher else None
        return "dispatcher", f"담당자 · {name}" if name else "담당자"
    if not event.actor_id:
        return None, "행위자 정보 없음"
    for events, role, label in (
        (_REPORTER_EVENTS, "reporter", "신고자"),
        (_WORKER_EVENTS, "worker", "작업자"),
        (_DISPATCHER_EVENTS, "dispatcher", "담당자"),
    ):
        if kind in events:
            name = (
                names.get(event.actor_id)
                if dispatcher and role in {"worker", "dispatcher"}
                else None
            )
            return role, f"{label} · {name}" if name else label
    return None, "행위자 정보 없음"


def ai_draft_view(ticket, principal, stale=None, image_evidence=None):
    """Dispatcher-only AI draft card data; reporters and workers never receive it."""
    if principal is None or principal.assignment or principal.user.role != "dispatcher":
        return None
    draft = (ticket.safety_data or {}).get("ai_draft")
    if not isinstance(draft, dict):
        return None
    if stale is None:
        revision = draft.get("analysis_revision")
        stale = type(revision) is not int or revision != ticket.analysis_revision
    urgency = draft.get("ai_urgency")
    effective = getattr(ticket, "effective_urgency", None)
    more_urgent = (
        bool(draft.get("late"))
        and not stale
        and type(urgency) is int
        and type(effective) is int
        and urgency < effective
    )
    try:
        return AIDraftView.model_validate({
            **draft, **(image_evidence or {}), "stale": stale, "more_urgent": more_urgent,
        })
    except ValueError:
        return None


async def current_ai_draft_view(db, ticket, principal):
    if principal is None or principal.assignment or principal.user.role != "dispatcher":
        return None
    draft = (ticket.safety_data or {}).get("ai_draft") or {}
    fingerprint = draft.get("input_hash")
    stale = None
    image_evidence = None
    if isinstance(fingerprint, str):
        from app.followup_questions import analysis_text
        from app.jobs import approved_image_snapshot, draft_image_evidence, input_fingerprint

        snapshot = await approved_image_snapshot(db, ticket.id)
        current = input_fingerprint(
            db, analysis_text(ticket), snapshot, draft.get("selected_image_digests", [])
        )
        stale = not hmac.compare_digest(fingerprint, current)
        location_hash = draft.get("location_input_hash")
        if isinstance(location_hash, str):
            stale = stale or not hmac.compare_digest(
                location_hash, input_fingerprint(db, ticket.location_text, [], [])
            )
        if draft.get("image_count") is None and not draft.get("reused"):
            # Older drafts can be linked only to the attempt that produced this exact revision.
            observed = (await db.scalars(
                select(ProcessingAttempt.metadata_json)
                .join(ProcessingJob, ProcessingJob.id == ProcessingAttempt.job_id)
                .where(
                    ProcessingJob.ticket_id == ticket.id,
                    ProcessingJob.kind == "ticket_analysis",
                    ProcessingJob.input_revision == draft.get("analysis_revision"),
                    ProcessingAttempt.mode == "live",
                    ProcessingAttempt.applied.is_(True),
                    ProcessingAttempt.metadata_json["input_hash"].as_string() == fingerprint,
                )
                .limit(2)
            )).all()
            if len(observed) == 1:
                image_evidence = draft_image_evidence(observed[0])
    return ai_draft_view(ticket, principal, stale, image_evidence)


def can_correct_location_text(settings, ticket, principal) -> bool:
    return bool(
        settings
        and settings.deployment_mode == "demo"
        and settings.planning_dataset_provider == "db"
        and principal
        and not principal.assignment
        and principal.user.role == "reporter"
        and principal.user.id == ticket.reporter_id
        and ticket.status == "received"
        and ticket.location_answer_count < 2
        and (ticket.location_data or {}).get("state") != "confirmed"
        and not (ticket.location_data or {}).get("selected")
    )


async def with_location_map(
    db: AsyncSession, ticket: Ticket, location: LocationView
) -> LocationView:
    settings = db.info.get("settings")
    if (
        settings is None
        or settings.deployment_mode not in {"service", "demo"}
        or settings.planning_dataset_provider != "db"
    ):
        return location
    unresolved = location.source_type == "unregistered" and location.state != "confirmed"
    if not unresolved and (location.source_type not in {"service", "catalog"} or not (
        location.selected or location.candidates
    )):
        return location
    active = await db.get(SchoolCatalogActive, 1)
    current = active is not None and (
        location.dataset_hash == active.dataset_hash
        and location.dataset_revision == active.dataset_revision
        and active.dataset_revision is not None
        if location.source_type == "service"
        else location.catalog_revision == active.catalog_revision
    )
    catalog = await db.get(
        SchoolCatalogVersion, active.catalog_revision
    ) if active and (current or unresolved) else None
    registered = {
        item["code"]: item for item in catalog.content.get("locations", [])
    } if catalog else {}

    def place(candidate):
        item = registered.get(candidate.code, {})
        lat, lon = item.get("latitude"), item.get("longitude")
        valid = (
            item.get("coordinate_system") == "WGS84"
            and type(lat) in (float, int) and type(lon) in (float, int)
            and math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180
        )
        return candidate.model_copy(update={
            "latitude": lat if valid else None, "longitude": lon if valid else None,
        })

    candidates = location.candidates
    if unresolved and catalog:
        from app.location_workflow import catalog_candidates_with_qr

        hint = next(
            (item.get("code") for item in reversed(ticket.location_history or [])
             if item.get("kind") == "qr_hint" and item.get("catalog_revision") == catalog.revision),
            None,
        )
        matches, _ = catalog_candidates_with_qr(
            catalog.content, ticket.description, ticket.location_text, hint,
        )
        candidates = [LocationCandidate.model_validate(item) for item in matches]
    return location.model_copy(update={
        "selected": place(location.selected) if location.selected else None,
        "candidates": [place(item) for item in candidates],
    })


async def ticket_view(
    db: AsyncSession, ticket: Ticket, principal=None, *, include_map: bool = False
) -> TicketView:
    snapshot = ticket.catalog_snapshot or {}
    category = (
        snapshot
        if all(key in snapshot for key in ("label", "domain", "department_code"))
        else CATEGORY_MAP.get(ticket.category_code, CATEGORY_MAP["UNKNOWN"])
    )
    events = (
        await db.scalars(
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket.id)
            .order_by(TicketEvent.created_at, TicketEvent.id)
        )
    ).all()
    dispatcher = bool(
        principal and not principal.assignment and principal.user.role == "dispatcher"
    )
    actor_ids = {e.actor_id for e in events if e.actor_id} if dispatcher else set()
    actor_names = (
        dict(
            (
                await db.execute(select(User.id, User.display_name).where(User.id.in_(actor_ids)))
            ).all()
        )
        if actor_ids
        else {}
    )
    uploads = (
        await db.scalars(
            select(Upload)
            .where(Upload.ticket_id == ticket.id, Upload.information_request_id.is_(None))
            .order_by(Upload.created_at, Upload.id)
        )
    ).all()
    # DB and disk are checked independently. Any mismatch or uncheckable path
    # hides historical photos until managed access is supported.
    if uploads:
        from starlette.concurrency import run_in_threadpool

        from app.storage import marker_present

        managed = await managed_db_present(db)
        settings = db.info.get("settings")
        if managed:
            access = db.info.get("managed_photo_access")
            if access is None:
                uploads = []
            else:
                from app.managed_access import checked_bytes, current_variant

                visible = []
                for upload in uploads:
                    try:
                        if upload.report_id is None:
                            if (
                                upload.owner_id != ticket.reporter_id
                                or upload.purpose != "ticket"
                                or upload.assignment_id is not None
                            ):
                                continue
                        else:
                            linked_report = await db.get(CompletionReport, upload.report_id)
                            linked_assignment = (
                                await db.get(Assignment, upload.assignment_id)
                                if upload.assignment_id
                                else None
                            )
                            if (
                                upload.purpose != "report"
                                or not linked_report
                                or not linked_assignment
                                or linked_report.ticket_id != ticket.id
                                or linked_report.assignment_id != linked_assignment.id
                                or linked_assignment.ticket_id != ticket.id
                                or linked_assignment.worker_id != upload.owner_id
                            ):
                                continue
                        await checked_bytes(db, *access, upload)
                        if upload.current_variant_id:
                            variant = await db.get(ImageVariant, upload.current_variant_id)
                            if not variant or variant.upload_id != upload.id:
                                continue
                            if variant and variant.status == "ready":
                                await checked_bytes(
                                    db, *access, upload, await current_variant(db, upload)
                                )
                        visible.append(upload)
                    except HTTPException:
                        continue
                uploads = visible
        else:
            try:
                marker = (
                    True
                    if settings is None
                    else await run_in_threadpool(marker_present, settings.storage_path)
                )
            except OSError:
                marker = True
            if marker:
                uploads = []
    assignment = await db.scalar(
        select(Assignment)
        .join(User, User.id == Assignment.worker_id)
        .where(Assignment.ticket_id == ticket.id, current_assignment_clause())
        .order_by(Assignment.created_at.desc())
        .limit(1)
    )
    worker = await db.get(User, assignment.worker_id) if assignment else None
    if not assignment_current(assignment, worker):
        assignment, worker = None, None
    report = await db.scalar(
        select(CompletionReport)
        .where(
            CompletionReport.ticket_id == ticket.id,
            CompletionReport.assignment_id == assignment.id if assignment else False,
            CompletionReport.superseded_at.is_(None),
        )
        .order_by(CompletionReport.created_at.desc(), CompletionReport.id.desc())
        .limit(1)
    )
    job = await db.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.ticket_id == ticket.id,
            ProcessingJob.kind == "ticket_analysis",
            ProcessingJob.input_revision == ticket.analysis_revision
            if not db.info.get("settings") or db.info["settings"].llm_fake
            else True,
        )
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
        .limit(1)
    )
    internal = principal is not None and (
        principal.assignment is not None or principal.user.role == "dispatcher"
    )
    visit_followup = None
    if principal and principal.user.role == "dispatcher" and assignment and report:
        from app.execution import pending_report, phase

        if report.reviewed and (report.confirmed_result or report.result) != "completed":
            current_phase = phase(assignment, ticket)
            if current_phase == "visit_ended":
                visit_followup = "reassign"
            elif current_phase in {"moving", "working"} and not await pending_report(
                db, assignment
            ):
                visit_followup = "end_visit"
    approved = list(
        (
            await db.scalars(
                select(CompletionReport)
                .where(CompletionReport.ticket_id == ticket.id, CompletionReport.reviewed.is_(True))
                .order_by(CompletionReport.created_at, CompletionReport.id)
            )
        ).all()
    )
    approved = [
        r
        for r in approved
        if r.approved_snapshot
        and (
            not principal or not principal.assignment or r.assignment_id == principal.assignment.id
        )
    ]

    async def rendered(item):
        from app.mail_outbox import delivery_status

        rendered_report = report_view(
            item,
            [await photo_view(db, u) for u in uploads if u.report_id == item.id],
            internal=internal,
        )
        if not principal or not principal.assignment:
            rendered_report.delivery_status = await delivery_status(
                db,
                report_id=item.id,
                ticket_id=ticket.id,
                recipient_id=principal.user.id if principal and not internal else None,
            )
        return rendered_report

    if not internal:
        report = approved[-1] if approved else None
    from app.duplicates import aggregate_safety, view_fields

    merge_fields = await view_fields(db, ticket, principal)
    risk = aggregate_safety(ticket)
    safety_fields = {**ticket.safety_data, **risk}
    from app.models import InformationRequest

    information = await db.scalar(
        select(InformationRequest)
        .where(InformationRequest.ticket_id == ticket.id)
        .order_by(InformationRequest.created_at.desc(), InformationRequest.id.desc())
        .limit(1)
    )
    information_status = (
        {"open": "waiting", "answered": "answered"}.get(information.status) if information else None
    )
    followup_view = None
    if (principal and not principal.assignment and principal.user.role in {"reporter", "dispatcher"}
            and (principal.user.role == "dispatcher" or principal.user.id == ticket.reporter_id)):
        followup_view = ticket.ai_followup_data
    return TicketView(
        ai_followup=followup_view,
        information_status=information_status,
        information_mail_allowed=bool(ticket.information_mail_allowed),
        **merge_fields,
        analysis_revision=ticket.analysis_revision,
        processing={
            "state": job.state,
            "attempt_count": job.attempt_count,
            "error_code": job.error_code,
            "next_attempt_at": job.next_attempt_at,
        }
        if job
        else None,
        location=(
            await with_location_map(db, ticket, LocationView.model_validate(ticket.location_data))
            if include_map
            else LocationView.model_validate(ticket.location_data)
        ).model_copy(
            update={
                "can_correct_text": can_correct_location_text(
                    db.info.get("settings"), ticket, principal
                )
            }
        ),
        routing=RoutingView.model_validate(ticket.routing_data),
        safety=SafetyView(
            **safety_fields,
            assessment_version=ticket.safety_version,
            override_reason=ticket.override_reason,
        ),
        id=ticket.id,
        public_no=ticket.public_no or f"B-{str(ticket.id)[:8]}",
        status=ticket.status,
        text=ticket.description,
        location_text=ticket.location_text,
        category_code=ticket.category_code,
        catalog_revision=ticket.catalog_revision,
        category_label=category["label"],
        domain=category["domain"],
        department_code=category["department_code"],
        ai_status=ticket.ai_status,
        ai_mode="live"
        if ((ticket.safety_data or {}).get("ai_draft") or {}).get("mode") == "live"
        else "replay",
        ai_reused=((ticket.safety_data or {}).get("ai_draft") or {}).get("reused") is True,
        ai_draft=await current_ai_draft_view(db, ticket, principal),
        photos=[await photo_view(db, u) for u in uploads if u.report_id is None],
        timeline=[
            EventView(
                id=e.id,
                type=e.event_type,
                at=e.created_at,
                label=(
                    "QR로 시작"
                    if e.event_type == "created"
                    and any(
                        entry.get("kind") == "qr_hint"
                        for entry in (ticket.location_history or [])
                        if isinstance(entry, dict)
                    )
                    else event_label(e.event_type)
                ),
                actor_kind=event_actor(e, actor_names, dispatcher, ticket.reporter_id)[0],
                actor_label=event_actor(e, actor_names, dispatcher, ticket.reporter_id)[1],
                # Free-form historical reasons can contain staff or model names.
                reason=(
                    event_label(e.event_type, "업무 상태 변경")
                    if e.event_type in _AI_UNKNOWN_EVENTS or not dispatcher
                    else e.reason
                ),
                from_status=e.from_status,
                to_status=e.to_status,
            )
            for e in events
            if internal
            or e.event_type
            not in {
                "report_confirmed",
                "report_analysis_succeeded",
                "report_analysis_failed",
                "report_analysis_drafted",
                "report_analysis_live_failed",
                "report_dismissed",
                "report_correction_requested",
            }
        ],
        assignment_id=assignment.id if assignment else None,
        worker_name=worker.display_name if worker and dispatcher else None,
        visit_followup=visit_followup,
        latest_report=await rendered(report) if report else None,
        approved_reports=[await rendered(item) for item in approved],
        orphaned_reports=[
            await rendered(item)
            for item in (
                await db.scalars(
                    select(CompletionReport)
                    .where(
                        CompletionReport.ticket_id == ticket.id,
                        CompletionReport.assignment_id != assignment.id if assignment else True,
                        CompletionReport.reviewed.is_(False),
                        CompletionReport.superseded_at.is_(None),
                    )
                    .order_by(CompletionReport.created_at, CompletionReport.id)
                )
            ).all()
        ]
        if principal and not principal.assignment and principal.user.role == "dispatcher"
        else [],
        reply_text=approved[-1].reply_text if approved else None,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def public_visible_ticket(at):
    """Exclude expired tickets and live tickets linked to expired snapshots."""
    other = aliased(Ticket)
    linked_expired = exists(
        select(TicketMerge.id)
        .join(
            other,
            or_(
                and_(TicketMerge.source_id == Ticket.id, TicketMerge.target_id == other.id),
                and_(TicketMerge.target_id == Ticket.id, TicketMerge.source_id == other.id),
            ),
        )
        .where(other.created_at <= at - timedelta(days=7))
    )
    return and_(Ticket.created_at > at - timedelta(days=7), ~linked_expired)


async def get_ticket(db: AsyncSession, ticket_id: uuid.UUID, principal: Principal, *, lock=False):
    query = select(Ticket).where(Ticket.id == ticket_id)
    if db.info["settings"].deployment_mode == "public_demo":
        query = query.where(public_visible_ticket(now()))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    ticket = await db.scalar(query)
    if not ticket:
        raise HTTPException(404, "ticket_not_found")
    if principal.assignment:
        if principal.assignment.ticket_id != ticket.id:
            raise HTTPException(404, "ticket_not_found")
    elif principal.user.role != "dispatcher" and ticket.reporter_id != principal.user.id:
        raise HTTPException(404, "ticket_not_found")
    return ticket


def transition(db, ticket, actor, event_type, reason, target=None):
    if event_type not in {
        "created",
        "received",
        "analysis_succeeded",
        "analysis_matched_replay",
        "analysis_unmatched",
        "analysis_failed",
    }:
        ticket.analysis_revision += 1
        settings = db.info.get("settings")
        if (not settings or settings.llm_fake) and ticket.ai_status in {
            "queued",
            "running",
            "retry_wait",
        }:
            ticket.ai_status = "superseded"
    previous = ticket.status
    if target is not None:
        ticket.status = target
    ticket.updated_at = now()
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            created_at=now(),
            actor_id=actor.id,
            event_type=event_type,
            reason=reason,
            from_status=previous,
            to_status=ticket.status,
        )
    )


def require_state(ticket, *allowed):
    if ticket.status not in allowed:
        raise HTTPException(409, "invalid_transition")


async def bind_uploads(db, ids, principal, ticket, report_id=None):
    for upload_id in sorted(ids, key=str):
        upload = await db.scalar(select(Upload).where(Upload.id == upload_id).with_for_update())
        if (
            not upload
            or upload.owner_id != principal.user.id
            or upload.deleted
            or upload.delete_after <= now()
            or upload.ticket_id is not None
            or upload.assignment_id != (principal.assignment.id if principal.assignment else None)
        ):
            raise HTTPException(403, "upload_forbidden")
        upload.ticket_id = ticket.id
        upload.report_id = report_id


async def idempotency(
    db: AsyncSession, request: Request, scope: str, payload, *, view_type=TicketView
):
    key = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
        raise HTTPException(422, "idempotency_key_required")
    scope = f"{scope}:{key}"
    body = payload.model_dump(mode="json")
    # Preserve pre-v112 create receipts when the new optional field was omitted.
    if scope.startswith("create:") and body.get("information_mail_allowed") is None:
        body.pop("information_mail_allowed", None)
    if (scope.startswith("ai_followup:") or scope.startswith("information_answer:")) and (
        body.get("analyze_with_ai") is False
    ):
        body.pop("analyze_with_ai", None)
    payload_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lock_id = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    prior = await db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope))
    if prior and prior.payload_hash != payload_hash:
        raise HTTPException(409, "idempotency_conflict")
    return (
        scope,
        payload_hash,
        view_type.model_validate_json(prior.response_json) if prior else None,
    )


def save_idempotency(db, scope, payload_hash, view):
    db.add(
        IdempotencyRecord(
            scope=scope, payload_hash=payload_hash, response_json=view.model_dump_json()
        )
    )
