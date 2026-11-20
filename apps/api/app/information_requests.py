"""Dispatcher questions and reporter answers, separate from ticket state."""

import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.auth_service import LOCAL_STAFF_IDS, service_account_eligible
from app.dispatch_lock import dispatch_lock
from app.location_workflow import refresh_safety
from app.mail_outbox import encrypt, namespace
from app.models import ImageVariant, InformationRequest, MailJob, TicketEvent, Upload, User
from app.schemas import Input, PhotoView
from app.security import authenticate, now, role
from app.tickets import get_ticket, idempotency, save_idempotency

router = APIRouter(prefix="/v1/tickets/{ticket_id}/information-requests")
consent_router = APIRouter(prefix="/v1/tickets/{ticket_id}")


class Question(Input):
    question: str = Field(min_length=1, max_length=500)


class Answer(Input):
    answer: str = Field(min_length=1, max_length=2000)
    analyze_with_ai: bool = Field(default=False, strict=True)
    upload_ids: list[uuid.UUID] = Field(default_factory=list, max_length=3)

    @field_validator("upload_ids")
    @classmethod
    def unique_uploads(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("duplicate_upload")
        return value


class Empty(Input):
    pass


class ConsentInput(Input):
    allowed: bool = Field(strict=True)


class ConsentView(BaseModel):
    ticket_id: uuid.UUID
    information_mail_allowed: bool


class RequestView(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    question: str
    status: Literal["open", "answered", "cancelled"]
    requested_at: datetime
    answer: str | None
    answered_at: datetime | None
    cancelled_at: datetime | None
    mail_status: str
    photos: list[PhotoView] = Field(default_factory=list)


async def view(db, item, settings, principal):
    from app.models import Ticket
    from app.protection import get_upload, managed_db_present, photo_view, require_legacy_storage
    from app.storage import LocalPrivateStorage

    photos = []
    ticket = await db.get(Ticket, item.ticket_id)
    uploads = (
        await db.scalars(
            select(Upload)
            .where(
                Upload.information_request_id == item.id,
                Upload.ticket_id == item.ticket_id,
            )
            .order_by(Upload.created_at, Upload.id)
        )
    ).all()
    for candidate in uploads:
        try:
            upload = await get_upload(db, candidate.id, principal)
            if (
                not ticket
                or upload.owner_id != ticket.reporter_id
                or upload.purpose != "ticket"
                or upload.report_id is not None
                or upload.assignment_id is not None
            ):
                continue
            if await managed_db_present(db):
                access = db.info.get("managed_photo_access")
                if access is None:
                    continue
                from app.managed_access import checked_bytes, current_variant

                await checked_bytes(db, *access, upload)
                if upload.current_variant_id:
                    variant = await db.get(ImageVariant, upload.current_variant_id)
                    if not variant or variant.upload_id != upload.id:
                        continue
                    if variant.status == "ready":
                        await checked_bytes(db, *access, upload, await current_variant(db, upload))
            else:
                await require_legacy_storage(db, LocalPrivateStorage(settings.storage_path))
            projected = await photo_view(db, upload)
            if projected.privacy.status != "deleted":
                photos.append(projected)
        except HTTPException:
            continue
    job = await db.scalar(
        select(MailJob)
        .where(
            MailJob.information_request_id == item.id,
            MailJob.purpose == "information_request",
            MailJob.namespace == namespace(settings),
        )
        .order_by(MailJob.created_at.desc())
        .limit(1)
    )
    return RequestView(
        id=item.id,
        ticket_id=item.ticket_id,
        question=item.question,
        status=item.status,
        requested_at=item.created_at,
        answer=item.answer,
        answered_at=item.answered_at,
        cancelled_at=item.cancelled_at,
        photos=photos,
        mail_status=(
            "provider_accepted"
            if job.state == "accepted" and job.delivery in {"smtp", "gmail"}
            else "non_provider_accepted"
            if job.state == "accepted"
            else job.state
        )
        if job
        else (
            "demo_not_sent"
            if settings.deployment_mode != "service"
            else "failed"
            if item.mail_intent_failed
            else "not_sent"
        ),
    )


def add_event(db, ticket, actor, kind, reason):
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=actor.id,
            event_type=kind,
            reason=reason,
            from_status=ticket.status,
            to_status=ticket.status,
        )
    )
    ticket.updated_at = now()


async def intent(database, settings, ticket_id, item_id):
    """Create delivery separately: an unavailable mail system never rolls back the question."""
    if settings.deployment_mode != "service" or settings.mail_delivery == "disabled":
        return
    async with database.transaction() as db:
        await dispatch_lock(db)
        item = await db.scalar(
            select(InformationRequest)
            .where(InformationRequest.id == item_id, InformationRequest.ticket_id == ticket_id)
            .with_for_update()
        )
        if not item or item.status != "open" or item.requested_by in LOCAL_STAFF_IDS:
            return
        from app.models import Ticket

        ticket = await db.get(Ticket, ticket_id)
        user = await db.get(User, ticket.reporter_id) if ticket else None
        if not ticket.information_mail_allowed or not service_account_eligible(user):
            return
        exists = await db.scalar(
            select(MailJob.id)
            .where(
                MailJob.ticket_id == ticket_id,
                MailJob.purpose == "information_request",
                MailJob.namespace == namespace(settings),
                MailJob.created_at >= now() - timedelta(days=1),
            )
            .limit(1)
        )
        if exists:
            return
        question = item.question.replace("\r\n", "\n").replace("\r", "\n")
        question = "".join(
            char for char in question if char == "\n" or ord(char) >= 32 and ord(char) != 127
        )
        payload = {
            "to": user.verified_email,
            "subject": "바로AI 추가 내용 요청",
            "body": f"접수 번호: {ticket.public_no}\n담당자가 추가 내용을 요청했어요.\n\n"
            + f"질문:\n{question}\n\n"
            + settings.mail_link_origin.rstrip("/")
            + f"/t/{ticket.id}",
        }
        db.add(
            MailJob(
                purpose="information_request",
                namespace=namespace(settings),
                delivery=settings.mail_delivery,
                dedupe_key=f"information:{item.id}",
                encrypted_payload=encrypt(settings, payload),
                ticket_id=ticket.id,
                information_request_id=item.id,
                recipient_id=user.id,
                expires_at=now() + timedelta(hours=24),
            )
        )


@router.get("", response_model=list[RequestView])
async def list_requests(ticket_id: uuid.UUID, request: Request):
    from app.routes import _photo_aware_transaction

    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        if principal.assignment or principal.user.role not in {"reporter", "dispatcher"}:
            raise HTTPException(403, "role_forbidden")
        await get_ticket(db, ticket_id, principal)
        items = (
            await db.scalars(
                select(InformationRequest)
                .where(InformationRequest.ticket_id == ticket_id)
                .order_by(InformationRequest.created_at)
            )
        ).all()
        return [await view(db, item, request.app.state.settings, principal) for item in items]


@router.post("", response_model=RequestView)
async def create_request(ticket_id: uuid.UUID, payload: Question, request: Request):
    settings = request.app.state.settings
    if settings.deployment_mode == "public_demo":
        raise HTTPException(404, "information_request_unavailable")
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        scope, digest, prior = await idempotency(
            db,
            request,
            f"information:{ticket.id}:{principal.actor_scope}",
            payload,
            view_type=RequestView,
        )
        if prior:
            item_id = prior.id
        else:
            items = (
                await db.scalars(
                    select(InformationRequest).where(InformationRequest.ticket_id == ticket.id)
                )
            ).all()
            if any(item.status == "open" for item in items):
                raise HTTPException(409, "information_request_open")
            if len(items) >= 3:
                raise HTTPException(409, "information_request_limit")
            item = InformationRequest(
                ticket_id=ticket.id,
                requested_by=principal.user.id,
                question=payload.question,
                status="open",
            )
            db.add(item)
            add_event(db, ticket, principal.user, "information_requested", "추가 정보 요청")
            await db.flush()
            result = await view(db, item, settings, principal)
            save_idempotency(db, scope, digest, result)
            item_id = item.id
    try:
        await intent(request.app.state.database, settings, ticket_id, item_id)
    except Exception:
        # The request has already committed; record failure without message details.
        async with request.app.state.database.transaction() as db:
            item = await db.get(InformationRequest, item_id)
            if item:
                item.mail_intent_failed = True
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        await get_ticket(db, ticket_id, principal)
        item = await db.get(InformationRequest, item_id)
        return await view(db, item, settings, principal)


async def mutate(ticket_id, item_id, payload, request, action):
    settings = request.app.state.settings
    from app.routes import _photo_aware_transaction

    async with _photo_aware_transaction(request) as db:
        await dispatch_lock(db)
        principal = await authenticate(request, db)
        role(principal, "reporter" if action == "answer" else "dispatcher")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        item = await db.scalar(
            select(InformationRequest)
            .where(InformationRequest.id == item_id, InformationRequest.ticket_id == ticket.id)
            .with_for_update()
        )
        if not item:
            raise HTTPException(404, "information_request_not_found")
        scope, digest, prior = await idempotency(
            db,
            request,
            f"information_{action}:{ticket.id}:{item.id}:{principal.actor_scope}",
            payload,
            view_type=RequestView,
        )
        if prior:
            return await view(db, item, settings, principal)
        if item.status != "open":
            raise HTTPException(409, "information_request_closed")
        if action == "answer":
            if payload.upload_ids:
                from app.protection import managed_db_present, require_legacy_storage
                from app.storage import LocalPrivateStorage
                from app.tickets import bind_uploads

                for upload_id in sorted(payload.upload_ids, key=str):
                    upload = await db.scalar(
                        select(Upload).where(Upload.id == upload_id).with_for_update()
                    )
                    if (
                        not upload
                        or upload.purpose != "ticket"
                        or upload.information_request_id is not None
                    ):
                        raise HTTPException(403, "upload_forbidden")
                access = db.info.get("managed_photo_access")
                if await managed_db_present(db):
                    if not access:
                        raise HTTPException(503, "photo_storage_unavailable")
                    from app.managed_access import _bind

                    await _bind(db, *access, payload.upload_ids, principal, ticket)
                else:
                    await require_legacy_storage(db, LocalPrivateStorage(settings.storage_path))
                    await bind_uploads(db, payload.upload_ids, principal, ticket)
                for upload_id in payload.upload_ids:
                    upload = await db.get(Upload, upload_id)
                    upload.information_request_id = item.id
                    upload.photo_coordinates = None
            item.answer, item.answered_at, item.status = payload.answer, now(), "answered"
            ticket.information_safety_evidence = [
                *(ticket.information_safety_evidence or []),
                payload.answer,
            ]
            refresh_safety(ticket)
            ticket.followup_analysis_evidence = [
                *(ticket.followup_analysis_evidence or []),
                {
                    "source": "information_request",
                    "source_id": str(item.id),
                    "question": item.question,
                    "answer": payload.answer,
                    "analyze_with_ai": payload.analyze_with_ai,
                },
            ]
            from app.followup_questions import queue_reanalysis

            if payload.analyze_with_ai:
                await queue_reanalysis(db, ticket)
            add_event(db, ticket, principal.user, "information_answered", "신고자가 추가 답변")
        else:
            item.cancelled_at, item.status = now(), "cancelled"
            add_event(db, ticket, principal.user, "information_cancelled", "추가 정보 요청 취소")
        await db.flush()
        result = await view(db, item, settings, principal)
        save_idempotency(db, scope, digest, result)
        return result


@router.post("/{item_id}/cancel", response_model=RequestView)
async def cancel_request(
    ticket_id: uuid.UUID, item_id: uuid.UUID, payload: Empty, request: Request
):
    return await mutate(ticket_id, item_id, payload, request, "cancel")


@router.post("/{item_id}/answer", response_model=RequestView)
async def answer_request(
    ticket_id: uuid.UUID, item_id: uuid.UUID, payload: Answer, request: Request
):
    return await mutate(ticket_id, item_id, payload, request, "answer")


@consent_router.post("/information-mail-consent", response_model=ConsentView)
async def set_information_mail_consent(
    ticket_id: uuid.UUID, payload: ConsentInput, request: Request
):
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        principal = await authenticate(request, db)
        role(principal, "reporter")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        scope, digest, prior = await idempotency(
            db,
            request,
            f"information_consent:{ticket.id}:{principal.actor_scope}",
            payload,
            view_type=ConsentView,
        )
        if not prior:
            if ticket.information_mail_allowed != payload.allowed:
                ticket.information_mail_allowed = payload.allowed
                add_event(
                    db,
                    ticket,
                    principal.user,
                    "information_mail_consent",
                    "추가 정보 요청 메일 동의"
                    if payload.allowed
                    else "추가 정보 요청 메일 동의 철회",
                )
            result = ConsentView(ticket_id=ticket.id, information_mail_allowed=payload.allowed)
            save_idempotency(db, scope, digest, result)
        return ConsentView(
            ticket_id=ticket.id, information_mail_allowed=ticket.information_mail_allowed
        )
