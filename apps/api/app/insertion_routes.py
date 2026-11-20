import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.dispatch_routes import dispatcher, planning_context, receipt, save_receipt
from app.dispatch_schemas import DispatchPlanView, InsertionCreate
from app.dispatching import get_plan, plan_view
from app.insertion import at, capture_insertion, proposal_diff
from app.models import DispatchPlan
from app.planning import fifo, nearest, solve_ortools, urgency_fifo, validate_plan
from app.security import now

router = APIRouter(prefix="/v1")


@router.post("/dispatch-plans/{parent_id}/insertions", response_model=DispatchPlanView)
async def create_insertion(parent_id: uuid.UUID, payload: InsertionCreate, request: Request):
    if getattr(request.app.state, "planning_busy", False):
        raise HTTPException(429, "planning_capacity_busy")
    request.app.state.planning_busy = True
    database = request.app.state.database
    try:
        async with database.transaction() as db:
            principal = await dispatcher(request, db)
            if request.app.state.settings.planning_dataset_provider == "db":
                await planning_context(request, db)
            scope, hashed, old = await receipt(
                db, request, principal, f"insertion:{parent_id}", payload
            )
            if old:
                cached = DispatchPlanView.model_validate_json(old.response_json)
                return plan_view(await get_plan(db, cached.id))
            parent = await get_plan(db, parent_id)
            stamp = now()
            not_before = stamp + timedelta(minutes=2)
            problem, snapshot, excluded, metadata, base = await capture_insertion(
                db, parent, payload, stamp, not_before
            )
            actor_id, epoch = principal.user.id, principal.user.auth_epoch
        result = await run_in_threadpool(
            {
                "fifo": fifo,
                "nearest": nearest,
                "urgency_fifo": urgency_fifo,
                "ortools": solve_ortools,
            }[payload.policy],
            problem,
        )
        validate_plan(problem, result)
        async with database.transaction() as db:
            principal = await dispatcher(request, db)
            if principal.user.id != actor_id or principal.user.auth_epoch != epoch:
                raise HTTPException(409, "planning_actor_stale")
            parent = await get_plan(db, parent_id)
            _, current, _, _, _ = await capture_insertion(db, parent, payload, stamp, not_before)
            if snapshot != current:
                raise HTTPException(409, "planning_snapshot_stale")
            expiry = min(
                [stamp + timedelta(minutes=2)]
                + [
                    datetime.fromisoformat(d["expires_at"])
                    for d in metadata["remaining_snapshot"].values()
                ]
                + [at(base, r.departure_min) for r in result.routes if r.visits]
            )
            if now() >= expiry:
                raise HTTPException(409, "planning_snapshot_expired")
            metadata["preserved_diff"] = list(metadata["diff"])
            metadata["diff"] = await proposal_diff(db, parent, result, base, metadata)
            plan = DispatchPlan(
                created_by=actor_id,
                parent_plan_id=parent.id,
                kind="insertion",
                insertion_data=metadata,
                captured_at=stamp,
                plan_start_at=base,
                expires_at=expiry,
                input_data=problem.model_dump(mode="json"),
                result_data=result.model_dump(mode="json"),
                snapshot_data=snapshot,
                request_data=payload.model_dump(mode="json"),
                excluded=excluded,
                input_hash=result.input_hash,
            )
            db.add(plan)
            await db.flush()
            view = plan_view(plan)
            save_receipt(db, scope, hashed, view)
            return view
    finally:
        request.app.state.planning_busy = False
