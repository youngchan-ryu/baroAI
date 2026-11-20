"""Unexposed recovery intents; no file adoption, writing, publication or deletion."""

import hashlib
import json
import re
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.managed_access import access_transaction
from app.managed_photo import actor_snapshot, check_upload_actor
from app.models import ImageVariant, StorageIdentity, StorageOperation, Upload
from app.protection import get_upload
from app.security import now
from app.storage import MAX_FILE
from app.storage_objects import read_ready


def digest(data):
    return hashlib.sha256(data).hexdigest()


def operation_view(operation):
    return {"id": operation.id, "state": operation.state, "delete_after": operation.delete_after}


async def lineage(db, identity, directory, principal, upload_id, *, read_source=True):
    upload = await get_upload(db, upload_id, principal, owner_only=True)
    upload = await db.scalar(
        select(Upload)
        .where(Upload.id == upload.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = (
        await db.get(ImageVariant, upload.current_variant_id) if upload.current_variant_id else None
    )
    if upload.current_variant_id and (not current or current.upload_id != upload.id):
        raise HTTPException(409, "stale_image_variant")
    previous_id = (
        current.id if current and current.status == "ready" else upload.last_successful_variant_id
    )
    previous = await db.get(ImageVariant, previous_id) if previous_id else None
    if previous_id and previous is None:
        raise HTTPException(409, "stale_image_variant")
    if previous is None:
        existing_ready = await db.scalar(
            select(ImageVariant.id)
            .where(ImageVariant.upload_id == upload.id, ImageVariant.status == "ready")
            .limit(1)
        )
        if existing_ready or upload.last_successful_variant_id:
            raise HTTPException(409, "stale_image_variant")
    if previous and (
        previous.upload_id != upload.id
        or previous.status != "ready"
        or previous.delete_after <= now()
    ):
        raise HTTPException(409, "stale_image_variant")
    if not read_source:
        return upload, previous, None
    try:
        source = await read_ready(
            db,
            identity,
            directory,
            key=previous.object_key if previous else upload.object_key,
            owner_id=upload.owner_id,
            kind="variant" if previous else "original",
        )
    except (OSError, ValueError):
        raise HTTPException(409, "image_not_ready") from None
    if previous and digest(source) != previous.sha256:
        raise HTTPException(409, "stale_image_variant")
    return upload, previous, digest(source)


async def checked_operation(db, identity, directory, request, operation_id):
    principal = await check_upload_actor(db, request)
    operation = await db.get(StorageOperation, operation_id)
    actor = actor_snapshot(principal)
    if (
        not operation
        or operation.storage_id != identity.storage_id
        or operation.actor_id != actor[0]
    ):
        raise HTTPException(404, "operation_not_found")
    if (operation.actor_role, operation.auth_epoch, operation.assignment_id) != actor[1:]:
        raise HTTPException(409, "upload_authorization_changed")
    if operation.delete_after <= now():
        raise HTTPException(410, "operation_expired")
    if operation.kind == "reprocess":
        upload, previous, _ = await lineage(
            db, identity, directory, principal, operation.upload_id, read_source=False
        )
        if (
            upload.current_variant_id != operation.current_variant_id
            or upload.last_successful_variant_id != operation.last_variant_id
            or (previous.id if previous else None) != operation.previous_variant_id
            or upload.delete_after < operation.delete_after
            or upload.purpose != operation.purpose
        ):
            raise HTTPException(409, "stale_image_variant")
    operation = await db.scalar(
        select(StorageOperation)
        .where(StorageOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not operation
        or operation.storage_id != identity.storage_id
        or operation.actor_id != actor[0]
    ):
        raise HTTPException(404, "operation_not_found")
    if (operation.actor_role, operation.auth_epoch, operation.assignment_id) != actor[1:]:
        raise HTTPException(409, "upload_authorization_changed")
    if operation.delete_after <= now():
        raise HTTPException(410, "operation_expired")
    if operation.kind == "reprocess":
        try:
            source = await read_ready(
                db,
                identity,
                directory,
                key=previous.object_key if previous else upload.object_key,
                owner_id=upload.owner_id,
                kind="variant" if previous else "original",
            )
        except (OSError, ValueError):
            raise HTTPException(409, "image_not_ready") from None
        if digest(source) != operation.previous_sha256:
            raise HTTPException(409, "stale_image_variant")
    return operation


def upload_input_hash(raw, request):
    # Keep consent in the idempotency contract without storing the coordinates.
    if request.headers.get("x-baro-photo-coordinates") == "consent":
        return digest(b"baro-photo-coordinates-consent\0" + raw)
    # Preserve old hashes for all pre-existing upload operations.
    return digest(b"baro-photo-location-intake\0" + raw) if (
        request.headers.get("x-baro-photo-location") == "intake"
    ) else digest(raw)


async def begin_operation(request, *, kind, source_bytes=None, masks=None, upload_id=None):
    """Commit authenticated intent before calculating output; never touches files for writing."""
    if kind not in {"upload", "reprocess"}:
        raise HTTPException(422, "invalid_operation")
    key = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
        raise HTTPException(422, "idempotency_key_required")
    if kind == "upload" and (
        not isinstance(source_bytes, bytes)
        or not 0 < len(source_bytes) <= MAX_FILE
        or upload_id is not None
        or masks
    ):
        raise HTTPException(422, "invalid_operation")
    if kind == "reprocess" and (upload_id is None or source_bytes is not None):
        raise HTTPException(422, "invalid_operation")
    mask_bytes = json.dumps(
        masks or [], sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        principal = await check_upload_actor(db, request)
        actor = actor_snapshot(principal)
        upload = previous = None
        source_hash = upload_input_hash(source_bytes, request) if source_bytes is not None else None
        scope = f"{kind}:{actor[0]}:{actor[3] or 'none'}:{upload_id or 'none'}"
        key_hash, mask_hash = digest(key.encode()), digest(mask_bytes)
        operation = await db.scalar(
            select(StorageOperation)
            .where(StorageOperation.scope == scope, StorageOperation.key_hash == key_hash)
            .execution_options(populate_existing=True)
        )
        if operation:
            operation = await checked_operation(db, identity, directory, request, operation.id)
            if (
                kind == "upload" and operation.input_hash != source_hash
            ) or operation.mask_hash != mask_hash:
                raise HTTPException(409, "idempotency_conflict")
            if operation.state == "abandoned":
                raise HTTPException(409, "operation_abandoned")
            return operation_view(operation)
        if kind == "reprocess":
            upload, previous, source_hash = await lineage(
                db, identity, directory, principal, upload_id
            )
        expiry = (
            min(now() + timedelta(days=30), upload.delete_after)
            if upload
            else now() + timedelta(days=30)
        )
        operation = StorageOperation(
            storage_id=identity.storage_id,
            actor_id=actor[0],
            actor_role=actor[1],
            auth_epoch=actor[2],
            assignment_id=actor[3],
            upload_id=upload.id if upload else None,
            current_variant_id=upload.current_variant_id if upload else None,
            last_variant_id=upload.last_successful_variant_id if upload else None,
            previous_variant_id=previous.id if previous else None,
            previous_sha256=source_hash if upload else None,
            kind=kind,
            purpose=upload.purpose if upload else ("report" if actor[3] else "ticket"),
            scope=scope,
            key_hash=key_hash,
            input_hash=source_hash,
            mask_hash=mask_hash,
            state="active",
            delete_after=expiry,
        )
        db.add(operation)
        await db.flush()
        return operation_view(operation)


async def _lock_abandon_operation(db, operation_id):
    return await db.scalar(
        select(StorageOperation)
        .where(StorageOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def abandon_operation(request, operation_id):
    """Withdraw authorization via DB locks even when the disk or TTL is unavailable."""
    from app.dispatch_lock import dispatch_lock
    from app.storage_provision import storage_lock

    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        await storage_lock(db)
        identity = await db.get(StorageIdentity, 1)
        settings = request.app.state.settings
        from app.auth_service import namespace_for

        namespace = namespace_for(settings)
        if (
            not identity
            or identity.state != "active"
            or (identity.mode, identity.environment, identity.namespace)
            != (settings.deployment_mode, settings.app_env, namespace)
        ):
            raise HTTPException(409, "managed_storage_not_available")
        principal = await check_upload_actor(db, request)
        actor = actor_snapshot(principal)
        operation = await _lock_abandon_operation(db, operation_id)
        if (
            not operation
            or operation.storage_id != identity.storage_id
            or operation.actor_id != actor[0]
        ):
            raise HTTPException(404, "operation_not_found")
        if (operation.actor_role, operation.auth_epoch, operation.assignment_id) != actor[1:]:
            raise HTTPException(409, "upload_authorization_changed")
        if operation.state == "published":
            raise HTTPException(409, "operation_published")
        if operation.state == "active":
            operation.state = "abandoned"
            await db.flush()
        return operation_view(operation)


async def read_operation(request, operation_id, *, abandon=False):
    """Reauthenticate and optionally abandon without modifying the filesystem."""
    if abandon:
        return await abandon_operation(request, operation_id)
    async with access_transaction(request.app.state.database, request.app.state.settings) as (
        db,
        identity,
        directory,
    ):
        operation = await checked_operation(db, identity, directory, request, operation_id)
        return operation_view(operation)


async def freeze_upload_result(request, operation_id, raw):
    """Internal first-stage upload result; processing occurs outside shared locks."""
    from starlette.concurrency import run_in_threadpool

    from app.services import images
    from app.storage import normalize_image
    from app.storage_objects import reserve_locked

    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FILE:
        raise HTTPException(422, "invalid_operation")
    database, settings = request.app.state.database, request.app.state.settings
    async with access_transaction(database, settings) as (db, identity, directory):
        operation = await checked_operation(db, identity, directory, request, operation_id)
        if operation.kind != "upload" or operation.state != "active":
            raise HTTPException(409, "operation_not_active")
        if operation.input_hash != upload_input_hash(raw, request):
            raise HTTPException(409, "idempotency_conflict")
    normalized = await run_in_threadpool(normalize_image, raw)
    try:
        processed = await run_in_threadpool(images.process_image, normalized, [])
    except Exception:
        processed = images.ProcessedImage(
            "blocked", None, None, images.VERSION, 0, "processing_failed", "unavailable"
        )
    usable = bool(
        processed.status == "ready"
        and processed.data
        and processed.sha256
        and digest(processed.data) == processed.sha256
    )
    result = {
        "source_sha256": digest(normalized),
        "variant_sha256": digest(processed.data) if usable else None,
        "status": "ready" if usable else "blocked",
        "pipeline_version": processed.version,
        "detector_version": processed.detector_version,
        "face_count": processed.face_count,
        "error_code": processed.error_code,
    }
    async with access_transaction(database, settings) as (db, identity, directory):
        operation = await checked_operation(db, identity, directory, request, operation_id)
        if operation.kind != "upload" or operation.state != "active":
            raise HTTPException(409, "operation_not_active")
        if operation.input_hash != upload_input_hash(raw, request):
            raise HTTPException(409, "idempotency_conflict")
        if request.headers.get("x-baro-photo-location") == "intake" and not operation.assignment_id:
            from app.location_workflow import photo_location_signal

            signal = await photo_location_signal(db, raw)
            if signal:
                result["location_signal"] = signal
        if operation.result_data:
            recorded = dict(operation.result_data)
            object_ids = recorded.pop("object_ids", [])
            if recorded != result:
                raise HTTPException(409, "operation_result_changed")
        else:
            source_id = await reserve_locked(
                db,
                identity,
                owner_id=operation.actor_id,
                kind="original",
                content=normalized,
                delete_after=operation.delete_after,
                operation_id=operation.id,
            )
            object_ids = [str(source_id)]
            if usable:
                variant_id = await reserve_locked(
                    db,
                    identity,
                    owner_id=operation.actor_id,
                    kind="variant",
                    content=processed.data,
                    delete_after=operation.delete_after,
                    operation_id=operation.id,
                )
                object_ids.append(str(variant_id))
            operation.result_data = {**result, "object_ids": object_ids}
            await db.flush()
        return {
            "object_ids": object_ids,
            "source": normalized,
            "variant": processed.data if usable else None,
        }


async def publish_upload(request, operation_id, *, coordinates=None):
    """Publish a fully written operation atomically; never processes or rewrites bytes."""
    import uuid

    from app.protection import photo_view
    from app.storage_objects import _verify_for_publication

    database, settings = request.app.state.database, request.app.state.settings
    async with access_transaction(database, settings) as (db, identity, directory):
        operation = await checked_operation(db, identity, directory, request, operation_id)
        if operation.kind != "upload":
            raise HTTPException(409, "operation_kind_mismatch")
        if operation.state == "abandoned":
            raise HTTPException(409, "operation_abandoned")
        if operation.state == "published":
            if not operation.published_upload_id:
                raise HTTPException(409, "operation_result_invalid")
            principal = await check_upload_actor(db, request)
            upload = await get_upload(db, operation.published_upload_id, principal, owner_only=True)
            return await photo_view(db, upload)
        data = operation.result_data
        if not data or data.get("status") not in {"ready", "blocked"}:
            raise HTTPException(409, "operation_result_invalid")
        ids = data.get("object_ids")
        if not isinstance(ids, list) or len(ids) != (2 if data["status"] == "ready" else 1):
            raise HTTPException(409, "operation_result_invalid")
        try:
            object_ids = [uuid.UUID(value) for value in ids]
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(409, "operation_result_invalid") from None
        if len(set(object_ids)) != len(object_ids):
            raise HTTPException(409, "operation_result_invalid")
        try:
            original = await _verify_for_publication(
                db,
                identity,
                directory,
                object_ids[0],
                owner_id=operation.actor_id,
                kind="original",
                operation_id=operation.id,
            )
            variant_object = None
            if len(object_ids) == 2:
                variant_object = await _verify_for_publication(
                    db,
                    identity,
                    directory,
                    object_ids[1],
                    owner_id=operation.actor_id,
                    kind="variant",
                    operation_id=operation.id,
                )
        except (OSError, ValueError):
            raise HTTPException(409, "image_not_ready") from None
        if (
            original.sha256 != data.get("source_sha256")
            or original.delete_after > operation.delete_after
            or (
                variant_object
                and (
                    variant_object.sha256 != data.get("variant_sha256")
                    or variant_object.delete_after > original.delete_after
                )
            )
            or (not variant_object and data.get("variant_sha256") is not None)
        ):
            raise HTTPException(409, "operation_result_invalid")
        upload = Upload(
            location_signal=data.get("location_signal"),
            photo_coordinates=coordinates,
            owner_id=operation.actor_id,
            assignment_id=operation.assignment_id,
            purpose=operation.purpose,
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
            pipeline_version=data["pipeline_version"],
            detector_version=data["detector_version"],
            face_count=data["face_count"],
            status=data["status"],
            error_code=data["error_code"],
            delete_after=variant_object.delete_after if variant_object else original.delete_after,
        )
        db.add(variant)
        await db.flush()
        upload.current_variant_id = variant.id
        if variant_object:
            upload.last_successful_variant_id = variant.id
            variant_object.state = "ready"
        original.state = "ready"
        operation.published_upload_id = upload.id
        operation.state = "published"
        await db.flush()
        return await photo_view(db, upload)
