"""Explicit dispatcher recovery of one approved reply delivery intent."""

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.auth_service import LOCAL_STAFF_IDS, service_account_eligible
from app.mail_outbox import (
    TEMPLATE_VERSION,
    approval_hash,
    encrypt,
    external_notification_suppressed,
    namespace,
)
from app.models import (
    CompletionReport,
    IdempotencyRecord,
    MailJob,
    Ticket,
    TicketEvent,
    TicketMerge,
    User,
)
from app.reporting import REPLIES
from app.schemas import Input
from app.security import authenticate, now, role

router = APIRouter(prefix="/v1")


class RecoveryInput(Input):
    expected_state: Literal["queued", "retry_wait", "superseded", "failed", "expired", "unknown"]
    expected_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_duplicate: bool = Field(strict=True)


class RecoveryItem(BaseModel):
    id: uuid.UUID
    state: str
    snapshot_hash: str
    legacy: bool
    ticket_public_no: str
    relationship: Literal["direct", "merged"]


class RecoveryResult(BaseModel):
    id: uuid.UUID
    state: Literal["queued"] = "queued"


def eligible(job):
    return job.state in {"failed", "expired", "unknown"} or (
        not job.approval_hash
        and (
            job.state in {"queued", "retry_wait"}
            or (job.state == "superseded" and job.error_code == "publication_unavailable")
        )
    )


def valid_report(report):
    return bool(
        report
        and report.reviewed
        and report.approved_at
        and report.approved_by
        and report.approved_snapshot
        and report.approved_snapshot.get("confirmed_result") in REPLIES
        and report.reply_text == REPLIES[report.approved_snapshot["confirmed_result"]]
    )


@router.get(
    "/tickets/{ticket_id}/reports/{report_id}/mail-recovery", response_model=list[RecoveryItem]
)
async def candidates(ticket_id: uuid.UUID, report_id: uuid.UUID, request: Request):
    settings = request.app.state.settings
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        if principal.user.id in LOCAL_STAFF_IDS:
            raise HTTPException(403, "recovery_unavailable")
        if settings.deployment_mode != "service":
            raise HTTPException(404, "recovery_unavailable")
        report = await db.get(CompletionReport, report_id)
        if not valid_report(report) or report.ticket_id != ticket_id:
            raise HTTPException(404, "report_not_found")
        children = select(MailJob.recovery_parent_id).where(MailJob.recovery_parent_id.is_not(None))
        jobs = (
            await db.scalars(
                select(MailJob)
                .where(
                    MailJob.report_id == report_id,
                    MailJob.purpose == "approved_reply",
                    MailJob.namespace == namespace(settings),
                    ~MailJob.id.in_(children),
                )
                .order_by(MailJob.created_at, MailJob.id)
            )
        ).all()
        if await external_notification_suppressed(db, report):
            raise HTTPException(409, "recovery_unavailable")
        snapshot = approval_hash(report)
        result = []
        for job in jobs:
            if not eligible(job) or (job.approval_hash and job.approval_hash != snapshot):
                continue
            ticket = await db.get(Ticket, job.ticket_id)
            if not ticket:
                continue
            result.append(
                RecoveryItem(
                    id=job.id,
                    state=job.state,
                    snapshot_hash=snapshot,
                    legacy=not bool(job.approval_hash),
                    ticket_public_no=ticket.public_no or f"B-{str(ticket.id)[:8]}",
                    relationship="merged" if job.merge_id else "direct",
                )
            )
        return result


@router.post(
    "/tickets/{ticket_id}/reports/{report_id}/mail-recovery/{job_id}", response_model=RecoveryResult
)
async def recover(
    ticket_id: uuid.UUID,
    report_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: RecoveryInput,
    request: Request,
):
    settings = request.app.state.settings
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        if principal.user.id in LOCAL_STAFF_IDS:
            raise HTTPException(403, "recovery_unavailable")
        if settings.deployment_mode != "service" or settings.mail_delivery not in {
            "local",
            "smtp",
            "gmail",
        }:
            raise HTTPException(404, "recovery_unavailable")
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise HTTPException(422, "idempotency_key_required")
        scope = f"mail-recovery:{principal.user.id}:{hashlib.sha256(key.encode()).hexdigest()}"
        digest = hashlib.sha256(
            json.dumps(
                {
                    "ticket": str(ticket_id),
                    "report": str(report_id),
                    "job": str(job_id),
                    "payload": payload.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        lock_id = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
        prior = await db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope))
        if prior:
            previous_report = await db.get(CompletionReport, report_id)
            if previous_report and await external_notification_suppressed(db, previous_report):
                raise HTTPException(409, "recovery_unavailable")
            if prior.payload_hash != digest:
                raise HTTPException(409, "idempotency_conflict")
            return RecoveryResult.model_validate_json(prior.response_json)
        job = await db.scalar(select(MailJob).where(MailJob.id == job_id).with_for_update())
        report = await db.get(CompletionReport, report_id)
        if (
            not job
            or job.report_id != report_id
            or job.purpose != "approved_reply"
            or job.namespace != namespace(settings)
            or not valid_report(report)
            or report.ticket_id != ticket_id
        ):
            raise HTTPException(409, "recovery_unavailable")
        if await external_notification_suppressed(db, report):
            raise HTTPException(409, "recovery_unavailable")
        snapshot = approval_hash(report)
        if (
            job.state != payload.expected_state
            or not eligible(job)
            or snapshot != payload.expected_snapshot_hash
            or (job.approval_hash and job.approval_hash != snapshot)
            or (job.state == "unknown" and not payload.acknowledge_duplicate)
        ):
            raise HTTPException(409, "recovery_changed")
        if await db.scalar(select(MailJob.id).where(MailJob.recovery_parent_id == job.id)):
            raise HTTPException(409, "recovery_already_requested")
        ticket = await db.get(Ticket, job.ticket_id)
        user = await db.get(User, job.recipient_id)
        if (
            not ticket
            or not user
            or ticket.reporter_id != user.id
            or not service_account_eligible(user)
        ):
            raise HTTPException(409, "recipient_unavailable")
        if job.merge_id:
            link = await db.get(TicketMerge, job.merge_id)
            if (
                not link
                or not link.active
                or not link.subscribed
                or link.source_id != ticket.id
                or link.target_id != report.ticket_id
            ):
                raise HTTPException(409, "subscription_ended")
        elif ticket.id != report.ticket_id:
            raise HTTPException(409, "recipient_unavailable")
        if job.state in {"queued", "retry_wait"}:
            job.state, job.error_code, job.encrypted_payload, job.finished_at = (
                "superseded",
                "recovered_legacy",
                None,
                now(),
            )
        child_id = uuid.uuid4()
        body = {
            "to": user.verified_email,
            "subject": "바로AI 접수 업데이트 안내",
            "body": "접수한 신고에 업데이트가 있어요. 로그인 후 본인 접수 내용을 확인해 주세요.\n"
            + settings.mail_link_origin.rstrip("/")
            + f"/t/{ticket.id}",
        }
        child = MailJob(
            id=child_id,
            purpose="approved_reply",
            namespace=namespace(settings),
            delivery=settings.mail_delivery,
            template_version=TEMPLATE_VERSION,
            dedupe_key=f"recovery:{job.id}",
            recovery_parent_id=job.id,
            encrypted_payload=encrypt(settings, body),
            ticket_id=ticket.id,
            report_id=report.id,
            recipient_id=user.id,
            merge_id=job.merge_id,
            approval_hash=snapshot,
            expires_at=now() + timedelta(hours=24),
        )
        db.add(child)
        db.add(
            TicketEvent(
                ticket_id=report.ticket_id,
                actor_id=principal.user.id,
                event_type="mail_recovery_requested",
                reason=f"알림 복구 요청: {job.id} → {child_id}; 이전 상태 {payload.expected_state}",
                from_status=None,
                to_status=None,
            )
        )
        result = RecoveryResult(id=child_id)
        db.add(
            IdempotencyRecord(
                scope=scope, payload_hash=digest, response_json=result.model_dump_json()
            )
        )
        await db.flush()
        return result
