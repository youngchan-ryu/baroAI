"""Unexposed managed upload flow; API/read integration remains disabled."""

import hashlib
import json
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import wraps

from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from app.models import CompletionReport, IdempotencyRecord, ImageVariant, StorageOperation, Upload
from app.protection import get_upload, photo_view
from app.security import authenticate, locked_assignment, now
from app.services import images
from app.storage import MAX_FILE, normalize_image
from app.storage_objects import (
    managed_transaction,
    reserve_locked,
    verify_for_publication,
    write_pending,
)
from app.tickets import get_ticket


def storage_errors(function):
    @wraps(function)
    async def call(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (OSError, ValueError):
            raise HTTPException(503, "photo_storage_unavailable") from None

    return call


async def check_upload_actor(db, request):
    principal = await authenticate(request, db, worker_allowed=True)
    if principal.assignment:
        ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        assignment = await locked_assignment(db, principal, principal.assignment.id)
        correction = await db.scalar(select(CompletionReport.id).where(
            CompletionReport.assignment_id == assignment.id,
            CompletionReport.reviewed.is_(False),
            CompletionReport.superseded_at.is_(None),
            CompletionReport.correction_requested_at.is_not(None),
        ).limit(1))
        if (
            ticket.status not in {"assigned", "in_progress"}
            or (assignment.visit_ended_at and not correction)
            or assignment.stopped_at
        ):
            raise HTTPException(409, "worker_upload_closed")
    elif principal.user.role != "reporter":
        raise HTTPException(403, "role_forbidden")
    return principal


def actor_snapshot(principal):
    return (
        principal.user.id,
        principal.user.role,
        principal.user.auth_epoch,
        principal.assignment.id if principal.assignment else None,
    )


def upload_scope(actor, request_key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_key):
        raise HTTPException(422, "idempotency_key_required")
    key_hash = hashlib.sha256(request_key.encode()).hexdigest()
    return f"managed-upload:{actor[0]}:{actor[3] or 'none'}:{key_hash}"


@asynccontextmanager
async def upload_request_lock(database, actor, request_key):
    """Serialize one actor/key through processing and any logical abandonment."""
    scope = upload_scope(actor, request_key)
    lock_id = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
    async with database.engine.connect() as connection:
        acquired = False
        try:
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_id})
            acquired = True
            yield
        finally:
            original = sys.exception()
            if acquired:
                try:
                    unlocked = await connection.scalar(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id}
                    )
                    if not unlocked:
                        await connection.invalidate()
                except BaseException:
                    try:
                        await connection.invalidate()
                    except BaseException:
                        if original is not None:
                            original.add_note("upload_lock_cleanup_unconfirmed")
                        else:
                            raise
                    if original is not None:
                        original.add_note("upload_lock_cleanup_unconfirmed")
                    else:
                        raise
            else:
                # Cancellation during acquisition leaves acquisition outcome unknown.
                await connection.invalidate()


@storage_errors
async def managed_operation_upload(request, file):
    """Use a committed operation and logically abandon failed unfinished work."""
    from app.storage_operations import (
        abandon_operation,
        begin_operation,
        freeze_upload_result,
        publish_upload,
    )

    operation_id = None
    try:
        raw = await file.read(MAX_FILE + 1)
        database, settings = request.app.state.database, request.app.state.settings
        async with managed_transaction(database, settings) as (db, _, _):
            actor = actor_snapshot(await check_upload_actor(db, request))
        async with upload_request_lock(database, actor, request.headers.get("idempotency-key", "")):
            async with managed_transaction(database, settings) as (db, _, _):
                if actor_snapshot(await check_upload_actor(db, request)) != actor:
                    raise HTTPException(409, "upload_authorization_changed")
            try:
                operation = await begin_operation(request, kind="upload", source_bytes=raw)
                operation_id = operation["id"]
                if operation["state"] == "published":
                    return await publish_upload(request, operation_id)
                result = await freeze_upload_result(request, operation_id, raw)
                coordinates = None
                if (settings.deployment_mode == "service" and actor[1] == "reporter"
                        and actor[3] is None
                        and request.headers.get("x-baro-photo-coordinates") == "consent"):
                    from app.storage import exif_gps

                    point = exif_gps(raw)
                    if point is not None:
                        coordinates = {"latitude": point[0], "longitude": point[1]}
                for object_id, data in zip(
                    result["object_ids"], (result["source"], result["variant"]), strict=False
                ):
                    await write_pending(
                        database, settings, uuid.UUID(object_id), data, request=request
                    )
                return await publish_upload(request, operation_id, coordinates=coordinates)
            except BaseException:
                if operation_id is not None:
                    try:
                        await abandon_operation(request, operation_id)
                    except Exception:  # noqa: BLE001 - retain the original failure
                        pass
                raise
    finally:
        await file.close()


async def existing_upload(db, principal, scope, payload_hash):
    from sqlalchemy import select

    prior = await db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope))
    if not prior:
        return None
    if prior.payload_hash != payload_hash:
        raise HTTPException(409, "idempotency_conflict")
    try:
        import uuid

        upload_id = uuid.UUID(json.loads(prior.response_json)["upload_id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "idempotency_record_invalid") from None
    upload = await get_upload(db, upload_id, principal, owner_only=True)
    return await photo_view(db, upload)


@storage_errors
async def managed_upload(request, file):
    """Authenticate twice and atomically publish source/processed references.

    Managed routes delegate here with authorization and storage checks.
    The photo transition remains in progress; public-demo photos stay blocked.
    """
    database, settings = request.app.state.database, request.app.state.settings
    try:
        async with managed_transaction(database, settings) as (db, _identity, _directory):
            principal = await check_upload_actor(db, request)
            actor = actor_snapshot(principal)
        scope = upload_scope(actor, request.headers.get("idempotency-key", ""))
        raw = await file.read(MAX_FILE + 1)
        payload_hash = hashlib.sha256(raw).hexdigest()
        async with managed_transaction(database, settings) as (db, _identity, _directory):
            principal = await check_upload_actor(db, request)
            if actor_snapshot(principal) != actor:
                raise HTTPException(409, "upload_authorization_changed")
            prior = await existing_upload(db, principal, scope, payload_hash)
            if prior is not None:
                return prior
        normalized = await run_in_threadpool(normalize_image, raw)
        del raw
        try:
            processed = await run_in_threadpool(images.process_image, normalized, [])
        except Exception:
            processed = images.ProcessedImage(
                "blocked", None, None, images.VERSION, 0, "processing_failed", "unavailable"
            )
        usable = (
            processed.status == "ready"
            and processed.data
            and processed.sha256
            and hashlib.sha256(processed.data).hexdigest() == processed.sha256
        )
        async with managed_transaction(database, settings) as (db, identity, _directory):
            principal = await check_upload_actor(db, request)
            if actor_snapshot(principal) != actor:
                raise HTTPException(409, "upload_authorization_changed")
            prior = await existing_upload(db, principal, scope, payload_hash)
            if prior is not None:
                return prior
            expiry = now() + timedelta(days=30)
            source_id = await reserve_locked(
                db,
                identity,
                owner_id=actor[0],
                kind="original",
                content=normalized,
                delete_after=expiry,
            )
            variant_id = None
            if usable:
                variant_id = await reserve_locked(
                    db,
                    identity,
                    owner_id=actor[0],
                    kind="variant",
                    content=processed.data,
                    delete_after=expiry,
                )
        await write_pending(database, settings, source_id, normalized)
        if variant_id is not None:
            await write_pending(database, settings, variant_id, processed.data)
        async with managed_transaction(database, settings) as (db, identity, directory):
            principal = await check_upload_actor(db, request)
            if actor_snapshot(principal) != actor:
                raise HTTPException(409, "upload_authorization_changed")
            prior = await existing_upload(db, principal, scope, payload_hash)
            if prior is not None:
                return prior
            original = await verify_for_publication(
                db, identity, directory, source_id, owner_id=actor[0], kind="original"
            )
            variant_object = None
            if variant_id is not None:
                variant_object = await verify_for_publication(
                    db, identity, directory, variant_id, owner_id=actor[0], kind="variant"
                )
            if variant_object and variant_object.delete_after > original.delete_after:
                raise HTTPException(409, "upload_expiry_mismatch")
            upload = Upload(
                owner_id=actor[0],
                assignment_id=actor[3],
                purpose="report" if actor[3] else "ticket",
                object_key=original.object_key,
                delete_after=original.delete_after,
                size=original.size,
                content_type="image/jpeg",
            )
            db.add(upload)
            await db.flush()
            variant = ImageVariant(
                upload_id=upload.id,
                object_key=variant_object.object_key if variant_object else None,
                sha256=variant_object.sha256 if variant_object else None,
                pipeline_version=processed.version,
                detector_version=processed.detector_version,
                face_count=processed.face_count,
                status="ready" if variant_object else "blocked",
                error_code=processed.error_code,
                delete_after=original.delete_after,
            )
            db.add(variant)
            await db.flush()
            upload.current_variant_id = variant.id
            if variant_object:
                upload.last_successful_variant_id = variant.id
                variant_object.state = "ready"
            original.state = "ready"
            db.add(
                IdempotencyRecord(
                    scope=scope,
                    payload_hash=payload_hash,
                    response_json=json.dumps({"upload_id": str(upload.id)}),
                )
            )
            await db.flush()
            return await photo_view(db, upload)
    finally:
        await file.close()


@storage_errors
async def managed_reprocess(request, upload_id, masks, *, detect_faces=False):
    """Unexposed reprocessing flow with approval revocation and lineage recheck."""
    from sqlalchemy import select

    from app.protection import get_upload, revoke_approvals
    from app.storage_objects import read_ready

    database, settings = request.app.state.database, request.app.state.settings
    async with managed_transaction(database, settings) as (db, identity, directory):
        principal = await check_upload_actor(db, request)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        actor = actor_snapshot(principal)
        snapshot = (upload.current_variant_id, upload.last_successful_variant_id)
        current = (
            await db.get(ImageVariant, upload.current_variant_id)
            if upload.current_variant_id
            else None
        )
        previous = (
            current
            if current and current.status == "ready"
            else await db.get(ImageVariant, upload.last_successful_variant_id)
            if upload.last_successful_variant_id
            else None
        )
        previous_id = previous.id if previous else None
        previous_sha = previous.sha256 if previous else None
        current_ready = bool(current and current.status == "ready")
        key = previous.object_key if previous else upload.object_key
        source = None
        try:
            if previous and (
                previous.upload_id != upload.id
                or previous.status != "ready"
                or previous.delete_after <= now()
            ):
                raise ValueError("invalid_variant_lineage")
            if previous is None:
                existing = await db.scalar(
                    select(ImageVariant.id)
                    .where(ImageVariant.upload_id == upload.id, ImageVariant.status == "ready")
                    .limit(1)
                )
                if (
                    existing
                    or upload.last_successful_variant_id
                    or (upload.current_variant_id and current is None)
                ):
                    raise ValueError("variant_lineage_unavailable")
            source = await read_ready(
                db,
                identity,
                directory,
                key=key,
                owner_id=upload.owner_id,
                kind="variant" if previous else "original",
            )
            if previous and hashlib.sha256(source).hexdigest() != previous_sha:
                raise ValueError("previous_variant_hash_mismatch")
        except (OSError, ValueError):
            source = None
        await revoke_approvals(db, upload)

    if source is None:
        processed = images.ProcessedImage(
            "blocked", None, None, images.VERSION, 0, "source_unavailable", "unavailable"
        )
    else:
        try:
            options = {"detect_faces": True} if detect_faces else {}
            processed = await run_in_threadpool(images.process_image, source, masks, **options)
        except Exception:
            processed = images.ProcessedImage(
                "blocked", None, None, images.VERSION, 0, "processing_failed", "unavailable"
            )
    usable = (
        processed.status == "ready"
        and processed.data
        and processed.sha256
        and hashlib.sha256(processed.data).hexdigest() == processed.sha256
    )

    async def recheck(db, identity, directory):
        principal = await check_upload_actor(db, request)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        if actor_snapshot(principal) != actor:
            raise HTTPException(409, "upload_authorization_changed")
        if (upload.current_variant_id, upload.last_successful_variant_id) != snapshot:
            raise HTTPException(409, "stale_image_variant")
        try:
            if previous_id:
                latest = await db.get(ImageVariant, previous_id, populate_existing=True)
                if (
                    not latest
                    or latest.upload_id != upload.id
                    or latest.status != "ready"
                    or latest.delete_after <= now()
                    or latest.object_key != key
                    or latest.sha256 != previous_sha
                ):
                    raise ValueError("stale_image_variant")
            if source is not None:
                latest_source = await read_ready(
                    db,
                    identity,
                    directory,
                    key=key,
                    owner_id=upload.owner_id,
                    kind="variant" if previous_id else "original",
                )
                if hashlib.sha256(latest_source).digest() != hashlib.sha256(source).digest():
                    raise ValueError("stale_image_variant")
        except (OSError, ValueError):
            raise HTTPException(409, "stale_image_variant") from None
        return upload

    async with managed_transaction(database, settings) as (db, identity, directory):
        upload = await recheck(db, identity, directory)
        if not usable:
            await revoke_approvals(db, upload)
            variant = ImageVariant(
                upload_id=upload.id,
                object_key=None,
                sha256=None,
                pipeline_version=processed.version,
                detector_version=processed.detector_version,
                face_count=processed.face_count,
                status="blocked",
                error_code=processed.error_code,
                delete_after=upload.delete_after,
            )
            db.add(variant)
            await db.flush()
            if current_ready:
                upload.last_successful_variant_id = snapshot[0]
            upload.current_variant_id = variant.id
            await db.flush()
            return await photo_view(db, upload)
        operation = StorageOperation(
            id=uuid.uuid4(),
            storage_id=identity.storage_id,
            actor_id=actor[0],
            actor_role=actor[1],
            auth_epoch=actor[2],
            assignment_id=actor[3],
            upload_id=upload.id,
            current_variant_id=snapshot[0],
            last_variant_id=snapshot[1],
            previous_variant_id=previous_id,
            previous_sha256=hashlib.sha256(source).hexdigest(),
            kind="reprocess",
            purpose=upload.purpose,
            scope=f"reprocess:{actor[0]}:{actor[3] or 'none'}:{upload.id}",
            key_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            input_hash=hashlib.sha256(source).hexdigest(),
            mask_hash=hashlib.sha256(
                json.dumps(
                    {"masks": masks, "detect_faces": detect_faces},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
            state="active",
            delete_after=min(upload.delete_after, now() + timedelta(days=30)),
        )
        db.add(operation)
        await db.flush()
        object_id = await reserve_locked(
            db,
            identity,
            owner_id=upload.owner_id,
            kind="variant",
            content=processed.data,
            delete_after=operation.delete_after,
            operation_id=operation.id,
        )
        operation.result_data = {"object_ids": [str(object_id)], "variant_sha256": processed.sha256}
        operation_id = operation.id
    await write_pending(database, settings, object_id, processed.data, request=request)
    async with managed_transaction(database, settings) as (db, identity, directory):
        from app.storage_objects import _verify_for_publication
        from app.storage_operations import checked_operation

        operation = await checked_operation(db, identity, directory, request, operation_id)
        if operation.state != "active":
            raise HTTPException(409, "operation_not_active")
        upload = await recheck(db, identity, directory)
        variant_object = await _verify_for_publication(
            db,
            identity,
            directory,
            object_id,
            owner_id=upload.owner_id,
            kind="variant",
            operation_id=operation.id,
        )
        if variant_object.delete_after > upload.delete_after:
            raise HTTPException(409, "upload_expiry_mismatch")
        await revoke_approvals(db, upload)
        variant = ImageVariant(
            upload_id=upload.id,
            object_key=variant_object.object_key,
            sha256=variant_object.sha256,
            pipeline_version=processed.version,
            detector_version=processed.detector_version,
            face_count=processed.face_count,
            status="ready",
            error_code=processed.error_code,
            delete_after=variant_object.delete_after,
        )
        db.add(variant)
        await db.flush()
        if current_ready:
            upload.last_successful_variant_id = snapshot[0]
        upload.current_variant_id = variant.id
        variant_object.state = "ready"
        operation.state = "published"
        operation.published_upload_id = upload.id
        await db.flush()
        return await photo_view(db, upload)
