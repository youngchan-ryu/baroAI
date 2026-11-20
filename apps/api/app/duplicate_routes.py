"""Staff-approved merge mutations and owner-only safe progress subscriptions."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app import duplicates
from app.models import DuplicateDismissal, TicketMerge
from app.routes import _photo_aware_transaction
from app.schemas import (
    DuplicateCandidates,
    DuplicateDismissInput,
    LinkedSafetyReviewInput,
    MergeInput,
    MergeSubscriptionInput,
    TicketView,
    UnmergeInput,
)
from app.security import authenticate, now, role
from app.tickets import get_ticket, idempotency, save_idempotency, ticket_view, transition

router = APIRouter(prefix="/v1")


@router.get("/tickets/{source_id}/duplicates", response_model=DuplicateCandidates)
async def duplicate_candidates(source_id: uuid.UUID, request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        source = await get_ticket(db, source_id, principal)
        return await duplicates.candidates(db, source)


@router.post("/tickets/{source_id}/merge", response_model=TicketView)
async def merge_ticket(source_id: uuid.UUID, payload: MergeInput, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        source, target, assignments, reports, links = await duplicates.lock_pair(
            db, source_id, payload.target_id
        )
        scope, digest, prior = await idempotency(
            db, request, f"merge:{source_id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, source, principal)
        await duplicates.merge(db, source, target, assignments, reports, links, principal, payload)
        await db.flush()
        result = await ticket_view(db, source, principal)
        save_idempotency(db, scope, digest, result)
        return result


@router.post("/tickets/{source_id}/unmerge", response_model=TicketView)
async def unmerge_ticket(source_id: uuid.UUID, payload: UnmergeInput, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        latest = await db.scalar(
            select(TicketMerge)
            .where(TicketMerge.source_id == source_id)
            .order_by(TicketMerge.created_at.desc(), TicketMerge.id.desc())
            .limit(1)
        )
        if not latest:
            raise HTTPException(409, "merge_not_active")
        source, target, _, _, links = await duplicates.lock_pair(db, source_id, latest.target_id)
        scope, digest, prior = await idempotency(
            db, request, f"unmerge:{source_id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, source, principal)
        await duplicates.unmerge(db, source, target, links, principal, payload)
        await db.flush()
        result = await ticket_view(db, source, principal)
        save_idempotency(db, scope, digest, result)
        return result


@router.post("/tickets/{source_id}/duplicate-dismiss", response_model=TicketView)
async def dismiss_duplicate(source_id: uuid.UUID, payload: DuplicateDismissInput, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        source, target, _, _, _ = await duplicates.lock_pair(db, source_id, payload.target_id)
        scope, digest, prior = await idempotency(
            db, request, f"dismiss:{source_id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, source, principal)
        await duplicates.check_versions(db, source, target, payload)
        if not duplicates.matching(source, target, now()):
            raise HTTPException(409, "merge_candidate_changed")
        exists = await db.scalar(
            select(DuplicateDismissal.id).where(
                DuplicateDismissal.source_id == source.id,
                DuplicateDismissal.target_id == target.id,
                DuplicateDismissal.source_version == payload.expected_source_version,
                DuplicateDismissal.target_version == payload.expected_target_version,
            )
        )
        if not exists:
            db.add(
                DuplicateDismissal(
                    source_id=source.id,
                    target_id=target.id,
                    source_version=payload.expected_source_version,
                    target_version=payload.expected_target_version,
                    actor_id=principal.user.id,
                    reason=payload.reason,
                )
            )
            duplicates.event(db, source, principal, "duplicate_dismissed", payload.reason)
        await db.flush()
        result = await ticket_view(db, source, principal)
        save_idempotency(db, scope, digest, result)
        return result


@router.post("/tickets/{source_id}/merge-subscription", response_model=TicketView)
async def merge_subscription(
    source_id: uuid.UUID, payload: MergeSubscriptionInput, request: Request
):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "reporter")
        source = await get_ticket(db, source_id, principal, lock=True)
        scope, digest, prior = await idempotency(
            db, request, f"subscription:{source_id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, source, principal)
        link = await db.scalar(
            select(TicketMerge)
            .where(TicketMerge.source_id == source_id, TicketMerge.active.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not link or source.status != "merged":
            raise HTTPException(409, "merge_not_active")
        if link.revision != payload.expected_merge_revision:
            raise HTTPException(409, "merge_revision_conflict")
        link.subscribed, link.subscribed_at = (
            payload.subscribed,
            now() if payload.subscribed else None,
        )
        link.revision += 1
        duplicates.event(
            db,
            source,
            principal,
            "merge_subscription_updated",
            "연결된 처리 소식 구독 신청" if payload.subscribed else "연결된 처리 소식 구독 해제",
        )
        await db.flush()
        result = await ticket_view(db, source, principal)
        save_idempotency(db, scope, digest, result)
        return result


@router.post("/tickets/{target_id}/linked-safety-review", response_model=TicketView)
async def linked_safety_review(
    target_id: uuid.UUID, payload: LinkedSafetyReviewInput, request: Request
):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        target = await get_ticket(db, target_id, principal, lock=True)
        scope, digest, prior = await idempotency(
            db, request, f"linked-safety:{target_id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, target, principal)
        if target.status == "merged":
            raise HTTPException(409, "merged_source_locked")
        links = list(
            (
                await db.scalars(
                    select(TicketMerge)
                    .where(TicketMerge.target_id == target_id)
                    .order_by(TicketMerge.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if await duplicates.merge_version(db, target) != payload.expected_merge_version:
            raise HTTPException(409, "merge_version_conflict")
        target.linked_safety_floor = min(
            (m.safety_snapshot.get("floor", 3) for m in links if m.active), default=3
        )
        target.linked_safety_requires_review = False
        target.safety_version += 1
        transition(db, target, principal.user, "merge_safety_reviewed", payload.reason)
        await db.flush()
        result = await ticket_view(db, target, principal)
        save_idempotency(db, scope, digest, result)
        return result
