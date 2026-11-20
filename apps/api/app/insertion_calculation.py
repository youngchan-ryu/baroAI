"""Internal authenticated service insertion calculation, not a product endpoint.

No plan is stored here. Callers cannot use these transient details for approval;
future persistence must happen within the validated transaction under the gate.
"""

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.dispatch_routes import dispatcher, service_dataset
from app.dispatching import get_plan
from app.insertion import capture_insertion, recheck_service_insertion_calculation
from app.planning import fifo, nearest, solve_ortools, urgency_fifo, validate_plan
from app.security import now


class _CalculationScope:
    __slots__ = ("_active", "_counts")

    def __init__(self, workers, visits, excluded):
        self._active = True
        self._counts = (workers, visits, excluded)

    def summary(self):
        if not self._active:
            raise RuntimeError("service_insertion_context_closed")
        return dict(zip(("workers", "visits", "excluded"), self._counts, strict=True))

    def _close(self):
        self._active = False
        self._counts = None


@asynccontextmanager
async def authenticated_service_calculation(request, parent_id, payload):
    """Capture under gate, solve outside it, reauthenticate and recheck under gate."""
    settings = request.app.state.settings
    if request.method != "POST" or settings.deployment_mode != "service":
        raise HTTPException(404, "service_insertion_unavailable")
    database = request.app.state.database
    async with database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        provider, revision = await service_dataset(request, db)
        parent = await get_plan(db, parent_id)
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
        provider, revision = await service_dataset(request, db)
        parent = await get_plan(db, parent_id)
        await recheck_service_insertion_calculation(
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
        scope = _CalculationScope(
            len(problem.workers), sum(len(r.visits) for r in result.routes), len(excluded)
        )
        # No data, solver result, provisional identifier, or DB session leaves the scope.
        del problem, state, excluded, metadata, base, result
        try:
            yield scope
        finally:
            scope._close()
