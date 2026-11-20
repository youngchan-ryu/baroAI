"""Existing workers' field and department profiles."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.dispatch_lock import dispatch_lock
from app.dispatch_routes import dispatcher
from app.models import (
    Assignment,
    SchoolCatalogActive,
    SchoolCatalogVersion,
    WorkerProfile,
    WorkerProfileChange,
    WorkerSchedule,
)
from app.staff_codes import eligible_workers, staff_codes

router = APIRouter(prefix="/v1/admin/workers")


class ProfileInput(BaseModel):
    field_codes: Annotated[list[str], Field(max_length=1000)]
    department_code: str | None
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    expected_catalog_revision: Annotated[int, Field(strict=True, ge=0)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class ProfileView(BaseModel):
    worker_id: uuid.UUID
    display_name: str
    staff_code: str
    field_codes: list[str]
    department_code: str | None
    revision: int
    catalog_revision: int


def view(worker, profile, staff_code):
    return {
        "worker_id": str(worker.id),
        "display_name": worker.display_name,
        "staff_code": staff_code,
        "field_codes": profile.field_codes if profile else [],
        "department_code": profile.department_code if profile else None,
        "revision": profile.revision if profile else 0,
        "catalog_revision": profile.catalog_revision if profile else 0,
    }


@router.get("", response_model=list[ProfileView])
async def workers(request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        items = await eligible_workers(db, request.app.state.settings)
        codes = staff_codes(items)
        return [
            view(worker, await db.get(WorkerProfile, worker.id), codes[worker.id])
            for worker in items
        ]


@router.patch("/{worker_id}/profile", response_model=ProfileView)
async def update_profile(worker_id: uuid.UUID, payload: ProfileInput, request: Request):
    if not payload.reason.strip() or len(set(payload.field_codes)) != len(payload.field_codes):
        raise HTTPException(422, "invalid_profile")
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        principal = await dispatcher(request, db, require_demo=False)
        items = await eligible_workers(db, request.app.state.settings)
        worker = next((item for item in items if item.id == worker_id), None)
        if not worker:
            raise HTTPException(422, "invalid_worker")
        pointer = await db.get(SchoolCatalogActive, 1)
        if not pointer or pointer.catalog_revision != payload.expected_catalog_revision:
            raise HTTPException(409, "catalog_revision_conflict")
        catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision)
        if not catalog:
            raise HTTPException(409, "catalog_revision_conflict")
        fields = {item["code"] for item in catalog.content["fields"]}
        departments = {item["code"] for item in catalog.content["departments"]}
        if not set(payload.field_codes) <= fields or (
            payload.department_code is not None and payload.department_code not in departments
        ):
            raise HTTPException(422, "unregistered_profile_code")
        profile = await db.get(WorkerProfile, worker_id, with_for_update=True)
        if payload.expected_revision != (profile.revision if profile else 0):
            raise HTTPException(409, "profile_revision_conflict")
        reserved = await db.scalar(
            select(Assignment.id)
            .where(
                Assignment.worker_id == worker_id,
                Assignment.active_plan_id.is_not(None),
                Assignment.visit_ended_at.is_(None),
            )
            .limit(1)
        )
        if reserved:
            raise HTTPException(409, "worker_has_plan_reservations")
        schedule = await db.get(WorkerSchedule, worker_id, with_for_update=True)
        if (
            schedule
            and schedule.data.get("enabled")
            and not set(schedule.data.get("skills", [])) <= set(payload.field_codes)
        ):
            raise HTTPException(409, "profile_invalidates_schedule")
        before = (
            {
                "field_codes": profile.field_codes,
                "department_code": profile.department_code,
                "revision": profile.revision,
                "catalog_revision": profile.catalog_revision,
            }
            if profile
            else {"field_codes": [], "department_code": None, "revision": 0, "catalog_revision": 0}
        )
        if profile is None:
            profile = WorkerProfile(
                worker_id=worker_id,
                revision=0,
                field_codes=[],
                catalog_revision=pointer.catalog_revision,
            )
            db.add(profile)
        profile.revision += 1
        profile.field_codes = sorted(payload.field_codes)
        profile.department_code = payload.department_code
        profile.catalog_revision = pointer.catalog_revision
        after = {
            "field_codes": profile.field_codes,
            "department_code": profile.department_code,
            "revision": profile.revision,
            "catalog_revision": profile.catalog_revision,
        }
        db.add(
            WorkerProfileChange(
                worker_id=worker_id,
                actor_id=principal.user.id,
                reason=payload.reason.strip(),
                before=before,
                after=after,
            )
        )
        await db.flush()
        return view(worker, profile, staff_codes(items)[worker_id])
