"""DB authority for a service planning dataset generation.

The hash establishes byte identity only. Operational verification of its
contents is outside this module. Callers hold the shared dispatch gate.
"""

import os

from fastapi import HTTPException
from sqlalchemy import exists, func, select

from app.auth_service import ensure_namespace
from app.dispatch_lock import dispatch_lock
from app.models import (
    Assignment,
    DispatchPlan,
    PlanMembership,
    PlanningDatasetActivation,
    PlanningDatasetActive,
    SchoolCatalogActive,
    SchoolPlanningDataset,
)


async def next_global_revision(db):
    """Allocate while holding dispatch_lock until the writing transaction ends."""
    dataset_max = await db.scalar(select(func.max(SchoolPlanningDataset.revision)))
    activation_max = await db.scalar(select(func.max(PlanningDatasetActivation.revision)))
    return max(dataset_max or 0, activation_max or 0) + 1


async def active_generation(db, settings, provider, expected_revision=None):
    """Read under dispatch gate in API transactions; return a fixed public failure."""
    if settings.deployment_mode != "service" or provider is None:
        raise HTTPException(503, "planning_dataset_unavailable")
    active = await db.get(PlanningDatasetActive, settings.service_namespace)
    if (
        active is None
        or active.sha256 != provider.sha256
        or active.dataset_id != provider.dataset.dataset_id
        or active.version != provider.dataset.version
        or (expected_revision is not None and active.revision != expected_revision)
    ):
        raise HTTPException(503, "planning_dataset_unavailable")
    return active.revision


async def active_school_dataset(db, settings):
    """Resolve only the catalog's current DB generation, under the dispatch gate."""
    from app.planning.datasets import MAX_DB_BUNDLE_BYTES, DatasetProvider, load_pinned_dataset

    pointer = await db.get(SchoolCatalogActive, 1)
    if pointer is None or pointer.dataset_revision is None or pointer.dataset_hash is None:
        raise HTTPException(503, "planning_dataset_unavailable")
    stored = await db.get(SchoolPlanningDataset, pointer.dataset_revision)
    namespace = (
        settings.prototype_namespace
        if settings.deployment_mode == "prototype"
        else settings.service_namespace
    )
    active = await db.get(PlanningDatasetActive, namespace)
    if (
        stored is None
        or active is None
        or active.revision != pointer.dataset_revision
        or active.sha256 != pointer.dataset_hash
        or stored.sha256 != pointer.dataset_hash
        or active.dataset_id != stored.dataset_id
        or active.version != stored.version
    ):
        raise HTTPException(503, "planning_dataset_unavailable")
    try:
        dataset = load_pinned_dataset(
            stored.canonical_bytes, stored.sha256, max_bytes=MAX_DB_BUNDLE_BYTES
        )
    except ValueError:
        raise HTTPException(503, "planning_dataset_unavailable") from None
    return DatasetProvider(dataset, stored.sha256), stored.revision


async def activate_dataset(database, settings, provider, *, expected_revision, reason):
    """Activate prevalidated bytes in one operator-invoked transaction."""
    if settings.deployment_mode != "service":
        raise ValueError("service_mode_required")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("invalid_expected_revision")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("invalid_activation_reason")
    async with database.transaction() as db:
        await dispatch_lock(db)
        await ensure_namespace(db, settings)
        active = await db.get(
            PlanningDatasetActive, settings.service_namespace, with_for_update=True
        )
        if (active.revision if active else 0) != expected_revision:
            raise ValueError("planning_dataset_revision_conflict")
        reserved = await db.scalar(
            select(Assignment.id)
            .where(Assignment.active_plan_id.is_not(None), Assignment.visit_ended_at.is_(None))
            .limit(1)
        )
        incomplete_plan = await db.scalar(
            select(DispatchPlan.id)
            .where(
                DispatchPlan.status == "approved",
                ~exists().where(PlanMembership.plan_id == DispatchPlan.id),
            )
            .limit(1)
        )
        if reserved or incomplete_plan:
            raise ValueError("planning_dataset_active_reservations")
        revision = await next_global_revision(db)
        db.add(
            PlanningDatasetActivation(
                namespace=settings.service_namespace,
                revision=revision,
                sha256=provider.sha256,
                dataset_id=provider.dataset.dataset_id,
                version=provider.dataset.version,
                reason=reason.strip(),
                operator_uid=os.getuid(),
            )
        )
        await db.flush()
        if active is None:
            active = PlanningDatasetActive(namespace=settings.service_namespace)
            db.add(active)
        active.revision = revision
        active.sha256 = provider.sha256
        active.dataset_id = provider.dataset.dataset_id
        active.version = provider.dataset.version
        await db.flush()
        return revision
