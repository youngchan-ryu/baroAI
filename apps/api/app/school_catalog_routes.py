"""Dispatcher-managed school catalog previews and atomic activation."""

import hashlib
import json
import os
import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from starlette.concurrency import run_in_threadpool

from app.dispatch_lock import dispatch_lock
from app.dispatch_routes import dispatcher, save_receipt
from app.dispatching import fingerprint
from app.models import (
    Assignment,
    DispatchPlan,
    IdempotencyRecord,
    PlanMembership,
    PlanningDatasetActivation,
    PlanningDatasetActive,
    SchoolCatalogActive,
    SchoolCatalogImport,
    SchoolCatalogVersion,
    SchoolPlanningDataset,
)
from app.planning.datasets import MAX_DB_BUNDLE_BYTES, load_pinned_dataset
from app.planning.registry import next_global_revision
from app.school_catalog_validate import validate
from app.school_catalog_xlsx import MAX_UPLOAD_BYTES, read_workbook, template_bytes

router = APIRouter(prefix="/v1/admin")


class ActivateInput(BaseModel):
    expected_active_revision: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=1000)


class ActivationView(BaseModel):
    version: str | None
    revision: int
    activated_at: str


class Change(BaseModel):
    sheet: str
    key: str
    before: dict | None
    after: dict | None


class Changes(BaseModel):
    added: list[Change]
    changed: list[Change]
    deactivated: list[Change]


class ImportErrorView(BaseModel):
    sheet: str
    row: int
    column: str
    code: str
    message: str


class DatasetPreview(BaseModel):
    present: bool
    version: str | None
    sha256: str | None
    location_count: int
    source: str | None
    source_version: str | None
    changed: bool


class ImportView(BaseModel):
    id: uuid.UUID
    base_revision: int
    valid: bool
    errors: list[ImportErrorView]
    changes: Changes
    dataset_preview: DatasetPreview


class NamedCode(BaseModel):
    code: str
    name: str


class ActiveDatasetView(BaseModel):
    revision: int
    hash: str
    version: str
    source: str
    source_version: str


class CatalogView(BaseModel):
    revision: int
    activated_at: str | None
    departments: list[NamedCode]
    fields: list[NamedCode]
    categories: list[dict]
    locations: list[dict]
    mappings: list[dict]
    dataset: ActiveDatasetView | None


async def active_catalog(db):
    pointer = await db.get(SchoolCatalogActive, 1)
    return (
        (pointer, await db.get(SchoolCatalogVersion, pointer.catalog_revision))
        if pointer
        else (None, None)
    )


def dataset_preview(candidate, prior):
    current = candidate.get("dataset")
    previous = (prior or {}).get("dataset")
    metadata = json.loads(current["canonical"]) if current else None
    return {
        "present": bool(current),
        "version": metadata["version"] if metadata else None,
        "sha256": current["sha256"] if current else None,
        "location_count": len(metadata["locations"]) if metadata else 0,
        "source": metadata["evidence"]["source"] if metadata else None,
        "source_version": metadata["evidence"]["source_version"] if metadata else None,
        "changed": (current or {}).get("sha256") != (previous or {}).get("sha256"),
    }


@router.get("/catalog-template")
async def template(request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
    return Response(
        template_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="school-catalog.xlsx"'},
    )


@router.get("/catalog", response_model=CatalogView)
async def catalog(request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        pointer, active = await active_catalog(db)
        if not active:
            return {
                "revision": 0,
                "activated_at": None,
                "departments": [],
                "fields": [],
                "categories": [],
                "locations": [],
                "mappings": [],
                "dataset": None,
            }
        content = active.content
        meta = content.get("dataset")
        return {
            "revision": active.revision,
            "activated_at": active.created_at.isoformat(),
            **{
                k: content[k]
                for k in ("departments", "fields", "categories", "locations", "mappings")
            },
            "dataset": {
                "revision": pointer.dataset_revision,
                "hash": pointer.dataset_hash,
                "version": meta["version"],
                "source": meta["source"],
                "source_version": meta["source_version"],
            }
            if meta
            else None,
        }


@router.post("/catalog-imports", status_code=201, response_model=ImportView)
async def create_import(request: Request, file: Annotated[UploadFile, File()]):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(422, "invalid_workbook")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    try:
        rows = await run_in_threadpool(read_workbook, content)
    except ValueError:
        raise HTTPException(422, "invalid_workbook") from None
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        pointer, active = await active_catalog(db)
        previous = active.content if active else None
        base_revision = pointer.catalog_revision if pointer else 0
    from app.planning.travel_estimates import TravelEstimateSettings

    settings = request.app.state.settings
    travel_settings = TravelEstimateSettings(
        settings.travel_detour_factor, settings.travel_speed_kmh, settings.travel_entry_minutes
    )
    candidate, errors, changes = await run_in_threadpool(validate, rows, previous, travel_settings)
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        principal = await dispatcher(request, db, require_demo=False)
        pointer, _ = await active_catalog(db)
        if (pointer.catalog_revision if pointer else 0) != base_revision:
            raise HTTPException(409, "catalog_revision_conflict")
        item = SchoolCatalogImport(
            actor_id=principal.user.id,
            base_revision=base_revision,
            valid=not errors,
            errors=errors,
            changes=changes,
            candidate=candidate,
        )
        db.add(item)
        await db.flush()
        return {
            "id": str(item.id),
            "base_revision": item.base_revision,
            "valid": item.valid,
            "errors": errors,
            "changes": changes,
            "dataset_preview": dataset_preview(candidate, previous),
        }


@router.post("/catalog-imports/{import_id}/activate", response_model=ActivationView)
async def activate(import_id: uuid.UUID, payload: ActivateInput, request: Request):
    if not payload.reason.strip():
        raise HTTPException(422, "reason_required")
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        principal = await dispatcher(request, db, require_demo=False)
        key = request.headers.get("Idempotency-Key", "")
        if not 8 <= len(key) <= 100:
            raise HTTPException(422, "idempotency_key_required")
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        scope = f"{principal.user.id}:catalog:{import_id}:{key_hash}"
        hashed = fingerprint(payload.model_dump(mode="json"))
        existing = await db.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.scope == scope)
        )
        if existing and existing.payload_hash != hashed:
            raise HTTPException(409, "idempotency_conflict")
        if existing:
            return ActivationView.model_validate_json(existing.response_json)
        item = await db.get(SchoolCatalogImport, import_id, with_for_update=True)
        if not item:
            raise HTTPException(404, "catalog_import_not_found")
        if not item.valid or item.errors:
            raise HTTPException(422, "catalog_import_invalid")
        pointer, active = await active_catalog(db)
        current_revision = pointer.catalog_revision if pointer else 0
        if (
            item.base_revision != current_revision
            or payload.expected_active_revision != current_revision
        ):
            raise HTTPException(409, "catalog_revision_conflict")
        reserved = await db.scalar(
            select(Assignment.id)
            .where(Assignment.active_plan_id.is_not(None), Assignment.visit_ended_at.is_(None))
            .limit(1)
        )
        incomplete = await db.scalar(
            select(DispatchPlan.id)
            .where(
                DispatchPlan.status == "approved",
                ~exists().where(PlanMembership.plan_id == DispatchPlan.id),
            )
            .limit(1)
        )
        if reserved or incomplete:
            raise HTTPException(409, "planning_dataset_active_reservations")
        settings = request.app.state.settings
        if settings.planning_dataset_provider != "db":
            raise HTTPException(409, "catalog_provider_not_selected")
        namespace = (
            settings.prototype_namespace
            if settings.deployment_mode == "prototype"
            else settings.service_namespace
        )
        new_dataset = item.candidate.get("dataset")
        data_revision, digest, version = None, None, None
        if new_dataset:
            raw = new_dataset["canonical"].encode("utf-8")
            digest = new_dataset["sha256"]
            try:
                data = load_pinned_dataset(raw, digest, max_bytes=MAX_DB_BUNDLE_BYTES)
            except ValueError:
                raise HTTPException(422, "catalog_import_invalid") from None
            data_revision, version = await next_global_revision(db), data.version
            db.add(
                SchoolPlanningDataset(
                    revision=data_revision,
                    canonical_bytes=raw,
                    sha256=digest,
                    dataset_id=data.dataset_id,
                    version=data.version,
                    evidence=data.evidence.model_dump(mode="json"),
                )
            )
            db.add(
                PlanningDatasetActivation(
                    namespace=namespace,
                    revision=data_revision,
                    sha256=digest,
                    dataset_id=data.dataset_id,
                    version=data.version,
                    reason=payload.reason.strip(),
                    operator_uid=os.getuid(),
                )
            )
            await db.flush()
            registry = await db.get(PlanningDatasetActive, namespace, with_for_update=True)
            if registry is None:
                registry = PlanningDatasetActive(namespace=namespace)
                db.add(registry)
            registry.revision, registry.sha256, registry.dataset_id, registry.version = (
                data_revision,
                digest,
                data.dataset_id,
                data.version,
            )
        summary = (
            {
                "sha256": digest,
                "dataset_id": data.dataset_id,
                "version": data.version,
                "source": data.evidence.source,
                "source_version": data.evidence.source_version,
                "location_count": len(data.locations),
            }
            if new_dataset
            else None
        )
        catalog_content = {**item.candidate, "dataset": summary}
        catalog_version = SchoolCatalogVersion(
            revision=current_revision + 1,
            import_id=item.id,
            actor_id=principal.user.id,
            reason=payload.reason.strip(),
            content=catalog_content,
            dataset_revision=data_revision,
            dataset_hash=digest,
        )
        db.add(catalog_version)
        await db.flush()
        if pointer is None:
            pointer = SchoolCatalogActive(id=1, catalog_revision=catalog_version.revision)
            db.add(pointer)
        pointer.catalog_revision, pointer.dataset_revision, pointer.dataset_hash = (
            catalog_version.revision,
            data_revision,
            digest,
        )
        await db.flush()
        result = ActivationView(
            version=version,
            revision=catalog_version.revision,
            activated_at=catalog_version.created_at.isoformat(),
        )
        save_receipt(db, scope, hashed, result)
        return result
