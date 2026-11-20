"""Internal validator for a first service insertion child; not connected to routes.

Requires the caller to hold the dispatch gate and normal user/assignment locks. Does
not use a live provider. Database hashes are not signatures against a DB operator.
"""

import json
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.auth_service import service_account_eligible
from app.dispatching import fingerprint
from app.insertion_snapshot import (
    ExecutionSnapshot,
    Origin,
    ParentEvidence,
    RouteEvidence,
    validate_execution_snapshot,
)
from app.models import (
    Assignment,
    CompletionReport,
    DispatchPlan,
    PlanMembership,
    PlanningDatasetActivation,
    ServicePlanDisposition,
    ServicePlanExecutionEntry,
    User,
)
from app.planning import PlanningInput, PlanResult, input_hash, validate_plan
from app.security import assignment_current


def _unassigned_matches(parents, result, dispositions):
    unassigned_tickets = {item.ticket_id for item in result.unassigned}
    expected = {
        item.origin.assignment_id
        for item in parents
        if str(item.origin.ticket_id) in unassigned_tickets
    }
    actual = {
        item.parent_assignment_id for item in dispositions if item.classification == "unassigned"
    }
    return actual == expected


async def _child_valid(db, child_id, assignment=None, ticket=None, *, historical=False):
    try:
        settings = db.info.get("settings")
        if getattr(settings, "deployment_mode", None) != "service":
            return False
        child = await db.get(DispatchPlan, child_id)
        if (
            not child
            or child.kind != "insertion"
            or child.status not in ({"approved", "superseded"} if historical else {"approved"})
            or not child.approved_by
            or not child.approved_at
            or not child.parent_plan_id
        ):
            return False
        parent = await db.get(DispatchPlan, child.parent_plan_id)
        if (
            not parent
            or parent.kind not in {"initial", "insertion"}
            or (parent.kind == "initial") != (parent.parent_plan_id is None)
            or parent.status != "superseded"
            or not parent.approved_by
            or not parent.approved_at
            or parent.approved_at > child.approved_at
        ):
            return False
        child_input = PlanningInput.model_validate_json(json.dumps(child.input_data))
        child_result = PlanResult.model_validate_json(json.dumps(child.result_data))
        parent_input = PlanningInput.model_validate_json(json.dumps(parent.input_data))
        parent_result = PlanResult.model_validate_json(json.dumps(parent.result_data))
        validate_plan(child_input, child_result)
        validate_plan(parent_input, parent_result)
        dataset = child.snapshot_data["dataset"]
        if (
            child_input.data_kind != "service"
            or parent_input.data_kind != "service"
            or input_hash(child_input) != child.input_hash
            or child_result.input_hash != child.input_hash
            or input_hash(parent_input) != parent.input_hash
            or parent_result.input_hash != parent.input_hash
            or child_input.travel != parent_input.travel
            or child_input.dataset_hash != parent_input.dataset_hash
            or child_input.dataset_revision != parent_input.dataset_revision
            or dataset
            != {"sha256": child_input.dataset_hash, "revision": child_input.dataset_revision}
            or parent.snapshot_data.get("dataset") != dataset
        ):
            return False
        root = parent
        seen = {child.id}
        while root.kind == "insertion":
            if root.id in seen or not root.parent_plan_id:
                return False
            seen.add(root.id)
            root = await db.get(DispatchPlan, root.parent_plan_id)
            if not root:
                return False
        if root.kind != "initial" or root.parent_plan_id is not None or root.id in seen:
            return False
        activation = await db.scalar(
            select(PlanningDatasetActivation).where(
                PlanningDatasetActivation.namespace == settings.service_namespace,
                PlanningDatasetActivation.revision == child_input.dataset_revision,
                PlanningDatasetActivation.sha256 == child_input.dataset_hash,
            )
        )
        if (
            not activation
            or activation.version != parent_input.travel.version
            or activation.dataset_id != root.request_data.get("preset_id")
        ):
            return False
        snapshot = ExecutionSnapshot.model_validate(child.snapshot_data["service_execution"])
        parent_members = list(
            (
                await db.scalars(
                    select(PlanMembership)
                    .where(PlanMembership.plan_id == parent.id)
                    .order_by(PlanMembership.id)
                )
            ).all()
        )
        saved_parent = child.snapshot_data["parent"]
        if (
            saved_parent[0] != str(parent.id)
            or saved_parent[1] + 1 != parent.revision
            or saved_parent[2] != "approved"
            or saved_parent[3] != parent.input_hash
            or saved_parent[4] != parent.insertion_data
        ):
            return False
        parent_evidence = {
            "input": parent.input_data,
            "result": parent.result_data,
            "snapshot": parent.snapshot_data,
            "request": parent.request_data,
            "approved_by": str(parent.approved_by),
            "approved_at": parent.approved_at.isoformat(),
            "memberships": [
                [
                    str(m.id),
                    str(m.assignment_id),
                    m.kind,
                    m.sequence,
                    str(m.original_assignment_id),
                    m.timing_data,
                ]
                for m in parent_members
            ],
        }
        parent_snapshot_entries = {}
        if parent.kind == "insertion":
            previous = ExecutionSnapshot.model_validate(parent.snapshot_data["service_execution"])
            parent_snapshot_entries = {
                item.origin.assignment_id: item
                for path in previous.workers
                for item in path.entries
            }
            previous_entries = list(
                (
                    await db.scalars(
                        select(ServicePlanExecutionEntry)
                        .where(ServicePlanExecutionEntry.plan_id == parent.id)
                        .order_by(ServicePlanExecutionEntry.id)
                    )
                ).all()
            )
            previous_dispositions = list(
                (
                    await db.scalars(
                        select(ServicePlanDisposition)
                        .where(ServicePlanDisposition.plan_id == parent.id)
                        .order_by(ServicePlanDisposition.id)
                    )
                ).all()
            )
            parent_evidence["execution_entries"] = [
                [
                    str(e.id),
                    str(e.assignment_id),
                    str(e.worker_id),
                    e.execution_order,
                    str(e.predecessor_assignment_id),
                    str(e.parent_membership_id),
                    e.classification,
                    e.origin_data,
                ]
                for e in previous_entries
            ]
            parent_evidence["dispositions"] = [
                [
                    str(d.id),
                    str(d.parent_assignment_id),
                    str(d.parent_membership_id),
                    d.classification,
                    str(d.replacement_assignment_id),
                ]
                for d in previous_dispositions
            ]
        parent_hash = fingerprint(parent_evidence)
        if parent_hash != child.snapshot_data["parent_evidence"]:
            return False
        parent_specs = {spec.id: spec for spec in parent_input.tickets}
        parent_routes = {route.worker_id: route for route in parent_result.routes}
        excluded = {x["worker_id"] for x in child.excluded if x.get("worker_id") is not None}
        parents = []
        for membership in parent_members:
            a = await db.get(Assignment, membership.assignment_id)
            if not a:
                return False
            if parent.kind == "initial":
                if (
                    a.plan_id != parent.id
                    or membership.kind != "new"
                    or membership.original_assignment_id is not None
                ):
                    return False
                route = parent_routes[str(a.worker_id)]
                sequence = a.plan_sequence
                if type(sequence) is not int or sequence < 0 or sequence >= len(route.visits):
                    return False
                visit = route.visits[sequence]
                if (
                    membership.sequence != sequence
                    or visit.ticket_id != str(a.ticket_id)
                    or visit.arrival_min != a.planned_arrival_min
                    or visit.completion_min != a.planned_completion_min
                ):
                    return False
                spec = parent_specs[str(a.ticket_id)]
                origin = Origin(
                    assignment_id=a.id,
                    ticket_id=a.ticket_id,
                    worker_id=a.worker_id,
                    plan_id=parent.id,
                    sequence=sequence,
                    location_code=parent_input.travel.nodes[spec.node],
                    dataset_hash=parent_input.dataset_hash,
                    dataset_revision=parent_input.dataset_revision,
                    planned_arrival=parent.plan_start_at + timedelta(minutes=visit.arrival_min),
                    planned_completion=parent.plan_start_at
                    + timedelta(minutes=visit.completion_min),
                )
                predecessor = a.predecessor_id
            else:
                prior = parent_snapshot_entries[a.id]
                origin = prior.origin
                sequence = prior.execution_order
                predecessor = prior.predecessor_id
                if membership.sequence != sequence or origin.ticket_id != a.ticket_id:
                    return False
            records = child.snapshot_data["workers"][str(a.worker_id)]["assignments"]
            matching = [row for row in records if row[0] == str(a.id)]
            if len(matching) != 1:
                return False
            row = matching[0]
            if (
                row[1] != str(a.ticket_id)
                or row[3] != a.auth_epoch
                or row[4] != str(origin.plan_id)
                or row[6] != str(parent.id)
                or row[14] != "None"
            ):
                return False
            classification = (
                "history"
                if row[13] != "None"
                else "fixed"
                if row[11] != "None" or row[12] != "None"
                else "excluded"
                if str(a.worker_id) in excluded
                else "route"
            )
            if classification == "history" and (
                not a.visit_ended_at or a.visit_ended_at > child.approved_at
            ):
                return False
            if classification == "fixed" and (
                not (a.departed_at or a.started_at)
                or (a.departed_at or a.started_at) > child.approved_at
            ):
                return False
            for index, current in (
                (11, a.departed_at),
                (12, a.started_at),
                (13, a.visit_ended_at),
            ):
                if row[index] != "None":
                    if str(current) != row[index]:
                        return False
                elif current is not None and (
                    current < child.approved_at or classification == "history"
                ):
                    return False
            if (
                (a.departed_at and a.started_at and a.started_at < a.departed_at)
                or (a.started_at and a.visit_ended_at and a.visit_ended_at < a.started_at)
                or (a.departed_at and a.visit_ended_at and a.visit_ended_at < a.departed_at)
            ):
                return False
            saved_predecessor = None if row[9] == "None" else uuid.UUID(row[9])
            if a.predecessor_id != saved_predecessor:
                return False
            parents.append(
                ParentEvidence(
                    membership_id=membership.id,
                    origin=origin,
                    classification=classification,
                    execution_order=sequence,
                    predecessor_id=predecessor,
                )
            )
        if parent.kind == "initial" and len(parents) != sum(
            len(route.visits) for route in parent_result.routes
        ):
            return False
        parent_map = {p.origin.assignment_id: p for p in parents}
        entries = list(
            (
                await db.scalars(
                    select(ServicePlanExecutionEntry).where(
                        ServicePlanExecutionEntry.plan_id == child.id
                    )
                )
            ).all()
        )
        dispositions = list(
            (
                await db.scalars(
                    select(ServicePlanDisposition).where(ServicePlanDisposition.plan_id == child.id)
                )
            ).all()
        )
        members = list(
            (
                await db.scalars(select(PlanMembership).where(PlanMembership.plan_id == child.id))
            ).all()
        )
        snapshot_entries = [entry for path in snapshot.workers for entry in path.entries]
        db_entries = {entry.assignment_id: entry for entry in entries}
        member_map = {member.assignment_id: member for member in members}
        if (
            len(entries) != len(snapshot_entries)
            or len(db_entries) != len(entries)
            or len(member_map) != len(entries)
        ):
            return False
        for item in snapshot_entries:
            aid = item.origin.assignment_id
            row = db_entries[aid]
            member = member_map[aid]
            a = await db.get(Assignment, aid)
            if (
                not a
                or a.ticket_id != item.origin.ticket_id
                or a.worker_id != item.origin.worker_id
                or (not historical and a.active_plan_id != child.id)
                or row.worker_id != item.origin.worker_id
                or row.execution_order != item.execution_order
                or row.predecessor_assignment_id != item.predecessor_id
                or row.parent_membership_id != item.parent_membership_id
                or row.classification != item.classification
                or row.origin_data
                != {
                    "origin": item.origin.model_dump(mode="json"),
                    "replaces_assignment_id": str(item.replaces_assignment_id)
                    if item.replaces_assignment_id
                    else None,
                }
                or member.sequence != item.execution_order
            ):
                return False
            old = parent_map.get(aid)
            expected_kind = (
                ("fixed" if item.classification == "fixed" else "retained")
                if old
                else ("replaced" if item.replaces_assignment_id else "new")
            )
            expected_original = aid if old else item.replaces_assignment_id
            if member.kind != expected_kind or member.original_assignment_id != expected_original:
                return False
            if not old and (
                a.plan_id != child.id
                or a.plan_sequence != item.origin.sequence
                or a.predecessor_id != item.predecessor_id
                or child.plan_start_at + timedelta(minutes=a.planned_arrival_min)
                != item.origin.planned_arrival
                or child.plan_start_at + timedelta(minutes=a.planned_completion_min)
                != item.origin.planned_completion
            ):
                return False
        if len(dispositions) != len(snapshot.dispositions):
            return False
        db_dispositions = {d.parent_assignment_id: d for d in dispositions}
        if not _unassigned_matches(parents, child_result, snapshot.dispositions):
            return False
        for item in snapshot.dispositions:
            row = db_dispositions[item.parent_assignment_id]
            old = await db.get(Assignment, item.parent_assignment_id)
            if (
                row.parent_membership_id != item.parent_membership_id
                or row.classification != item.classification
                or row.replacement_assignment_id != item.replacement_assignment_id
                or not old
                or not old.revoked
                or old.active_plan_id is not None
            ):
                return False
        route_evidence = []
        specs = {item.id: item for item in child_input.tickets}
        for route in child_result.routes:
            for sequence, visit in enumerate(route.visits):
                matching = [
                    item
                    for item in snapshot_entries
                    if item.classification == "route"
                    and str(item.origin.worker_id) == route.worker_id
                    and str(item.origin.ticket_id) == visit.ticket_id
                ]
                if len(matching) != 1:
                    return False
                item = matching[0]
                old = parent_map.get(item.origin.assignment_id)
                route_evidence.append(
                    RouteEvidence(
                        worker_id=uuid.UUID(route.worker_id),
                        assignment_id=item.origin.assignment_id,
                        ticket_id=uuid.UUID(visit.ticket_id),
                        location_code=child_input.travel.nodes[specs[visit.ticket_id].node],
                        origin_sequence=old.origin.sequence if old else sequence,
                        planned_arrival=child.plan_start_at + timedelta(minutes=visit.arrival_min),
                        planned_completion=child.plan_start_at
                        + timedelta(minutes=visit.completion_min),
                    )
                )
        validate_execution_snapshot(
            snapshot,
            parent=tuple(parents),
            routes=tuple(route_evidence),
            plan_id=child.id,
            parent_plan_id=parent.id,
            dataset_hash=child_input.dataset_hash,
            dataset_revision=child_input.dataset_revision,
        )
        if historical:
            return True
        target = next(
            item for item in snapshot_entries if item.origin.assignment_id == assignment.id
        )
        if (
            target.classification not in {"route", "fixed", "excluded"}
            or assignment.ticket_id != ticket.id
            or not assignment_current(assignment, await db.get(User, assignment.worker_id))
        ):
            return False
        location = ticket.location_data
        if (
            location.get("state") != "confirmed"
            or location.get("source_type") != "service"
            or location.get("dataset_hash") != child_input.dataset_hash
            or location.get("dataset_revision") != child_input.dataset_revision
            or (location.get("selected") or {}).get("code") != target.origin.location_code
            or (
                assignment.execution_location_code is not None
                and assignment.execution_location_code != target.origin.location_code
            )
        ):
            return False
        if target.predecessor_id:
            previous = await db.get(Assignment, target.predecessor_id)
            if not previous or not previous.visit_ended_at:
                return False
        if target.classification == "excluded":
            worker = await db.get(User, assignment.worker_id)
            if not service_account_eligible(worker):
                return False
            worker_assignments = list(
                (
                    await db.scalars(
                        select(Assignment).where(Assignment.worker_id == assignment.worker_id)
                    )
                ).all()
            )
            for other in worker_assignments:
                pending = await db.scalar(
                    select(CompletionReport.id)
                    .where(
                        CompletionReport.assignment_id == other.id,
                        CompletionReport.reviewed.is_(False),
                        CompletionReport.superseded_at.is_(None),
                        CompletionReport.analysis_status != "dismissed",
                    )
                    .limit(1)
                )
                if pending:
                    return False
                if other.id == assignment.id or other.visit_ended_at:
                    continue
                if other.active_plan_id != child.id and (
                    not other.revoked or other.active_plan_id is not None
                ):
                    return False
                if other.stopped_at and other.active_plan_id:
                    return False
                if (other.departed_at or other.started_at) and not other.revoked:
                    return False
        return True
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        IndexError,
        OverflowError,
        StopIteration,
    ):
        return False


async def initial_child_execution_valid(db, assignment, ticket):
    """Check structure and live execution conditions under the caller's gate."""
    if not await historical_plan_valid(db, assignment.active_plan_id):
        return False
    return await _child_valid(db, assignment.active_plan_id, assignment, ticket)


async def first_child_structure_valid(db, child_id):
    """Check lineage only; never use this to authorize execution."""
    return await historical_plan_valid(db, child_id)


async def historical_plan_valid(db, plan_id):
    """Validate every explicit ancestor without consulting live ownership."""
    seen = set()
    current = plan_id
    while current is not None:
        if current in seen:
            return False
        seen.add(current)
        plan = await db.get(DispatchPlan, current)
        if not plan:
            return False
        if plan.kind == "initial":
            if plan.parent_plan_id is not None:
                return False
            from fastapi import HTTPException

            from app.insertion_residual import load_first_parent_residuals

            try:
                target = await db.get(DispatchPlan, plan_id)
                await load_first_parent_residuals(db, target)
                return True
            except HTTPException:
                return False
        if plan.kind != "insertion" or not await _child_valid(db, current, historical=True):
            return False
        current = plan.parent_plan_id
    return False
