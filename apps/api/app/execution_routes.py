import uuid

from fastapi import APIRouter, HTTPException, Request

from app.dispatch_routes import dispatcher, receipt, save_receipt
from app.execution import (
    confirm_remaining,
    current_for_staff,
    end_visit,
    execution_version,
    execution_view,
    pending_report,
    phase,
    require_action,
)
from app.models import CompletionReport
from app.schemas import (
    DepartInput,
    EndVisitInput,
    ExecutionView,
    RemainingInput,
    StopVisitInput,
    TicketView,
)
from app.security import authenticate, locked_assignment, now
from app.tickets import get_ticket, ticket_view, transition

router = APIRouter(prefix="/v1")


@router.post("/worker/assignments/{assignment_id}/depart", response_model=TicketView)
async def depart(assignment_id: uuid.UUID, payload: DepartInput, request: Request):
    from app.routes import _photo_aware_transaction

    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db, worker_allowed=True)
        if not principal.assignment or principal.assignment.id != assignment_id:
            raise HTTPException(403, "assignment_forbidden")
        ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        a = await locked_assignment(db, principal, assignment_id)
        await require_action(db, a, ticket, payload.expected_execution_revision, "depart")
        a.execution_location_code = (ticket.location_data.get("selected") or {}).get("code")
        a.departed_at, a.execution_revision = now(), a.execution_revision + 1
        transition(db, ticket, principal.user, "departed", "작업자가 이동 시작을 확인했어요")
        await db.flush()
        return await ticket_view(db, ticket, principal)


@router.post("/assignments/{assignment_id}/remaining", response_model=ExecutionView)
async def remaining(assignment_id: uuid.UUID, payload: RemainingInput, request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        scope, hashed, old = await receipt(
            db, request, principal, f"remaining:{assignment_id}", payload
        )
        a, ticket = await current_for_staff(db, assignment_id)
        if not old:
            await confirm_remaining(db, a, ticket, principal, payload)
            await db.flush()
        view = await execution_view(db, a, ticket, staff=True)
        if not old:
            save_receipt(db, scope, hashed, view)
        return view


@router.post("/assignments/{assignment_id}/end-visit", response_model=ExecutionView)
async def finish_visit(assignment_id: uuid.UUID, payload: EndVisitInput, request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        scope, hashed, old = await receipt(
            db, request, principal, f"end-visit:{assignment_id}", payload
        )
        a, ticket = await current_for_staff(db, assignment_id)
        if request.app.state.settings.deployment_mode == "public_demo":
            ticket = await get_ticket(db, ticket.id, principal)
        if old:
            return await execution_view(db, a, ticket, staff=True)
        execution_version(a, payload.expected_execution_revision)
        report = await db.get(CompletionReport, payload.report_id)
        from app.execution import latest_report

        latest = await latest_report(db, a)
        if (
            not latest
            or latest.id != payload.report_id
            or phase(a, ticket) not in {"moving", "working"}
            or not report
            or report.assignment_id != a.id
            or not report.reviewed
            or report.revision != payload.expected_report_revision
            or await pending_report(db, a)
        ):
            raise HTTPException(409, "visit_end_requires_current_approved_report")
        end_visit(db, a, ticket, principal.user, payload.reason)
        await db.flush()
        view = await execution_view(db, a, ticket, staff=True)
        save_receipt(db, scope, hashed, view)
        return view


@router.get("/assignments/{assignment_id}/execution", response_model=ExecutionView)
async def read_execution(assignment_id: uuid.UUID, request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        from app.models import Assignment, Ticket

        a = await db.get(Assignment, assignment_id)
        if not a:
            raise HTTPException(404, "assignment_not_found")
        ticket = (
            await get_ticket(db, a.ticket_id, principal)
            if request.app.state.settings.deployment_mode == "public_demo"
            else await db.get(Ticket, a.ticket_id)
        )
        return await execution_view(db, a, ticket, staff=True)


@router.post("/assignments/{assignment_id}/stop-visit", response_model=ExecutionView)
async def stop_plan_visit(assignment_id: uuid.UUID, payload: "StopVisitInput", request: Request):
    from app.execution import stop_visit
    from app.models import Assignment, DispatchPlan, Ticket, User

    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        scope, hashed, old = await receipt(
            db, request, principal, f"stop-visit:{assignment_id}", payload
        )
        a = await db.get(Assignment, assignment_id)
        if not a:
            raise HTTPException(404, "assignment_not_found")
        await db.get(User, a.worker_id, with_for_update=True)
        ticket = await db.get(Ticket, a.ticket_id, with_for_update=True)
        if old:
            return await execution_view(db, a, ticket, staff=True)
        plan = await db.get(DispatchPlan, a.active_plan_id) if a.active_plan_id else None
        execution_version(a, payload.expected_execution_revision)
        if (
            not plan
            or plan.id != payload.expected_active_plan_id
            or plan.status != "approved"
            or plan.revision != payload.expected_plan_revision
            or phase(a, ticket) not in {"moving", "working"}
        ):
            raise HTTPException(409, "plan_stop_confirmation_conflict")
        stop_visit(db, a, ticket, principal.user, payload.reason)
        plan.revision += 1
        await db.flush()
        view = await execution_view(db, a, ticket, staff=True)
        save_receipt(db, scope, hashed, view)
        return view
