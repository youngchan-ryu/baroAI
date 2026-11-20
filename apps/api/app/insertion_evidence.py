"""Read-only service insertion evidence adapter; no public approval or persistence.

Caller must authenticate the current dispatcher and enforce CSRF in this transaction.
Only initial parents are supported; descendants and token/report preservation on
approval are deliberately not implemented. Never persist the transient fingerprint.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect, select

from app.dispatching import fingerprint
from app.insertion import insertion_snapshot_current, proposal_diff
from app.insertion_snapshot import (
    Disposition,
    Entry,
    ExecutionSnapshot,
    Origin,
    ParentEvidence,
    RouteEvidence,
    WorkerPath,
    validate_execution_snapshot,
)
from app.models import (
    Assignment,
    CompletionReport,
    DispatchPlan,
    PlanMembership,
    ServicePlanExecutionEntry,
    Ticket,
)
from app.planning import PlanningInput, PlanResult


def require_clean_session(db):
    if db.new or db.dirty or db.deleted:
        raise HTTPException(409, "service_insertion_dirty_session")


async def initial_parent_evidence(db, child, *, service_context):
    """Revalidate a proposed child under the gate, then derive initial parent evidence.

    The result contains a transient digest of complete assignment/report rows for a
    caller to compare inside the same locked transaction; it is not authorization,
    a signature, or proof that stored tokens or reports were preserved by approval.
    """
    require_clean_session(db)
    with db.no_autoflush:
        return await _initial_parent_evidence(db, child, service_context=service_context)


async def _initial_parent_evidence(db, child, *, service_context):
    parent = await insertion_snapshot_current(db, child, service_context=service_context)
    if parent.kind != "initial":
        raise HTTPException(409, "service_insertion_parent_invalid")
    try:
        problem = PlanningInput.model_validate_json(json.dumps(parent.input_data))
        excluded = {
            str(item["worker_id"]) for item in child.excluded if item.get("worker_id") is not None
        }
        memberships = list(
            (
                await db.scalars(select(PlanMembership).where(PlanMembership.plan_id == parent.id))
            ).all()
        )
        evidence, baseline = [], []
        for membership in memberships:
            a = await db.get(Assignment, membership.assignment_id)
            t = await db.get(Ticket, a.ticket_id)
            ticket_specs = [spec for spec in problem.tickets if spec.id == str(a.ticket_id)]
            if len(ticket_specs) != 1 or a.plan_id != parent.id:
                raise ValueError("parent_origin_invalid")
            if not a.visit_ended_at and (
                t.status == "completed" or (t.status == "in_progress" and not a.started_at)
            ):
                raise ValueError("explicit_visit_state_required")
            if a.visit_ended_at:
                classification = "history"
            elif a.departed_at or a.started_at:
                classification = "fixed"
            else:
                classification = "excluded" if str(a.worker_id) in excluded else "route"
            origin = Origin(
                assignment_id=a.id,
                ticket_id=a.ticket_id,
                worker_id=a.worker_id,
                plan_id=a.plan_id,
                sequence=a.plan_sequence,
                location_code=problem.travel.nodes[ticket_specs[0].node],
                dataset_hash=problem.dataset_hash,
                dataset_revision=problem.dataset_revision,
                planned_arrival=parent.plan_start_at + timedelta(minutes=a.planned_arrival_min),
                planned_completion=parent.plan_start_at
                + timedelta(minutes=a.planned_completion_min),
            )
            evidence.append(
                ParentEvidence(
                    membership_id=membership.id,
                    origin=origin,
                    classification=classification,
                    execution_order=a.plan_sequence,
                    predecessor_id=a.predecessor_id,
                )
            )
            reports = list(
                (
                    await db.scalars(
                        select(CompletionReport)
                        .where(CompletionReport.assignment_id == a.id)
                        .order_by(CompletionReport.id)
                    )
                ).all()
            )

            def row_values(row):
                return {
                    column.key: getattr(row, column.key) for column in inspect(type(row)).columns
                }

            baseline.append(
                {"assignment": row_values(a), "reports": [row_values(r) for r in reports]}
            )
        evidence.sort(key=lambda item: (str(item.origin.worker_id), item.execution_order))
        baseline.sort(key=lambda item: str(item["assignment"]["id"]))
        return tuple(evidence), fingerprint(jsonable_encoder(baseline))
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_insertion_parent_invalid") from None


async def _child_parent_evidence(db, child, *, service_context):
    """Derive a first child's current path while its capture and gate are valid."""
    parent = await insertion_snapshot_current(db, child, service_context=service_context)
    if parent.kind != "insertion":
        raise HTTPException(409, "service_insertion_parent_invalid")
    try:
        snapshot = ExecutionSnapshot.model_validate(parent.snapshot_data["service_execution"])
        saved = {
            entry.origin.assignment_id: entry for path in snapshot.workers for entry in path.entries
        }
        entries = list(
            (
                await db.scalars(
                    select(ServicePlanExecutionEntry).where(
                        ServicePlanExecutionEntry.plan_id == parent.id
                    )
                )
            ).all()
        )
        members = list(
            (
                await db.scalars(select(PlanMembership).where(PlanMembership.plan_id == parent.id))
            ).all()
        )
        memberships = {m.assignment_id: m for m in members}
        if (
            len(saved) != len(entries)
            or len(memberships) != len(members)
            or set(saved) != set(memberships)
        ):
            raise ValueError("parent_entry_scope_invalid")
        excluded = {
            str(item["worker_id"]) for item in child.excluded if item.get("worker_id") is not None
        }
        evidence, baseline = [], []
        for entry in entries:
            old = saved[entry.assignment_id]
            member = memberships[entry.assignment_id]
            a = await db.get(Assignment, entry.assignment_id)
            t = await db.get(Ticket, a.ticket_id)
            if (
                entry.origin_data["origin"] != old.origin.model_dump(mode="json")
                or entry.worker_id != old.origin.worker_id
                or entry.execution_order != old.execution_order
                or entry.predecessor_assignment_id != old.predecessor_id
                or a.active_plan_id != parent.id
                or a.worker_id != entry.worker_id
            ):
                raise ValueError("parent_entry_invalid")
            if not a.visit_ended_at and (
                t.status == "completed" or (t.status == "in_progress" and not a.started_at)
            ):
                raise ValueError("explicit_visit_state_required")
            classification = (
                "history"
                if a.visit_ended_at
                else "fixed"
                if a.departed_at or a.started_at
                else "excluded"
                if str(a.worker_id) in excluded
                else "route"
            )
            evidence.append(
                ParentEvidence(
                    membership_id=member.id,
                    origin=old.origin,
                    classification=classification,
                    execution_order=entry.execution_order,
                    predecessor_id=entry.predecessor_assignment_id,
                )
            )
            reports = list(
                (
                    await db.scalars(
                        select(CompletionReport)
                        .where(CompletionReport.assignment_id == a.id)
                        .order_by(CompletionReport.id)
                    )
                ).all()
            )

            def values(row):
                return {
                    column.key: getattr(row, column.key) for column in inspect(type(row)).columns
                }

            baseline.append({"assignment": values(a), "reports": [values(r) for r in reports]})
        evidence.sort(key=lambda item: (str(item.origin.worker_id), item.execution_order))
        baseline.sort(key=lambda item: str(item["assignment"]["id"]))
        return tuple(evidence), fingerprint(jsonable_encoder(baseline))
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_insertion_parent_invalid") from None


async def _validate_child_residuals(db, parent, child, problem, result, routes):
    """Cross-check every unresolved parent ledger row against current DB facts."""
    from datetime import datetime

    from app.insertion_residual import (
        ResidualEvidence,
        ResidualOutcome,
        load_first_parent_residuals,
        validate_residual_consumption,
    )

    try:
        previous = await load_first_parent_residuals(db, parent)
        evidence, outcomes = [], []
        solver_ids = [uuid.UUID(spec.id) for spec in problem.tickets]
        route_by_ticket = {}
        for route in routes:
            if route.ticket_id in route_by_ticket:
                raise ValueError("residual_route_duplicate")
            route_by_ticket[route.ticket_id] = route.assignment_id
        unassigned = [uuid.UUID(item.ticket_id) for item in result.unassigned]
        if len(set(unassigned)) != len(unassigned):
            raise ValueError("residual_unassigned_duplicate")
        for tid, row in previous.items():
            if row.outcome == "assigned":
                continue
            ticket = await db.get(Ticket, tid)
            if not ticket:
                raise ValueError("residual_ticket_missing")
            external = await db.scalar(
                select(Assignment.id)
                .where(
                    Assignment.ticket_id == tid,
                    Assignment.revoked.is_(False),
                    Assignment.visit_ended_at.is_(None),
                )
                .limit(1)
            )
            report = await db.scalar(
                select(CompletionReport.id)
                .where(
                    CompletionReport.ticket_id == tid,
                    CompletionReport.reviewed.is_(False),
                    CompletionReport.superseded_at.is_(None),
                    CompletionReport.analysis_status != "dismissed",
                )
                .limit(1)
            )
            availability = (
                "terminal"
                if ticket.status in {"completed", "merged", "cancelled", "rejected", "transferred"}
                else "external"
                if external
                else "report"
                if report
                else "unavailable"
                if ticket.status != "confirmed"
                else "available"
            )
            evidence.append(
                ResidualEvidence(
                    ticket_id=tid,
                    previous_assignment_id=row.previous_assignment_id,
                    source_plan_id=row.source_plan_id,
                    release_at=row.release_at,
                    deadline_at=row.deadline_at,
                    service_min=row.service_min,
                    availability=availability,
                )
            )
            selected = [x for x in child.excluded if x.get("ticket_id") == str(tid)]
            preserved = [
                x for x in child.insertion_data["preserved_diff"] if x.get("ticket_id") == str(tid)
            ]
            in_route, in_unassigned = tid in route_by_ticket, tid in unassigned
            if sum((in_route, in_unassigned, bool(selected), availability == "terminal")) != 1:
                raise ValueError("residual_result_invalid")
            if in_route or in_unassigned:
                timing = child.insertion_data["task_timing"].get(str(tid))
                if (
                    tid not in solver_ids
                    or not timing
                    or datetime.fromisoformat(timing["release_at"]) != row.release_at
                    or datetime.fromisoformat(timing["deadline_at"]) != row.deadline_at
                    or timing["service_min"] != row.service_min
                ):
                    raise ValueError("residual_timing_changed")
                outcomes.append(
                    ResidualOutcome(
                        ticket_id=tid,
                        kind="assigned" if in_route else "unassigned",
                        assignment_id=route_by_ticket.get(tid),
                        reason=None if in_route else "solver_unassigned",
                    )
                )
            elif availability == "terminal":
                if tid in solver_ids or selected or preserved:
                    raise ValueError("residual_terminal_invalid")
                outcomes.append(ResidualOutcome(ticket_id=tid, kind="terminal", reason="terminal"))
            else:
                if (
                    len(selected) != 1
                    or len(preserved) != 1
                    or tid in solver_ids
                    or preserved[0].get("kind") != "unassigned"
                    or preserved[0].get("reason") != selected[0].get("reason")
                ):
                    raise ValueError("residual_exclusion_invalid")
                reason = selected[0].get("reason")
                if reason == "parent_task_no_longer_available":
                    reason = availability
                outcomes.append(ResidualOutcome(ticket_id=tid, kind="excluded", reason=reason))
        validate_residual_consumption(tuple(evidence), tuple(outcomes))
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_insertion_residual_invalid") from None


async def _capture_initial_evidence(request, db, child_id, *, child_parent=False):
    """Derive initial-parent and route evidence in a fresh authenticated transaction.

    This is an internal read-only adapter. Generated IDs and the transient fingerprint
    must not be returned, logged, or persisted. The caller must not have flushed
    business writes earlier in the transaction. No approval or execution is performed.
    """
    require_clean_session(db)
    if request.method != "POST":
        raise HTTPException(405, "service_insertion_method_invalid")
    from app.dispatch_routes import dispatcher, service_dataset

    with db.no_autoflush:
        await dispatcher(request, db, require_demo=False)
        provider, revision = await service_dataset(request, db)
        from app.planning.datasets import has_unverified_travel

        if has_unverified_travel(provider):
            raise HTTPException(503, "verified_planning_data_unavailable")
        child = await db.get(DispatchPlan, child_id, populate_existing=True)
        if not child:
            raise HTTPException(404, "dispatch_plan_not_found")
        context = (request.app.state.settings, provider, revision)
        parents, baseline = await (
            _child_parent_evidence(db, child, service_context=context)
            if child_parent
            else _initial_parent_evidence(db, child, service_context=context)
        )
        try:
            parent = await db.get(DispatchPlan, child.parent_plan_id, populate_existing=True)
            problem = PlanningInput.model_validate_json(json.dumps(child.input_data))
            result = PlanResult.model_validate_json(json.dumps(child.result_data))
            metadata = dict(child.insertion_data)
            metadata["diff"] = list(metadata["preserved_diff"])
            diff = await proposal_diff(db, parent, result, child.plan_start_at, metadata)
            choices = diff[len(metadata["preserved_diff"]) :]
            expected_count = sum(len(route.visits) for route in result.routes)
            if len(choices) != expected_count + len(result.unassigned):
                raise ValueError("route_scope_invalid")
            parent_by_id = {str(item.origin.assignment_id): item for item in parents}
            specs = {item.id: item for item in problem.tickets}
            routes, generated = [], {}
            offset = 0
            for route in result.routes:
                for sequence, visit in enumerate(route.visits):
                    choice = choices[offset]
                    offset += 1
                    if (
                        choice.get("ticket_id") != visit.ticket_id
                        or choice.get("worker_id") != route.worker_id
                        or choice.get("kind") not in {"new", "replaced", "retained"}
                    ):
                        raise ValueError("route_choice_invalid")
                    old_id = choice.get("original_assignment_id")
                    if choice["kind"] == "retained":
                        old = parent_by_id[old_id]
                        assignment_id = old.origin.assignment_id
                        origin_sequence = old.origin.sequence
                    else:
                        if choice["kind"] == "replaced" and old_id not in parent_by_id:
                            raise ValueError("replacement_origin_invalid")
                        if choice["kind"] == "new" and old_id is not None:
                            raise ValueError("new_origin_invalid")
                        assignment_id = uuid.uuid4()
                        if await db.get(Assignment, assignment_id) or assignment_id in generated:
                            raise ValueError("assignment_identifier_collision")
                        generated[assignment_id] = (visit.ticket_id, choice["kind"], old_id)
                        origin_sequence = sequence
                    spec = specs[visit.ticket_id]
                    routes.append(
                        RouteEvidence(
                            worker_id=uuid.UUID(route.worker_id),
                            assignment_id=assignment_id,
                            ticket_id=uuid.UUID(visit.ticket_id),
                            location_code=problem.travel.nodes[spec.node],
                            origin_sequence=origin_sequence,
                            planned_arrival=child.plan_start_at
                            + timedelta(minutes=visit.arrival_min),
                            planned_completion=child.plan_start_at
                            + timedelta(minutes=visit.completion_min),
                        )
                    )
            if child_parent:
                await _validate_child_residuals(db, parent, child, problem, result, tuple(routes))
            return child, result, parents, tuple(routes), generated, baseline
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
            raise HTTPException(409, "service_insertion_evidence_invalid") from None


def _assemble_initial_snapshot(child, result, parents, routes, generated):
    """Assemble and independently validate the candidate while the gate is held."""
    try:
        parent_by_id = {item.origin.assignment_id: item for item in parents}
        parent_by_ticket = {item.origin.ticket_id: item for item in parents}
        if len(parent_by_ticket) != len(parents):
            raise ValueError("parent_ticket_duplicate")
        by_worker = {}
        for parent in parents:
            if parent.classification in {"history", "fixed", "excluded"}:
                by_worker.setdefault(parent.origin.worker_id, []).append(
                    (parent.classification, parent.origin, parent.membership_id, None)
                )
        dispositions = []
        for route in routes:
            old = parent_by_id.get(route.assignment_id)
            if old:
                origin, membership_id, replaces = old.origin, old.membership_id, None
            else:
                ticket_id, kind, old_id = generated[route.assignment_id]
                if uuid.UUID(ticket_id) != route.ticket_id:
                    raise ValueError("generated_ticket_mismatch")
                replaces = uuid.UUID(old_id) if kind == "replaced" else None
                membership_id = None
                origin = Origin(
                    assignment_id=route.assignment_id,
                    ticket_id=route.ticket_id,
                    worker_id=route.worker_id,
                    plan_id=child.id,
                    sequence=route.origin_sequence,
                    location_code=route.location_code,
                    dataset_hash=child.snapshot_data["dataset"]["sha256"],
                    dataset_revision=child.snapshot_data["dataset"]["revision"],
                    planned_arrival=route.planned_arrival,
                    planned_completion=route.planned_completion,
                )
                if replaces is not None:
                    replaced = parent_by_id[replaces]
                    dispositions.append(
                        Disposition(
                            parent_assignment_id=replaces,
                            parent_membership_id=replaced.membership_id,
                            classification="replaced",
                            replacement_assignment_id=route.assignment_id,
                        )
                    )
            by_worker.setdefault(route.worker_id, []).append(
                ("route", origin, membership_id, replaces)
            )
        for item in result.unassigned:
            old = parent_by_ticket.get(uuid.UUID(item.ticket_id))
            if old:
                dispositions.append(
                    Disposition(
                        parent_assignment_id=old.origin.assignment_id,
                        parent_membership_id=old.membership_id,
                        classification="unassigned",
                    )
                )
        paths = []
        for worker_id, values in sorted(by_worker.items(), key=lambda pair: str(pair[0])):
            values.sort(
                key=lambda item: {"history": 0, "fixed": 1, "route": 2, "excluded": 2}[item[0]]
            )
            entries, predecessor = [], None
            for index, (classification, origin, membership_id, replaces) in enumerate(values):
                entries.append(
                    Entry(
                        origin=origin,
                        classification=classification,
                        execution_order=index,
                        predecessor_id=predecessor,
                        parent_membership_id=membership_id,
                        replaces_assignment_id=replaces,
                    )
                )
                predecessor = origin.assignment_id
            paths.append(WorkerPath(worker_id=worker_id, entries=tuple(entries)))
        snapshot = ExecutionSnapshot(
            version=1,
            plan_id=child.id,
            parent_plan_id=child.parent_plan_id,
            dataset_hash=child.snapshot_data["dataset"]["sha256"],
            dataset_revision=child.snapshot_data["dataset"]["revision"],
            workers=tuple(paths),
            dispositions=tuple(dispositions),
        )
        validate_execution_snapshot(
            snapshot,
            parent=parents,
            routes=routes,
            plan_id=child.id,
            parent_plan_id=child.parent_plan_id,
            dataset_hash=child.snapshot_data["dataset"]["sha256"],
            dataset_revision=child.snapshot_data["dataset"]["revision"],
        )
        return snapshot
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_execution_snapshot_invalid") from None


class _ValidatedCapture:
    __slots__ = ("_active", "_counts")

    def __init__(self, parent_count, route_count, generated_count):
        self._active = True
        self._counts = (parent_count, route_count, generated_count)

    def summary(self):
        if not self._active:
            raise RuntimeError("service_insertion_context_closed")
        return dict(
            zip(("parent_count", "route_count", "generated_count"), self._counts, strict=True)
        )

    def _close(self):
        self._active = False
        self._counts = None


@asynccontextmanager
async def authenticated_initial_capture(request, child_id):
    """Validate an ephemeral candidate inside one new authenticated transaction.

    UUIDs and evidence never leave this context through its interface. They are not
    persisted or stable across retries. This helper does not approve or execute.
    """
    async with request.app.state.database.transaction() as db:
        child, result, parents, routes, generated, baseline = await _capture_initial_evidence(
            request, db, child_id
        )
        snapshot = _assemble_initial_snapshot(child, result, parents, routes, generated)
        capture = _ValidatedCapture(len(parents), len(routes), len(generated))
        del child, result, parents, routes, generated, baseline, snapshot
        try:
            yield capture
        finally:
            capture._close()
