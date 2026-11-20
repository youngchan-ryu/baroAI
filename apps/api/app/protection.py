import hashlib
import math
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import exists, select, text
from starlette.concurrency import run_in_threadpool

from app.models import (
    Assignment,
    ImageApproval,
    ImageVariant,
    StorageIdentity,
    StorageObject,
    Upload,
)
from app.schemas import PhotoView, PrivacyView
from app.security import locked_assignment, now
from app.services import images


def expected_purpose(upload):
    return "completion_analysis" if upload.assignment_id else "ticket_analysis"


async def photo_view(db, upload):
    variant = (
        await db.get(ImageVariant, upload.current_variant_id) if upload.current_variant_id else None
    )
    approval = await db.scalar(
        select(ImageApproval)
        .where(
            ImageApproval.upload_id == upload.id,
            ImageApproval.variant_id == upload.current_variant_id,
        )
        .order_by(
            ImageApproval.revoked_at.is_(None).desc(),
            ImageApproval.created_at.desc(),
            ImageApproval.id.desc(),
        )
        .limit(1)
    )
    provider = db.info.get("llm_provider", "openai")
    purpose = expected_purpose(upload)
    status = "pending"
    if upload.deleted or upload.delete_after <= now():
        status = "deleted"
    elif variant and variant.delete_after <= now():
        status = "deleted"
    elif variant:
        status = "processed" if variant.status == "ready" else "failed"
        if approval and variant.status == "ready":
            status = (
                "revoked"
                if (
                    approval.revoked_at
                    or approval.provider != provider
                    or approval.purpose != purpose
                    or approval.sha256 != variant.sha256
                    or approval.pipeline_version != variant.pipeline_version
                )
                else "approved"
            )
    labels = []
    details = []
    signal = upload.location_signal
    if signal and not upload.deleted and upload.delete_after > now() and not upload.assignment_id:
        from app.models import SchoolCatalogActive, SchoolCatalogVersion

        pointer = await db.get(SchoolCatalogActive, 1)
        catalog = (await db.get(SchoolCatalogVersion, pointer.catalog_revision)
                   if pointer and signal.get("catalog_revision") == pointer.catalog_revision
                   else None)
        if catalog:
            locations = {item["code"]: item for item in catalog.content.get("locations", [])}
            for code in signal.get("codes", []):
                if code not in locations:
                    continue
                item = locations[code]
                labels.append(item["name"])
                lat, lon = item.get("latitude"), item.get("longitude")
                valid = (item.get("coordinate_system") == "WGS84"
                         and isinstance(lat, (int, float)) and not isinstance(lat, bool)
                         and isinstance(lon, (int, float)) and not isinstance(lon, bool)
                         and math.isfinite(lat) and math.isfinite(lon)
                         and -90 <= lat <= 90 and -180 <= lon <= 180)
                details.append({"code": code, "label": item["name"],
                                "latitude": lat if valid else None,
                                "longitude": lon if valid else None})
    return PhotoView(
        location_candidates=labels,
        location_candidate_details=details,
        id=upload.id,
        url=f"/api/v1/uploads/{upload.id}",
        privacy=PrivacyView(
            status=status,
            variant_id=variant.id if variant else None,
            sha256=variant.sha256 if variant else None,
            pipeline_version=variant.pipeline_version if variant else None,
            detector_version=variant.detector_version if variant else None,
            preview_url=f"/api/v1/uploads/{upload.id}/variants/{variant.id}"
            if variant and variant.status == "ready" and status != "deleted"
            else None,
            face_count=variant.face_count if variant else None,
            provider=provider,
            purpose=purpose,
            approved_at=approval.created_at if approval and status == "approved" else None,
            delete_after=upload.delete_after,
        ),
    )


async def revoke_approvals(db, upload):
    approvals = (
        await db.scalars(
            select(ImageApproval).where(
                ImageApproval.upload_id == upload.id, ImageApproval.revoked_at.is_(None)
            )
        )
    ).all()
    for approval in approvals:
        approval.revoked_at = now()


async def managed_db_present(db):
    if await db.scalar(select(text("to_regclass('storage_identity')"))):
        state = await db.scalar(select(StorageIdentity.state).where(StorageIdentity.id == 1))
        if state == "active":
            return True
    if await db.scalar(select(text("to_regclass('storage_objects')"))):
        if await db.scalar(select(exists().where(StorageObject.id.is_not(None)))):
            return True
    return False


async def require_legacy_storage(db, storage, *, allow_new_storage=False):
    if await managed_db_present(db):
        raise HTTPException(409, "managed_storage_not_available")
    try:
        await run_in_threadpool(storage.ensure_legacy)
    except FileNotFoundError:
        if not allow_new_storage or await db.scalar(select(exists().where(Upload.id.is_not(None)))):
            raise HTTPException(409, "photo_storage_unavailable") from None
    except PermissionError:
        raise HTTPException(409, "managed_storage_not_available") from None
    except OSError:
        raise HTTPException(503, "photo_storage_unavailable") from None


async def process_upload(db, upload, storage, masks, *, detect_faces=False):
    await require_legacy_storage(db, storage)
    await revoke_approvals(db, upload)
    try:
        current = (
            await db.get(ImageVariant, upload.current_variant_id)
            if upload.current_variant_id
            else None
        )
        previous = (
            current
            if current and current.status == "ready"
            else (
                await db.get(ImageVariant, upload.last_successful_variant_id)
                if upload.last_successful_variant_id
                else None
            )
        )
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
                # Historical failed rows have no provable lineage; never unmask by guessing.
                raise ValueError("variant_lineage_unavailable")
        if current and current.status == "ready":
            upload.last_successful_variant_id = current.id
        key = previous.object_key if previous else upload.object_key
        source = await run_in_threadpool(storage.read, key)
        if previous and hashlib.sha256(source).hexdigest() != previous.sha256:
            raise ValueError("previous_variant_hash_mismatch")
        options = {"detect_faces": True} if detect_faces else {}
        processed = await run_in_threadpool(images.process_image, source, masks, **options)
    except PermissionError:
        raise HTTPException(409, "managed_storage_not_available") from None
    except Exception:
        processed = images.ProcessedImage(
            "blocked", None, None, images.VERSION, 0, "source_unavailable", "unavailable"
        )
    key = None
    if (
        processed.status == "ready"
        and processed.data
        and processed.sha256
        and hashlib.sha256(processed.data).hexdigest() == processed.sha256
    ):
        import uuid

        key = f"{uuid.uuid4().hex}.jpg"
        try:
            await run_in_threadpool(storage.put, key, processed.data)
        except OSError:
            raise HTTPException(503, "photo_storage_unavailable") from None
    variant = ImageVariant(
        upload_id=upload.id,
        object_key=key,
        sha256=processed.sha256 if key else None,
        pipeline_version=processed.version,
        detector_version=processed.detector_version,
        face_count=processed.face_count,
        status="ready" if key else "blocked",
        error_code=processed.error_code,
        delete_after=min(upload.delete_after, now() + timedelta(days=30)),
    )
    db.add(variant)
    await db.flush()
    upload.current_variant_id = variant.id
    if variant.status == "ready":
        upload.last_successful_variant_id = variant.id
    await db.flush()
    return variant


async def get_upload(db, upload_id, principal, *, owner_only=False):
    # Same lock order as ticket writes. Recheck worker revocation after ticket lock.
    from app.tickets import get_ticket

    record = await db.get(Upload, upload_id)
    if not record:
        raise HTTPException(404, "upload_not_found")
    if principal.assignment:
        await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        assignment = await locked_assignment(db, principal, principal.assignment.id)
        allowed = (
            record.ticket_id == assignment.ticket_id
            and record.assignment_id in (None, assignment.id)
        ) or (record.owner_id == principal.user.id and record.assignment_id == assignment.id)
    else:
        allowed = record.owner_id == principal.user.id
        if record.ticket_id:
            ticket = await get_ticket(db, record.ticket_id, principal, lock=True)
            allowed = (
                allowed
                or principal.user.role == "dispatcher"
                or ticket.reporter_id == principal.user.id
            )
    record = await db.scalar(
        select(Upload)
        .where(Upload.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if record.information_request_id and (principal.assignment or principal.user.role == "worker"):
        allowed = False
    if record.report_id and principal.user.role == "reporter":
        from app.models import CompletionReport

        report = await db.get(CompletionReport, record.report_id, populate_existing=True)
        if not report or not report.reviewed or not report.approved_snapshot:
            allowed = False
    if not allowed or (owner_only and record.owner_id != principal.user.id):
        raise HTTPException(404, "upload_not_found")
    if record.deleted or record.delete_after <= now():
        raise HTTPException(410, "image_deleted")
    return record


async def approved_variant(
    db, upload_id, principal, *, ticket_id, report_id=None, provider, purpose, storage
):
    """Return only the exact currently approved bytes, never a source/path fallback."""
    upload = await get_upload(db, upload_id, principal, owner_only=True)
    await require_legacy_storage(db, storage)
    if (
        upload.ticket_id != ticket_id
        or upload.report_id != report_id
        or expected_purpose(upload) != purpose
    ):
        raise HTTPException(409, "image_scope_mismatch")
    if upload.assignment_id:
        assignment = await db.get(Assignment, upload.assignment_id, populate_existing=True)
        if (
            not principal.assignment
            or principal.assignment.id != assignment.id
            or assignment.revoked
            or (principal.session is None and assignment.expires_at <= now())
        ):
            raise HTTPException(409, "image_scope_mismatch")
    variant = (
        await db.get(ImageVariant, upload.current_variant_id) if upload.current_variant_id else None
    )
    if (
        not variant
        or variant.status != "ready"
        or not variant.object_key
        or variant.delete_after <= now()
    ):
        raise HTTPException(409, "image_not_ready")
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
    try:
        data = await run_in_threadpool(storage.read, variant.object_key)
    except OSError:
        raise HTTPException(409, "image_not_ready") from None
    if hashlib.sha256(data).hexdigest() != variant.sha256:
        raise HTTPException(409, "image_hash_mismatch")
    return data
