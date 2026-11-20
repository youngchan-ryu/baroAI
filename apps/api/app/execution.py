"""Visit execution is distinct from ticket resolution. All writes hold the dispatch gate."""

import json
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.models import Assignment, CompletionReport, Ticket, User
from app.schemas import ExecutionView
from app.security import assignment_current, now
from app.services.location import registered


def phase(a, ticket):
    if a.stopped_at:
        return "stopped"
    if a.visit_ended_at or ticket.status == "completed":
        return "visit_ended"
    if a.started_at or ticket.status == "in_progress":
        return "working"
    return "moving" if a.departed_at else "queued"


def execution_version(a, expected):
    if expected is not None and a.execution_revision != expected:
        raise HTTPException(409, "execution_revision_conflict")


def execution_location(ticket):
    selected = ticket.location_data.get("selected") or {}
    if ticket.location_data.get("state") != "confirmed" or not registered(selected.get("code", "")):
        raise HTTPException(409, "execution_location_unconfirmed")
    return selected["code"]


async def service_plan_execution_valid(db, a, ticket):
    """Check approved service plan evidence without depending on a live dataset provider."""
    settings = db.info.get("settings")
    if getattr(settings, "deployment_mode", None) != "service":
        return True
    from app.models import DispatchPlan, PlanMembership

    if a.plan_id is None and a.active_plan_id is None:
        # A detached plan assignment must not become a manual assignment silently.
        if any(
            value is not None
            for value in (
                a.plan_sequence,
                a.planned_arrival_min,
                a.planned_completion_min,
                a.predecessor_id,
            )
        ):
            return False
        membership = await db.scalar(
            select(PlanMembership.id).where(PlanMembership.assignment_id == a.id).limit(1)
        )
        return membership is None
    if not a.plan_id or a.plan_id != a.active_plan_id or a.ticket_id != ticket.id:
        return False
    from app.planning import PlanningInput, PlanResult, input_hash, validate_plan

    plan = await db.get(DispatchPlan, a.plan_id)
    if (
        not plan
        or plan.kind != "initial"
        or plan.status != "approved"
        or not plan.approved_by
        or not plan.approved_at
    ):
        return False
    try:
        problem = PlanningInput.model_validate_json(json.dumps(plan.input_data))
        result = PlanResult.model_validate_json(json.dumps(plan.result_data))
        if problem.data_kind != "service" or input_hash(problem) != plan.input_hash:
            return False
        validate_plan(problem, result)
        if result.input_hash != plan.input_hash:
            return False
        dataset = plan.snapshot_data.get("dataset") or {}
        if (
            dataset.get("sha256") != problem.dataset_hash
            or dataset.get("revision") != problem.dataset_revision
        ):
            return False
        memberships = list(
            (
                await db.scalars(select(PlanMembership).where(PlanMembership.assignment_id == a.id))
            ).all()
        )
        if len(memberships) != 1:
            return False
        membership = memberships[0]
        if (
            membership.plan_id != plan.id
            or membership.kind != "new"
            or membership.original_assignment_id is not None
            or membership.sequence != a.plan_sequence
        ):
            return False
        routes = [route for route in result.routes if route.worker_id == str(a.worker_id)]
        specs = [spec for spec in problem.tickets if spec.id == str(ticket.id)]
        if len(routes) != 1 or len(specs) != 1 or type(a.plan_sequence) is not int:
            return False
        if a.plan_sequence < 0 or a.plan_sequence >= len(routes[0].visits):
            return False
        same_position = list(
            (
                await db.scalars(
                    select(Assignment).where(
                        Assignment.plan_id == plan.id,
                        Assignment.worker_id == a.worker_id,
                        Assignment.plan_sequence == a.plan_sequence,
                    )
                )
            ).all()
        )
        if len(same_position) != 1 or same_position[0].id != a.id:
            return False
        if a.plan_sequence == 0:
            if a.predecessor_id is not None:
                return False
        else:
            previous = list(
                (
                    await db.scalars(
                        select(Assignment).where(
                            Assignment.plan_id == plan.id,
                            Assignment.worker_id == a.worker_id,
                            Assignment.plan_sequence == a.plan_sequence - 1,
                        )
                    )
                ).all()
            )
            if len(previous) != 1 or previous[0].id != a.predecessor_id:
                return False
            predecessor = previous[0]
            previous_visit = routes[0].visits[a.plan_sequence - 1]
            if (
                str(predecessor.ticket_id) != previous_visit.ticket_id
                or predecessor.planned_arrival_min != previous_visit.arrival_min
                or predecessor.planned_completion_min != previous_visit.completion_min
            ):
                return False
            previous_memberships = list(
                (
                    await db.scalars(
                        select(PlanMembership).where(PlanMembership.assignment_id == predecessor.id)
                    )
                ).all()
            )
            if len(previous_memberships) != 1:
                return False
            previous_membership = previous_memberships[0]
            if (
                previous_membership.plan_id != plan.id
                or previous_membership.sequence != a.plan_sequence - 1
                or previous_membership.kind != "new"
                or previous_membership.original_assignment_id is not None
            ):
                return False
        visit = routes[0].visits[a.plan_sequence]
        if (
            visit.ticket_id != str(ticket.id)
            or visit.arrival_min != a.planned_arrival_min
            or visit.completion_min != a.planned_completion_min
        ):
            return False
        code = problem.travel.nodes[specs[0].node]
        location = ticket.location_data
        selected = location.get("selected") or {}
        return (
            location.get("state") == "confirmed"
            and location.get("source_type") == "service"
            and location.get("dataset_hash") == problem.dataset_hash
            and location.get("dataset_revision") == problem.dataset_revision
            and selected.get("code") == code
            and (a.execution_location_code is None or a.execution_location_code == code)
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


async def latest_report(db, a):
    return await db.scalar(
        select(CompletionReport)
        .where(CompletionReport.assignment_id == a.id, CompletionReport.reviewed.is_(True))
        .order_by(
            CompletionReport.approved_at.desc().nullslast(), CompletionReport.created_at.desc()
        )
        .limit(1)
    )


async def pending_report(db, a):
    return await db.scalar(
        select(CompletionReport.id)
        .where(
            CompletionReport.assignment_id == a.id,
            CompletionReport.reviewed.is_(False),
            CompletionReport.superseded_at.is_(None),
            CompletionReport.analysis_status != "dismissed",
        )
        .limit(1)
    )


def ongoing_visit(a, ticket, worker):
    """Match the visit that prevents a worker starting another assignment."""
    return assignment_current(a, worker) and phase(a, ticket) in {"moving", "working"}


async def blocked(db, a, ticket):
    if a.revoked:
        return "assignment_not_current"
    if phase(a, ticket) == "visit_ended":
        return "visit_already_ended"
    if a.predecessor_id:
        previous = await db.get(Assignment, a.predecessor_id)
        old_ticket = await db.get(Ticket, previous.ticket_id) if previous else None
        if previous and phase(previous, old_ticket) != "visit_ended":
            expected = previous.remaining_data.get("expected_end_at")
            if expected and now() >= datetime.fromisoformat(expected):
                return "remaining_reconfirmation_required"
            return "predecessor_visit_not_ended"
    others = list(
        (
            await db.scalars(
                select(Assignment).where(
                    Assignment.worker_id == a.worker_id,
                    Assignment.id != a.id,
                    Assignment.revoked.is_(False),
                )
            )
        ).all()
    )
    for other in others:
        t = await db.get(Ticket, other.ticket_id)
        if ongoing_visit(other, t, await db.get(User, other.worker_id)):
            return "worker_has_ongoing_visit"
    return None


async def execution_view(db, a, ticket, *, staff=False):
    current = phase(a, ticket)
    reason = await blocked(db, a, ticket)
    if current in {"queued", "moving"} and reason is None:
        if not await service_plan_execution_valid(db, a, ticket):
            reason = "service_plan_execution_stale"
    report = await latest_report(db, a)
    pending = await pending_report(db, a)
    current_report = await db.scalar(
        select(CompletionReport)
        .where(
            CompletionReport.assignment_id == a.id,
            CompletionReport.superseded_at.is_(None),
            CompletionReport.reviewed.is_(False),
            CompletionReport.analysis_status != "dismissed",
        )
        .order_by(CompletionReport.created_at.desc(), CompletionReport.id.desc())
        .limit(1)
    )
    correction_requested = bool(current_report and current_report.correction_requested_at)
    remaining = a.remaining_data or None
    from app.models import DispatchPlan

    plan = await db.get(DispatchPlan, a.active_plan_id) if a.active_plan_id else None
    return ExecutionView(
        active_plan_id=a.active_plan_id,
        active_plan_revision=plan.revision if plan else None,
        phase=current,
        revision=a.execution_revision,
        departed_at=a.departed_at,
        started_at=a.started_at,
        visit_ended_at=a.visit_ended_at,
        legacy_state_inferred=bool(
            (ticket.status == "in_progress" and not a.started_at)
            or (ticket.status == "completed" and not a.visit_ended_at)
        ),
        can_depart=current == "queued" and reason is None,
        can_start=current in {"queued", "moving"} and reason is None,
        can_report=(current == "working" or (current == "visit_ended" and correction_requested))
        and not a.revoked and (not pending or correction_requested),
        correction_requested=correction_requested,
        can_stop_visit=bool(
            staff and a.active_plan_id and not a.stopped_at and current in {"moving", "working"}
        ),
        can_end_visit=bool(staff and current in {"moving", "working"} and report and not pending),
        blocked_reason=reason,
        remaining=remaining,
        latest_approved_report={
            "id": report.id,
            "revision": report.revision,
            "result": report.confirmed_result or report.result,
        }
        if report
        else None,
    )


async def require_action(db, a, ticket, expected, action):
    execution_version(a, expected)
    reason = await blocked(db, a, ticket)
    current = phase(a, ticket)
    if current in {"visit_ended", "stopped"}:
        raise HTTPException(409, "visit_already_ended")
    if action in {"depart", "start"} and reason:
        raise HTTPException(409, reason)
    if action in {"depart", "start"} and not await service_plan_execution_valid(db, a, ticket):
        raise HTTPException(409, "service_plan_execution_stale")
    if action == "depart" and current != "queued":
        raise HTTPException(409, "visit_already_departed")
    if action == "start" and current not in {"queued", "moving"}:
        raise HTTPException(409, "visit_already_started")


def end_visit(db, a, ticket, actor, reason):
    from app.models import TicketEvent

    if a.visit_ended_at:
        return
    a.visit_ended_at = now()
    a.execution_revision += 1
    a.remaining_data = {}
    if not a.execution_location_code:
        selected = ticket.location_data.get("selected") or {}
        a.execution_location_code = selected.get("code")
    db.add(
        TicketEvent(ticket_id=ticket.id, actor_id=actor.id, event_type="visit_ended", reason=reason)
    )


async def current_for_staff(db, assignment_id):
    a = await db.get(Assignment, assignment_id)
    worker = await db.get(User, a.worker_id, with_for_update=True) if a else None
    if not a or not assignment_current(a, worker):
        raise HTTPException(409, "assignment_not_current")
    ticket = await db.get(Ticket, a.ticket_id, with_for_update=True)
    return a, ticket


async def confirm_remaining(db, a, ticket, principal, payload):
    execution_version(a, payload.expected_execution_revision)
    if phase(a, ticket) not in {"moving", "working"}:
        raise HTTPException(409, "remaining_requires_ongoing_visit")
    if await pending_report(db, a):
        raise HTTPException(409, "report_pending_review")
    settings = db.info.get("settings")
    if getattr(settings, "deployment_mode", None) == "service":
        if not await service_plan_execution_valid(db, a, ticket):
            raise HTTPException(409, "service_plan_execution_stale")
        if a.plan_id is not None:
            code = (ticket.location_data.get("selected") or {}).get("code")
            if not code or a.execution_location_code != code:
                raise HTTPException(409, "service_plan_execution_stale")
        else:
            code = execution_location(ticket)
    else:
        code = execution_location(ticket)
    stamp = now()
    expected = stamp + timedelta(minutes=payload.remaining_min)
    a.execution_revision += 1
    a.execution_location_code = code
    a.remaining_data = {
        "minutes": payload.remaining_min,
        "confirmed_at": stamp.isoformat(),
        "expected_end_at": expected.isoformat(),
        "expires_at": min(stamp + timedelta(minutes=5), expected).isoformat(),
        "reason": payload.reason,
        "revision": a.execution_revision,
        "confirmed_by": str(principal.user.id),
        "location_code": code,
    }
    from app.models import TicketEvent

    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=principal.user.id,
            event_type="remaining_confirmed",
            reason=payload.reason,
        )
    )


def stop_visit(db, a, ticket, actor, reason):
    from app.models import TicketEvent

    a.stopped_at, a.stopped_by, a.stop_reason = now(), actor.id, reason
    a.revoked = True
    a.execution_revision += 1
    a.remaining_data = {}
    # No destination arrival or successful resolution is inferred from stopping.
    db.add(
        TicketEvent(
            ticket_id=ticket.id, actor_id=actor.id, event_type="visit_stopped", reason=reason
        )
    )
