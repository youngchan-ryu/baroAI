"""Internal fail-closed preflight for a service insertion parent.

Not wired to public insertion routes. This first stage handles only an initial
service plan; descendant lineage and approval still need separate validation.
"""

import json
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.auth_service import service_account_eligible
from app.dispatch_lock import dispatch_lock
from app.dispatch_schemas import PlanCreate
from app.execution import service_plan_execution_valid
from app.models import Assignment, DispatchPlan, PlanMembership, Ticket, User, WorkerSchedule
from app.planning import PlanningInput, PlanResult, input_hash, validate_plan
from app.planning.registry import active_generation


async def validate_service_remaining(
    db, assignment, remaining, parent_input, reference_at, *, approved_code=None
):
    """Check that a fixed visit's remaining-time evidence describes that approved visit."""
    try:
        if not isinstance(remaining, dict) or not remaining:
            raise ValueError("invalid_remaining")
        minutes, revision = remaining["minutes"], remaining["revision"]
        if type(minutes) is not int or not 1 <= minutes <= 480:
            raise ValueError("invalid_remaining_minutes")
        if type(revision) is not int or revision != assignment.execution_revision:
            raise ValueError("invalid_remaining_revision")
        reason, confirmed_by = remaining["reason"], remaining["confirmed_by"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("invalid_remaining_reason")
        if not isinstance(confirmed_by, str):
            raise ValueError("invalid_remaining_confirmer")
        confirmer_id = uuid.UUID(confirmed_by)
        if not confirmer_id.int or not await db.get(User, confirmer_id):
            raise ValueError("invalid_remaining_confirmer")
        confirmed = datetime.fromisoformat(remaining["confirmed_at"])
        expected = datetime.fromisoformat(remaining["expected_end_at"])
        expires = datetime.fromisoformat(remaining["expires_at"])
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (confirmed, expected, expires, reference_at)
        ):
            raise ValueError("invalid_remaining_timezone")
        if (
            confirmed > reference_at
            or (assignment.departed_at is not None and confirmed < assignment.departed_at)
            or (assignment.started_at is not None and confirmed < assignment.started_at)
            or expected != confirmed + timedelta(minutes=minutes)
            or expires <= confirmed
            or expires > min(confirmed + timedelta(minutes=5), expected)
        ):
            raise ValueError("invalid_remaining_times")
        if approved_code is None:
            specs = [item for item in parent_input.tickets if item.id == str(assignment.ticket_id)]
            if len(specs) != 1:
                raise ValueError("invalid_remaining_ticket")
            code = parent_input.travel.nodes[specs[0].node]
        else:
            if approved_code not in parent_input.travel.nodes:
                raise ValueError("invalid_remaining_ticket")
            code = approved_code
        if remaining["location_code"] != code or assignment.execution_location_code != code:
            raise ValueError("invalid_remaining_location")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        raise HTTPException(409, "service_remaining_evidence_invalid") from None


async def validate_initial_service_parent(db, settings, provider, provider_revision, parent_id):
    """Validate an initial parent under the dispatch gate without changing it.

    A digest is an identity check, not an institutional attestation or a
    signature against a malicious database operator.
    """
    await dispatch_lock(db)
    if type(provider_revision) is not int or provider_revision < 1:
        raise HTTPException(503, "planning_dataset_unavailable")
    revision = await active_generation(db, settings, provider, provider_revision)
    parent = await db.get(DispatchPlan, parent_id, populate_existing=True)
    if not parent or parent.kind != "initial" or parent.status != "approved":
        raise HTTPException(409, "service_insertion_parent_invalid")
    if not parent.approved_by or not parent.approved_at:
        raise HTTPException(409, "service_insertion_parent_invalid")
    try:
        problem = PlanningInput.model_validate_json(json.dumps(parent.input_data))
        result = PlanResult.model_validate_json(json.dumps(parent.result_data))
        request = PlanCreate.model_validate(parent.request_data)
        validate_plan(problem, result)
        snapshot = parent.snapshot_data
        data = provider.dataset
        nodes = tuple(item.code for item in data.locations)
        if (
            problem.data_kind != "service"
            or problem.dataset_hash != provider.sha256
            or problem.dataset_revision != revision
            or input_hash(problem) != parent.input_hash
            or result.input_hash != parent.input_hash
            or snapshot.get("dataset") != {"sha256": provider.sha256, "revision": revision}
            or problem.travel.nodes != nodes
            or problem.travel.minutes != data.travel_minutes
            or problem.travel.version != data.version
            or request.preset_id != data.dataset_id
            or request.dataset_hash != provider.sha256
            or request.dataset_revision != revision
            or request.plan_start_at != parent.plan_start_at
        ):
            raise ValueError("parent_dataset_mismatch")
        routes = {route.worker_id: route for route in result.routes}
        for worker in problem.workers:
            user = await db.get(User, uuid.UUID(worker.id))
            schedule = await db.get(WorkerSchedule, uuid.UUID(worker.id))
            saved = snapshot["workers"][worker.id]
            expected_revision = saved["schedule_revision"] + (1 if worker.id in routes else 0)
            if (
                not user
                or user.role != "worker"
                or not service_account_eligible(user)
                or user.auth_epoch != saved["epoch"]
                or not schedule
                or schedule.revision != expected_revision
                or schedule.data != saved["schedule"]
                or schedule.data.get("dataset_hash") != provider.sha256
                or schedule.data.get("dataset_revision") != revision
            ):
                raise ValueError("parent_worker_stale")
        memberships = list(
            (
                await db.scalars(select(PlanMembership).where(PlanMembership.plan_id == parent.id))
            ).all()
        )
        route_visits = [(route, visit) for route in result.routes for visit in route.visits]
        if len(memberships) != len(route_visits):
            raise ValueError("parent_membership_count")
        assignments = []
        indexed = {}
        for membership in memberships:
            assignment = await db.get(Assignment, membership.assignment_id)
            if not assignment:
                raise ValueError("parent_assignment_missing")
            position = (str(assignment.worker_id), assignment.plan_sequence)
            if position in indexed:
                raise ValueError("parent_duplicate_position")
            indexed[position] = assignment
        for membership in memberships:
            assignment = await db.get(Assignment, membership.assignment_id)
            ticket = await db.get(Ticket, assignment.ticket_id) if assignment else None
            owner = await db.get(User, assignment.worker_id) if assignment else None
            if (
                not assignment
                or not ticket
                or not owner
                or assignment.auth_epoch != owner.auth_epoch
                or assignment.active_plan_id != parent.id
                or assignment.plan_id != parent.id
            ):
                raise ValueError("parent_assignment_invalid")
            if assignment.visit_ended_at is None:
                if ticket.status == "completed":
                    raise ValueError("explicit_visit_end_required")
                if assignment.revoked or not await service_plan_execution_valid(
                    db, assignment, ticket
                ):
                    raise ValueError("parent_assignment_invalid")
            else:
                # Completed visits are historical: current ticket fields can legitimately change.
                sequence = assignment.plan_sequence
                route = routes.get(str(assignment.worker_id))
                if type(sequence) is not int or not route or not 0 <= sequence < len(route.visits):
                    raise ValueError("historical_position_invalid")
                visit = route.visits[sequence]
                specs = [item for item in problem.tickets if item.id == str(assignment.ticket_id)]
                prior = indexed.get((str(assignment.worker_id), sequence - 1)) if sequence else None
                start = assignment.started_at or assignment.departed_at
                if (
                    len(specs) != 1
                    or membership.kind != "new"
                    or membership.sequence != sequence
                    or membership.original_assignment_id is not None
                    or str(assignment.ticket_id) != visit.ticket_id
                    or assignment.planned_arrival_min != visit.arrival_min
                    or assignment.planned_completion_min != visit.completion_min
                    or assignment.execution_location_code != problem.travel.nodes[specs[0].node]
                    or not start
                    or assignment.visit_ended_at < start
                    or (
                        assignment.departed_at is not None
                        and assignment.visit_ended_at < assignment.departed_at
                    )
                    or (
                        assignment.departed_at is not None
                        and assignment.started_at is not None
                        and assignment.started_at < assignment.departed_at
                    )
                    or (sequence == 0 and assignment.predecessor_id is not None)
                    or (sequence > 0 and (not prior or assignment.predecessor_id != prior.id))
                ):
                    raise ValueError("historical_evidence_invalid")
            assignments.append(assignment.id)
        actual = set(
            (
                await db.scalars(
                    select(Assignment.id).where(Assignment.active_plan_id == parent.id)
                )
            ).all()
        )
        if len(set(assignments)) != len(route_visits) or set(assignments) != actual:
            raise ValueError("parent_assignment_scope")
        return problem, result
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise HTTPException(409, "service_insertion_parent_invalid") from None


async def validate_first_child_as_parent(db, settings, provider, provider_revision, parent_id):
    """Internal 2nd-insertion preflight; does not create or approve a plan.

    The prior child execution entries supply current order. The original assignment
    order and planned times remain its immutable origin. Caller holds the transaction
    until capture is complete; no public service insertion route is enabled.
    """
    from app.insertion_execution import first_child_structure_valid
    from app.insertion_snapshot import ExecutionSnapshot
    from app.models import ServicePlanExecutionEntry
    from app.security import now

    await dispatch_lock(db)
    if type(provider_revision) is not int or provider_revision < 1:
        raise HTTPException(503, "planning_dataset_unavailable")
    revision = await active_generation(db, settings, provider, provider_revision)
    parent = await db.get(DispatchPlan, parent_id, populate_existing=True)
    if not parent or parent.kind != "insertion" or parent.status != "approved":
        raise HTTPException(409, "service_insertion_parent_invalid")
    if not await first_child_structure_valid(db, parent_id):
        raise HTTPException(409, "service_insertion_parent_invalid")
    try:
        problem = PlanningInput.model_validate_json(json.dumps(parent.input_data))
        result = PlanResult.model_validate_json(json.dumps(parent.result_data))
        data = provider.dataset
        if (
            problem.dataset_hash != provider.sha256
            or problem.dataset_revision != revision
            or problem.travel.nodes != tuple(item.code for item in data.locations)
            or problem.travel.minutes != data.travel_minutes
            or problem.travel.version != data.version
        ):
            raise ValueError("child_dataset_mismatch")
        snapshot = ExecutionSnapshot.model_validate(parent.snapshot_data["service_execution"])
        entries = list(
            (
                await db.scalars(
                    select(ServicePlanExecutionEntry).where(
                        ServicePlanExecutionEntry.plan_id == parent.id
                    )
                )
            ).all()
        )
        actual = set(
            (
                await db.scalars(
                    select(Assignment.id).where(Assignment.active_plan_id == parent.id)
                )
            ).all()
        )
        if actual != {row.assignment_id for row in entries}:
            raise ValueError("child_assignment_scope")
        worker_ids = set(parent.insertion_data["worker_specs"])
        if {str(path.worker_id) for path in snapshot.workers} - worker_ids:
            raise ValueError("child_worker_scope")
        for worker_id in sorted(worker_ids):
            user = await db.get(User, uuid.UUID(worker_id))
            schedule = await db.get(WorkerSchedule, uuid.UUID(worker_id))
            saved = parent.snapshot_data["workers"][worker_id]
            if (
                not user
                or user.role != "worker"
                or not service_account_eligible(user)
                or user.auth_epoch != saved["epoch"]
                or not schedule
                or schedule.revision != saved["schedule_revision"] + 1
                or schedule.data != saved["schedule"]
                or schedule.data.get("dataset_hash") != provider.sha256
                or schedule.data.get("dataset_revision") != revision
            ):
                raise ValueError("child_worker_stale")
        origin_by_id = {
            item.origin.assignment_id: item.origin
            for path in snapshot.workers
            for item in path.entries
        }
        for row in entries:
            a = await db.get(Assignment, row.assignment_id)
            ticket = await db.get(Ticket, a.ticket_id) if a else None
            worker = await db.get(User, a.worker_id) if a else None
            if not a or not ticket or not worker or a.auth_epoch != worker.auth_epoch:
                raise ValueError("child_assignment_stale")
            origin = origin_by_id[a.id]
            began = a.started_at or a.departed_at
            if (
                (a.departed_at and a.started_at and a.started_at < a.departed_at)
                or (a.visit_ended_at and (not began or a.visit_ended_at < began))
                or (a.departed_at and a.visit_ended_at and a.visit_ended_at < a.departed_at)
                or (ticket.status == "in_progress" and not a.started_at and not a.visit_ended_at)
                or any(
                    value and value > now()
                    for value in (a.departed_at, a.started_at, a.visit_ended_at)
                )
                or (
                    a.plan_id == parent.id
                    and any(
                        value and value < parent.approved_at
                        for value in (a.departed_at, a.started_at, a.visit_ended_at)
                    )
                )
                or (
                    (began or a.visit_ended_at)
                    and a.execution_location_code != origin.location_code
                )
            ):
                raise ValueError("child_visit_evidence_invalid")
            if not a.visit_ended_at:
                if a.revoked or a.stopped_at or ticket.status == "completed":
                    raise ValueError("child_assignment_stale")
                location = ticket.location_data
                if (
                    location.get("state") != "confirmed"
                    or location.get("source_type") != "service"
                    or location.get("dataset_hash") != provider.sha256
                    or location.get("dataset_revision") != revision
                    or (location.get("selected") or {}).get("code") != origin.location_code
                    or (
                        a.execution_location_code is not None
                        and a.execution_location_code != origin.location_code
                    )
                    or ((a.departed_at or a.started_at) and a.execution_location_code is None)
                ):
                    raise ValueError("child_location_stale")
        return problem, result, snapshot
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_insertion_parent_invalid") from None
