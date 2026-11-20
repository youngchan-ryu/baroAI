"""Managed photo access primitives. Public routes remain disabled."""

import hashlib
from contextlib import asynccontextmanager

from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import Response

from app.dispatch_lock import dispatch_lock
from app.models import (
    Assignment,
    CompletionReport,
    ImageApproval,
    ImageVariant,
    StorageIdentity,
    StorageObject,
    Upload,
)
from app.protection import expected_purpose, get_upload, photo_view, revoke_approvals
from app.security import authenticate, locked_assignment, now
from app.storage_objects import managed_transaction, read_ready
from app.storage_provision import storage_lock
from app.tickets import get_ticket


@asynccontextmanager
async def access_transaction(database, settings):
    """Report storage failures without exposing paths or underlying OS errors."""
    try:
        async with managed_transaction(database, settings) as access:
            yield access
    except (OSError, ValueError):
        raise HTTPException(503, "photo_storage_unavailable") from None


async def checked_bytes(db, identity, directory, upload, variant=None):
    try:
        data = await read_ready(
            db,
            identity,
            directory,
            key=variant.object_key if variant else upload.object_key,
            owner_id=upload.owner_id,
            kind="variant" if variant else "original",
        )
    except (OSError, ValueError):
        raise HTTPException(409, "image_not_ready") from None
    if variant and hashlib.sha256(data).hexdigest() != variant.sha256:
        raise HTTPException(409, "image_hash_mismatch")
    return data


async def current_variant(db, upload, variant_id=None):
    if variant_id is not None and variant_id != upload.current_variant_id:
        raise HTTPException(409, "stale_image_variant")
    variant = (
        await db.get(ImageVariant, upload.current_variant_id) if upload.current_variant_id else None
    )
    if (
        not variant
        or variant.upload_id != upload.id
        or variant.status != "ready"
        or not variant.object_key
    ):
        raise HTTPException(409, "image_not_ready")
    if variant.delete_after <= now():
        raise HTTPException(410, "image_deleted")
    return variant


async def managed_metadata(request, upload_id):
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        await checked_bytes(db, identity, directory, upload)
        if upload.current_variant_id:
            variant = await db.get(ImageVariant, upload.current_variant_id)
            if not variant or variant.upload_id != upload.id:
                raise HTTPException(409, "image_not_ready")
            if variant.status == "ready" and variant.delete_after > now():
                await checked_bytes(db, identity, directory, upload, variant)
        return await photo_view(db, upload)


async def managed_image(request, upload_id, *, source=False, variant_id=None):
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=source)
        variant = None if source else await current_variant(db, upload, variant_id)
        data = await checked_bytes(db, identity, directory, upload, variant)
        return Response(
            data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


async def managed_approve(request, upload_id, payload):
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        variant = await current_variant(db, upload, payload.variant_id)
        if (
            variant.sha256 != payload.sha256
            or variant.pipeline_version != payload.pipeline_version
            or payload.provider != request.app.state.settings.llm_primary
            or payload.purpose != expected_purpose(upload)
        ):
            raise HTTPException(409, "image_approval_mismatch")
        await checked_bytes(db, identity, directory, upload, variant)
        await revoke_approvals(db, upload)
        db.add(
            ImageApproval(
                upload_id=upload.id,
                variant_id=variant.id,
                sha256=variant.sha256,
                pipeline_version=variant.pipeline_version,
                actor_id=principal.user.id,
                provider=payload.provider,
                purpose=payload.purpose,
            )
        )
        await db.flush()
        return await photo_view(db, upload)


async def managed_revoke(request, upload_id):
    # Consent withdrawal is DB-only, including while storage is unavailable.
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        await storage_lock(db)
        identity = await db.get(StorageIdentity, 1)
        if identity is None or identity.state != "active":
            raise HTTPException(409, "managed_storage_not_available")
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        tracked = await db.scalar(
            select(StorageObject)
            .where(
                StorageObject.storage_id == identity.storage_id,
                StorageObject.object_key == upload.object_key,
                StorageObject.owner_id == upload.owner_id,
                StorageObject.kind == "original",
            )
            .with_for_update()
        )
        if tracked is None:
            raise HTTPException(409, "image_not_ready")
        await revoke_approvals(db, upload)
        await db.flush()
        return await photo_view(db, upload)


async def managed_approved_bytes(request, upload_id, *, ticket_id, report_id, provider, purpose):
    """Reauthenticate under managed locks; future external transfer needs a new check."""
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        principal = await authenticate(request, db, worker_allowed=True)
        await get_ticket(db, ticket_id, principal, lock=True)
        if principal.assignment:
            await locked_assignment(db, principal, principal.assignment.id)
        return await _approved_bytes(
            db,
            identity,
            directory,
            upload_id,
            principal,
            ticket_id=ticket_id,
            report_id=report_id,
            provider=provider,
            purpose=purpose,
        )


async def _approved_bytes(
    db, identity, directory, upload_id, principal, *, ticket_id, report_id, provider, purpose
):
    upload = await get_upload(db, upload_id, principal, owner_only=True)
    if (
        upload.ticket_id != ticket_id
        or upload.report_id != report_id
        or expected_purpose(upload) != purpose
    ):
        raise HTTPException(409, "image_scope_mismatch")
    if upload.assignment_id:
        assignment = await db.get(Assignment, upload.assignment_id, populate_existing=True)
        if (
            not assignment
            or not principal.assignment
            or principal.assignment.id != assignment.id
            or assignment.revoked
            or (principal.session is None and assignment.expires_at <= now())
        ):
            raise HTTPException(409, "image_scope_mismatch")
    variant = await current_variant(db, upload)
    approval = await db.scalar(
        select(ImageApproval).where(
            ImageApproval.upload_id == upload.id,
            ImageApproval.variant_id == variant.id,
            ImageApproval.revoked_at.is_(None),
            ImageApproval.actor_id == upload.owner_id,
            ImageApproval.provider == provider,
            ImageApproval.purpose == purpose,
            ImageApproval.sha256 == variant.sha256,
            ImageApproval.pipeline_version == variant.pipeline_version,
        )
    )
    if not approval:
        raise HTTPException(409, "image_not_approved")
    return await checked_bytes(db, identity, directory, upload, variant)


async def managed_bind(request, ids, ticket_id, report_id=None):
    """Standalone internal attachment; not a substitute for atomic report creation."""
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        principal = await authenticate(request, db, worker_allowed=True)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        if principal.assignment:
            assignment = await locked_assignment(db, principal, principal.assignment.id)
            if assignment.stopped_at or assignment.visit_ended_at or ticket.status != "in_progress":
                raise HTTPException(409, "invalid_transition")
        elif ticket.status != "created":
            raise HTTPException(409, "invalid_transition")
        await _bind(db, identity, directory, ids, principal, ticket, report_id)
        if ids:
            if report_id:
                report = await db.get(CompletionReport, report_id)
                report.revision += 1
                report.analysis_status = "superseded"
            else:
                ticket.analysis_revision += 1
                ticket.updated_at = now()
                if ticket.ai_status in {"queued", "running", "retry_wait"}:
                    ticket.ai_status = "superseded"
        await db.flush()


async def _bind(db, identity, directory, ids, principal, ticket, report_id=None):
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "duplicate_upload")
    if principal.assignment:
        if principal.assignment.ticket_id != ticket.id or report_id is None:
            raise HTTPException(403, "upload_forbidden")
        report = await db.scalar(
            select(CompletionReport)
            .where(CompletionReport.id == report_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not report
            or report.ticket_id != ticket.id
            or report.assignment_id != principal.assignment.id
            or report.reviewed
            or report.worker_confirmed_revision is not None
            or report.analysis_status == "dismissed"
        ):
            raise HTTPException(403, "upload_forbidden")
    elif (
        principal.user.role != "reporter"
        or ticket.reporter_id != principal.user.id
        or report_id is not None
    ):
        raise HTTPException(403, "upload_forbidden")
    for upload_id in sorted(ids, key=str):
        upload = await db.scalar(select(Upload).where(Upload.id == upload_id).with_for_update())
        if (
            not upload
            or upload.purpose != ("report" if report_id else "ticket")
            or upload.owner_id != principal.user.id
            or upload.deleted
            or upload.delete_after <= now()
            or upload.ticket_id is not None
            or upload.assignment_id != (principal.assignment.id if principal.assignment else None)
        ):
            raise HTTPException(403, "upload_forbidden")
        await checked_bytes(db, identity, directory, upload)
        variant = (
            await db.get(ImageVariant, upload.current_variant_id)
            if upload.current_variant_id
            else None
        )
        if not variant or variant.upload_id != upload.id or variant.delete_after <= now():
            raise HTTPException(409, "image_not_ready")
        if variant.status == "ready":
            if not variant.object_key:
                raise HTTPException(409, "image_not_ready")
            await checked_bytes(db, identity, directory, upload, variant)
        elif variant.status != "blocked" or variant.object_key:
            raise HTTPException(409, "image_not_ready")
        upload.ticket_id, upload.report_id = ticket.id, report_id


async def managed_create_ticket(request, payload):
    """Internal only: creation and attachments commit together under managed gates."""
    from app.routes import _create_ticket_tx

    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        return await _create_ticket_tx(db, payload, request, managed=(identity, directory))


async def managed_report_work(request, assignment_id, payload):
    """Internal only: report and attachments commit together under managed gates."""
    from app.routes import _report_work_tx

    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        return await _report_work_tx(
            db, assignment_id, payload, request, managed=(identity, directory)
        )
