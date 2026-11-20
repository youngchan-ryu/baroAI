"""Internal first-child-to-second-child proposal storage; public routes stay disabled.

This stage has no service-wide solver concurrency limit. It is not a production route.
"""

import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.dispatch_routes import dispatcher, save_receipt, service_dataset
from app.dispatching import fingerprint, get_plan, plan_view
from app.insertion import capture_insertion, proposal_diff, recheck_service_insertion_calculation
from app.models import DispatchPlan, IdempotencyRecord
from app.planning import fifo, nearest, solve_ortools, urgency_fifo, validate_plan
from app.security import now


async def _proposal_receipt(db, request, principal, parent_id, payload):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 100:
        raise HTTPException(422, "idempotency_key_required")
    scope = f"{principal.user.id}:sip:{parent_id}:{hashlib.sha256(key.encode()).hexdigest()}"
    hashed = fingerprint(payload.model_dump(mode="json"))
    row = await db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope))
    if row and row.payload_hash != hashed:
        raise HTTPException(409, "idempotency_conflict")
    return scope, hashed, row


async def _replay(db, row, parent_id):
    try:
        plan_id = json.loads(row.response_json)["id"]
        from uuid import UUID

        plan = await get_plan(db, UUID(plan_id))
        if plan.parent_plan_id != parent_id or plan.kind != "insertion":
            raise ValueError("wrong_proposal")
        return plan_view(plan)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, "idempotency_conflict") from None


async def create_second_service_proposal(request, parent_id, payload):
    """Store exactly one revalidated proposal per key; return only after commit."""
    settings = request.app.state.settings
    if request.method != "POST" or settings.deployment_mode != "service":
        raise HTTPException(404, "service_insertion_unavailable")
    database = request.app.state.database
    async with database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        scope, hashed, old = await _proposal_receipt(db, request, principal, parent_id, payload)
        if old:
            return await _replay(db, old, parent_id)
        provider, revision = await service_dataset(request, db)
        parent = await get_plan(db, parent_id)
        if parent.kind != "insertion":
            raise HTTPException(409, "service_insertion_parent_invalid")
        stamp = now()
        not_before = stamp + timedelta(minutes=2)
        problem, state, excluded, metadata, base = await capture_insertion(
            db, parent, payload, stamp, not_before, service_context=(settings, provider, revision)
        )
        actor_id, actor_epoch = principal.user.id, principal.user.auth_epoch
    result = await run_in_threadpool(
        {"fifo": fifo, "nearest": nearest, "urgency_fifo": urgency_fifo, "ortools": solve_ortools}[
            payload.policy
        ],
        problem,
    )
    validate_plan(problem, result)
    async with database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        if principal.user.id != actor_id or principal.user.auth_epoch != actor_epoch:
            raise HTTPException(409, "planning_actor_stale")
        scope, hashed, old = await _proposal_receipt(db, request, principal, parent_id, payload)
        if old:
            return await _replay(db, old, parent_id)
        provider, revision = await service_dataset(request, db)
        parent = await get_plan(db, parent_id)
        expires = await recheck_service_insertion_calculation(
            db,
            parent,
            payload,
            stamp,
            not_before,
            problem,
            result,
            state,
            excluded,
            metadata,
            base,
            service_context=(settings, provider, revision),
        )
        metadata["preserved_diff"] = list(metadata["diff"])
        metadata["diff"] = await proposal_diff(db, parent, result, base, metadata)
        plan = DispatchPlan(
            created_by=actor_id,
            parent_plan_id=parent.id,
            kind="insertion",
            insertion_data=metadata,
            captured_at=stamp,
            plan_start_at=base,
            expires_at=expires,
            input_data=problem.model_dump(mode="json"),
            result_data=result.model_dump(mode="json"),
            snapshot_data=state,
            request_data=payload.model_dump(mode="json"),
            excluded=excluded,
            input_hash=result.input_hash,
        )
        db.add(plan)
        await db.flush()
        response = plan_view(plan)
        save_receipt(db, scope, hashed, response)
        await db.flush()
    return response
