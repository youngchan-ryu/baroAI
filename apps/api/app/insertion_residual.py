"""Internal residual-ticket evidence; callers must hold the dispatch gate.

This does not authorize insertion or attest an external system. Evidence comes from
locked application DB state, not the proposed solver result.
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.insertion_snapshot import SnapshotModel


class ResidualEvidence(SnapshotModel):
    ticket_id: UUID
    previous_assignment_id: UUID | None
    source_plan_id: UUID
    release_at: AwareDatetime
    deadline_at: AwareDatetime
    service_min: int = Field(gt=0, strict=True)
    availability: Literal["available", "external", "report", "unavailable", "terminal"]

    @model_validator(mode="after")
    def valid_timing(self):
        if self.deadline_at < self.release_at:
            raise ValueError("residual_timing_invalid")
        return self


class ResidualOutcome(SnapshotModel):
    ticket_id: UUID
    kind: Literal["assigned", "unassigned", "excluded", "terminal"]
    assignment_id: UUID | None = None
    reason: str | None = None


def validate_residual_consumption(evidence, outcomes):
    """Require one independently justified result for every prior residual ticket."""
    expected = {item.ticket_id: item for item in evidence}
    observed = {item.ticket_id: item for item in outcomes}
    if (
        len(expected) != len(evidence)
        or len(observed) != len(outcomes)
        or set(expected) != set(observed)
    ):
        raise ValueError("residual_coverage_invalid")
    for ticket_id, item in expected.items():
        result = observed[ticket_id]
        if item.availability == "terminal":
            valid = result.kind == "terminal" and result.reason == "terminal"
        elif item.availability != "available":
            valid = result.kind == "excluded" and result.reason == item.availability
        elif result.kind == "assigned":
            valid = result.assignment_id is not None and result.reason is None
        elif result.kind == "unassigned":
            valid = result.assignment_id is None and result.reason == "solver_unassigned"
        else:
            valid = result.kind == "excluded" and result.reason == "planning_data_unconfirmed"
        if not valid or (result.kind != "assigned" and result.assignment_id is not None):
            raise ValueError("residual_outcome_invalid")


# Fixed server codes only; neither user text nor report contents belong in the ledger.
RESIDUAL_REASONS = {
    "assigned": frozenset({"solver_assigned"}),
    "unassigned": frozenset({"solver_unassigned"}),
    "external": frozenset({"external_assignment"}),
    "report": frozenset({"pending_report"}),
    "unavailable": frozenset({"ticket_unavailable"}),
    "planning_data": frozenset({"planning_data_unconfirmed"}),
    "terminal": frozenset({"ticket_terminal"}),
}


def residual_snapshot_row(row):
    """Canonical, non-sensitive provenance; report text and token hashes never appear."""
    return {
        "version": row.version,
        "id": str(row.id),
        "plan_id": str(row.plan_id),
        "ticket_id": str(row.ticket_id),
        "source_plan_id": str(row.source_plan_id),
        "origin_kind": row.origin_kind,
        "previous_residual_id": str(row.previous_residual_id) if row.previous_residual_id else None,
        "release_at": row.release_at.isoformat(),
        "deadline_at": row.deadline_at.isoformat(),
        "service_min": row.service_min,
        "previous_assignment_id": str(row.previous_assignment_id)
        if row.previous_assignment_id
        else None,
        "previous_disposition_id": str(row.previous_disposition_id)
        if row.previous_disposition_id
        else None,
        "outcome": row.outcome,
        "reason": row.reason,
        "assigned_assignment_id": str(row.assigned_assignment_id)
        if row.assigned_assignment_id
        else None,
        "external_assignment_id": str(row.external_assignment_id)
        if row.external_assignment_id
        else None,
        "external_execution_revision": row.external_execution_revision,
        "report_id": str(row.report_id) if row.report_id else None,
        "report_revision": row.report_revision,
        "terminal_status": row.terminal_status,
        "terminal_updated_at": row.terminal_updated_at.isoformat()
        if row.terminal_updated_at
        else None,
        "terminal_event_id": str(row.terminal_event_id) if row.terminal_event_id else None,
    }


def validate_residual_lineage(*, plan_id, parent_id, rows, parent_rows, new_ticket_ids, snapshot):
    """Compare a locked DB candidate with its direct parent and its saved JSON snapshot.

    Callers independently establish source records, ticket states, and new-ticket
    completeness under the gate; this pure check does not attest those facts.
    """
    previous = {row.ticket_id: row for row in parent_rows}
    current = {row.ticket_id: row for row in rows}
    new_ids = set(new_ticket_ids)
    if len(previous) != len(parent_rows) or len(current) != len(rows):
        raise ValueError("residual_lineage_scope_invalid")
    inherited = {
        ticket_id: row
        for ticket_id, row in previous.items()
        if row.outcome != "assigned" or ticket_id in new_ids
    }
    truly_new = new_ids - set(previous)
    if set(current) != set(inherited) | truly_new:
        raise ValueError("residual_lineage_scope_invalid")
    for ticket_id, row in current.items():
        if (
            row.plan_id != plan_id
            or row.version != 1
            or row.reason not in RESIDUAL_REASONS.get(row.outcome, ())
        ):
            raise ValueError("residual_lineage_identity_invalid")
        previous = inherited.get(ticket_id)
        if previous:
            if (
                previous.plan_id != parent_id
                or row.previous_residual_id != previous.id
                or row.source_plan_id != previous.source_plan_id
                or row.origin_kind != previous.origin_kind
                or row.release_at != previous.release_at
                or row.deadline_at != previous.deadline_at
                or row.service_min != previous.service_min
                or row.previous_assignment_id != previous.previous_assignment_id
                or row.previous_disposition_id != previous.previous_disposition_id
            ):
                raise ValueError("residual_lineage_origin_invalid")
        elif row.previous_residual_id is not None:
            raise ValueError("residual_lineage_origin_invalid")
    canonical = [
        residual_snapshot_row(row) for row in sorted(rows, key=lambda item: str(item.ticket_id))
    ]
    if canonical != snapshot:
        raise ValueError("residual_snapshot_mismatch")


def validate_residual_origin(row, source, source_parent, parent_ticket_ids):
    """Check origin against immutable source input or the exact first urgent request."""
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from app.dispatch_schemas import InsertionCreate
    from app.insertion import offset
    from app.planning import PlanningInput

    problem = PlanningInput.model_validate_json(json.dumps(source.input_data))
    specs = [item for item in problem.tickets if item.id == str(row.ticket_id)]
    if row.origin_kind == "initial_input":
        if source.kind != "initial" or source.parent_plan_id is not None or len(specs) != 1:
            raise ValueError("residual_origin_invalid")
        origin = SimpleNamespace(
            release_min=specs[0].release_min,
            deadline_min=specs[0].deadline_min,
            service_min=specs[0].service_min,
        )
        base = source.plan_start_at
    elif row.origin_kind == "insertion_urgent":
        if (
            source.kind != "insertion"
            or not source_parent
            or source.parent_plan_id != source_parent.id
        ):
            raise ValueError("residual_origin_invalid")
        request = InsertionCreate.model_validate(source.request_data)
        matches = [item for item in request.urgent_tickets if item.ticket_id == row.ticket_id]
        if len(matches) != 1 or row.ticket_id in parent_ticket_ids:
            raise ValueError("residual_origin_invalid")
        origin, base = matches[0], source_parent.plan_start_at
        if len(specs) > 1:
            raise ValueError("residual_origin_invalid")
        if specs:
            spec = specs[0]
            if (
                spec.release_min
                != offset(source.plan_start_at, base + timedelta(minutes=origin.release_min))
                or spec.deadline_min
                != offset(source.plan_start_at, base + timedelta(minutes=origin.deadline_min))
                or spec.service_min != origin.service_min
            ):
                raise ValueError("residual_origin_input_invalid")
        else:
            exclusions = [
                item for item in source.excluded if item.get("ticket_id") == str(row.ticket_id)
            ]
            preserved = [
                item
                for item in source.insertion_data.get("preserved_diff", [])
                if item.get("ticket_id") == str(row.ticket_id)
            ]
            if (
                len(exclusions) != 1
                or len(preserved) != 1
                or preserved[0].get("kind") != "unassigned"
            ):
                raise ValueError("residual_origin_exclusion_invalid")
        timing = source.insertion_data.get("task_timing", {}).get(str(row.ticket_id))
        if timing is not None:
            if (
                timing.get("source_plan_id") != str(source_parent.id)
                or timing.get("release_at")
                != (base + timedelta(minutes=origin.release_min)).isoformat()
                or timing.get("deadline_at")
                != (base + timedelta(minutes=origin.deadline_min)).isoformat()
                or timing.get("service_min") != origin.service_min
            ):
                raise ValueError("residual_origin_timing_invalid")
    else:
        raise ValueError("residual_origin_kind_invalid")
    if (
        row.release_at != base + timedelta(minutes=origin.release_min)
        or row.deadline_at != base + timedelta(minutes=origin.deadline_min)
        or row.service_min != origin.service_min
    ):
        raise ValueError("residual_origin_timing_invalid")


async def save_initial_residuals(db, parent, child):
    """Persist residual evidence for an initial or first-child parent.

    The dispatch gate and relevant ticket locks must already be held. Any missing
    or contradictory evidence aborts the entire approval; no legacy backfill occurs.
    """
    import json
    from datetime import timedelta
    from uuid import UUID, uuid4

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.dispatch_schemas import InsertionCreate
    from app.models import (
        Assignment,
        CompletionReport,
        DispatchPlan,
        ServicePlanDisposition,
        ServicePlanExecutionEntry,
        ServicePlanResidual,
        Ticket,
        TicketEvent,
    )
    from app.planning import PlanningInput, PlanResult

    try:
        if parent.kind == "initial" and parent.parent_plan_id is None:
            root, inherited = parent, {}
        elif parent.kind == "insertion":
            inherited = await load_first_parent_residuals(db, parent)
            root = parent
            seen = set()
            while root.kind == "insertion":
                if root.id in seen or not root.parent_plan_id:
                    raise ValueError("unsupported_residual_parent")
                seen.add(root.id)
                root = await db.get(DispatchPlan, root.parent_plan_id)
                if not root:
                    raise ValueError("unsupported_residual_parent")
            if root.kind != "initial" or root.parent_plan_id is not None:
                raise ValueError("unsupported_residual_parent")
        else:
            raise ValueError("unsupported_residual_parent")
        initial = PlanningInput.model_validate_json(json.dumps(root.input_data))
        previous = PlanResult.model_validate_json(json.dumps(parent.result_data))
        result = PlanResult.model_validate_json(json.dumps(child.result_data))
        request = InsertionCreate.model_validate(child.request_data)
        initial_specs = {UUID(item.id): item for item in initial.tickets}
        urgent = {item.ticket_id: item for item in request.urgent_tickets}
        if len(initial_specs) != len(initial.tickets) or len(urgent) != len(request.urgent_tickets):
            raise ValueError("residual_input_duplicate")
        previous_ids = (
            {UUID(item.ticket_id) for item in previous.unassigned}
            if parent.kind == "initial"
            else {tid for tid, row in inherited.items() if row.outcome != "assigned"}
        )
        result_ids = {UUID(item.ticket_id) for item in result.unassigned}
        excluded = {
            UUID(item["ticket_id"])
            for item in child.insertion_data["preserved_diff"]
            if item.get("kind") == "unassigned"
        }
        ids = previous_ids | result_ids | excluded
        entries = list(
            (
                await db.scalars(
                    select(ServicePlanExecutionEntry).where(
                        ServicePlanExecutionEntry.plan_id == child.id
                    )
                )
            ).all()
        )
        assigned = {}
        for entry in entries:
            assignment = await db.get(Assignment, entry.assignment_id)
            if not assignment or assignment.ticket_id in assigned:
                raise ValueError("residual_entry_duplicate")
            assigned[assignment.ticket_id] = assignment.id
        dispositions = list(
            (
                await db.scalars(
                    select(ServicePlanDisposition).where(
                        ServicePlanDisposition.plan_id == child.id,
                        ServicePlanDisposition.classification == "unassigned",
                    )
                )
            ).all()
        )
        by_ticket = {}
        for disposition in dispositions:
            assignment = await db.get(Assignment, disposition.parent_assignment_id)
            if not assignment or assignment.ticket_id in by_ticket:
                raise ValueError("residual_disposition_duplicate")
            by_ticket[assignment.ticket_id] = disposition
        rows = []
        for tid in sorted(ids, key=str):
            ticket = await db.get(Ticket, tid, populate_existing=True)
            if not ticket:
                raise ValueError("residual_ticket_missing")
            previous_row = inherited.get(tid)
            disposition = by_ticket.get(tid)
            if previous_row:
                source = previous_row.source_plan_id
                origin = previous_row.origin_kind
                release_at, deadline_at, service_min = (
                    previous_row.release_at,
                    previous_row.deadline_at,
                    previous_row.service_min,
                )
                previous_assignment_id = previous_row.previous_assignment_id
                previous_disposition_id = previous_row.previous_disposition_id
            elif tid in initial_specs:
                source, spec, base, origin = (
                    root.id,
                    initial_specs[tid],
                    root.plan_start_at,
                    "initial_input",
                )
            elif tid in urgent:
                source, spec, base, origin = (
                    child.id,
                    urgent[tid],
                    parent.plan_start_at,
                    "insertion_urgent",
                )
            elif parent.kind == "insertion":
                source_plan = parent
                matches = []
                while source_plan.kind == "insertion":
                    source_request = InsertionCreate.model_validate(source_plan.request_data)
                    found = [
                        item for item in source_request.urgent_tickets if item.ticket_id == tid
                    ]
                    if found:
                        matches.extend((source_plan, item) for item in found)
                    source_plan = await db.get(DispatchPlan, source_plan.parent_plan_id)
                    if not source_plan:
                        raise ValueError("residual_source_missing")
                if len(matches) != 1:
                    raise ValueError("residual_source_missing")
                source_plan, spec = matches[0]
                source_parent = await db.get(DispatchPlan, source_plan.parent_plan_id)
                if not source_parent:
                    raise ValueError("residual_source_missing")
                source, spec, base, origin = (
                    source_plan.id,
                    spec,
                    source_parent.plan_start_at,
                    "insertion_urgent",
                )
            else:
                raise ValueError("residual_source_missing")
            if not previous_row:
                release_at = base + timedelta(minutes=spec.release_min)
                deadline_at = base + timedelta(minutes=spec.deadline_min)
                service_min = spec.service_min
                previous_assignment_id = disposition.parent_assignment_id if disposition else None
                previous_disposition_id = disposition.id if disposition else None
            row = ServicePlanResidual(
                id=uuid4(),
                version=1,
                plan_id=child.id,
                ticket_id=tid,
                source_plan_id=source,
                origin_kind=origin,
                previous_residual_id=previous_row.id if previous_row else None,
                release_at=release_at,
                deadline_at=deadline_at,
                service_min=service_min,
                previous_assignment_id=previous_assignment_id,
                previous_disposition_id=previous_disposition_id,
            )
            external = list(
                (
                    await db.scalars(
                        select(Assignment).where(
                            Assignment.ticket_id == tid,
                            Assignment.revoked.is_(False),
                            Assignment.visit_ended_at.is_(None),
                            Assignment.active_plan_id.is_distinct_from(child.id),
                        )
                    )
                ).all()
            )
            reports = list(
                (
                    await db.scalars(
                        select(CompletionReport).where(
                            CompletionReport.ticket_id == tid,
                            CompletionReport.reviewed.is_(False),
                            CompletionReport.superseded_at.is_(None),
                            CompletionReport.analysis_status != "dismissed",
                        )
                    )
                ).all()
            )
            if ticket.status in {"completed", "merged", "cancelled", "rejected", "transferred"}:
                event = await db.scalar(
                    select(TicketEvent)
                    .where(
                        TicketEvent.ticket_id == tid,
                        TicketEvent.to_status.is_not(None),
                    )
                    .order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc())
                    .limit(1)
                )
                if not event or event.to_status != ticket.status:
                    raise ValueError("residual_terminal_event_missing")
                row.outcome, row.reason = "terminal", "ticket_terminal"
                row.terminal_status, row.terminal_updated_at, row.terminal_event_id = (
                    ticket.status,
                    ticket.updated_at,
                    event.id,
                )
            elif external:
                if len(external) != 1:
                    raise ValueError("residual_external_ambiguous")
                row.outcome, row.reason = "external", "external_assignment"
                row.external_assignment_id = external[0].id
                row.external_execution_revision = external[0].execution_revision
            elif reports:
                if len(reports) != 1:
                    raise ValueError("residual_report_ambiguous")
                row.outcome, row.reason = "report", "pending_report"
                row.report_id, row.report_revision = reports[0].id, reports[0].revision
            elif tid in assigned:
                row.outcome, row.reason = "assigned", "solver_assigned"
                row.assigned_assignment_id = assigned[tid]
            elif tid in result_ids:
                row.outcome, row.reason = "unassigned", "solver_unassigned"
            elif ticket.status != "confirmed":
                row.outcome, row.reason = "unavailable", "ticket_unavailable"
            else:
                # Provider-based planning-data exclusions are not yet independently verified.
                raise ValueError("residual_exclusion_unverified")
            db.add(row)
            rows.append(row)
        child.snapshot_data = child.snapshot_data | {
            "service_residuals": [residual_snapshot_row(row) for row in rows]
        }
        await db.flush()
        await validate_locked_residual_records(db, child.id)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_insertion_residual_invalid") from None


async def _load_first_parent_residuals(db, parent):
    """Verify first-child historical ledger independently of present availability."""
    import json

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.models import (
        Assignment,
        CompletionReport,
        DispatchPlan,
        PlanMembership,
        ServicePlanDisposition,
        ServicePlanExecutionEntry,
        ServicePlanResidual,
        TicketEvent,
    )
    from app.planning import PlanningInput, PlanResult, input_hash, validate_plan

    try:
        root = await db.get(DispatchPlan, parent.parent_plan_id)
        if not root or root.kind != "initial" or root.parent_plan_id is not None:
            raise ValueError("unsupported_residual_parent")
        initial = PlanningInput.model_validate_json(json.dumps(root.input_data))
        initial_result = PlanResult.model_validate_json(json.dumps(root.result_data))
        problem = PlanningInput.model_validate_json(json.dumps(parent.input_data))
        result = PlanResult.model_validate_json(json.dumps(parent.result_data))
        for plan, inputs, output in ((root, initial, initial_result), (parent, problem, result)):
            if input_hash(inputs) != plan.input_hash or output.input_hash != plan.input_hash:
                raise ValueError("residual_parent_hash_invalid")
            validate_plan(inputs, output)
        rows = list(
            (
                await db.scalars(
                    select(ServicePlanResidual)
                    .where(ServicePlanResidual.plan_id == parent.id)
                    .order_by(ServicePlanResidual.ticket_id)
                )
            ).all()
        )
        by_ticket = {row.ticket_id: row for row in rows}
        if len(by_ticket) != len(rows) or parent.snapshot_data.get("service_residuals") != [
            residual_snapshot_row(row) for row in rows
        ]:
            raise ValueError("residual_parent_snapshot_invalid")
        expected = {UUID(item.ticket_id) for item in initial_result.unassigned}
        expected |= {UUID(item.ticket_id) for item in result.unassigned}
        expected |= {
            UUID(item["ticket_id"])
            for item in parent.insertion_data["preserved_diff"]
            if item.get("kind") == "unassigned"
        }
        if set(by_ticket) != expected:
            raise ValueError("residual_parent_scope_invalid")
        root_ids = {UUID(item.id) for item in initial.tickets}
        result_routes = {
            UUID(visit.ticket_id): route.worker_id
            for route in result.routes
            for visit in route.visits
        }
        for row in rows:
            source = (
                root
                if row.source_plan_id == root.id
                else parent
                if row.source_plan_id == parent.id
                else None
            )
            if not source or row.previous_residual_id is not None:
                raise ValueError("residual_parent_source_invalid")
            validate_residual_origin(row, source, root if source is parent else None, root_ids)
            if row.reason not in RESIDUAL_REASONS.get(row.outcome, ()):
                raise ValueError("residual_parent_outcome_invalid")
            if row.previous_assignment_id:
                assignment = await db.get(Assignment, row.previous_assignment_id)
                disposition = await db.get(ServicePlanDisposition, row.previous_disposition_id)
                member = (
                    await db.get(PlanMembership, disposition.parent_membership_id)
                    if disposition
                    else None
                )
                if (
                    not assignment
                    or assignment.ticket_id != row.ticket_id
                    or not disposition
                    or disposition.plan_id != parent.id
                    or disposition.parent_assignment_id != assignment.id
                    or disposition.classification != "unassigned"
                    or disposition.replacement_assignment_id is not None
                    or not member
                    or member.plan_id != root.id
                    or member.assignment_id != assignment.id
                ):
                    raise ValueError("residual_parent_disposition_invalid")
            if row.outcome == "assigned":
                assignment = await db.get(Assignment, row.assigned_assignment_id)
                entry = await db.scalar(
                    select(ServicePlanExecutionEntry).where(
                        ServicePlanExecutionEntry.plan_id == parent.id,
                        ServicePlanExecutionEntry.assignment_id == row.assigned_assignment_id,
                    )
                )
                if (
                    not assignment
                    or assignment.ticket_id != row.ticket_id
                    or not entry
                    or (result_routes.get(row.ticket_id) != str(entry.worker_id))
                ):
                    raise ValueError("residual_parent_entry_invalid")
            elif row.ticket_id in result_routes:
                raise ValueError("residual_parent_result_invalid")
            elif row.outcome == "unassigned" and row.ticket_id not in {
                UUID(item.ticket_id) for item in result.unassigned
            }:
                raise ValueError("residual_parent_result_invalid")
            elif row.outcome == "terminal":
                event = await db.get(TicketEvent, row.terminal_event_id)
                if (
                    not event
                    or event.ticket_id != row.ticket_id
                    or event.to_status != row.terminal_status
                ):
                    raise ValueError("residual_parent_terminal_invalid")
            elif row.outcome not in {"unassigned", "assigned"}:
                matches = [
                    item for item in parent.excluded if item.get("ticket_id") == str(row.ticket_id)
                ]
                preserved = [
                    item
                    for item in parent.insertion_data["preserved_diff"]
                    if item.get("ticket_id") == str(row.ticket_id)
                    and item.get("kind") == "unassigned"
                ]
                if (
                    len(matches) != 1
                    or len(preserved) != 1
                    or matches[0].get("reason") != preserved[0].get("reason")
                ):
                    raise ValueError("residual_parent_exclusion_invalid")
                if row.outcome == "external":
                    external = await db.get(Assignment, row.external_assignment_id)
                    if (
                        not external
                        or external.ticket_id != row.ticket_id
                        or external.execution_revision < row.external_execution_revision
                    ):
                        raise ValueError("residual_parent_external_invalid")
                elif row.outcome == "report":
                    report = await db.get(CompletionReport, row.report_id)
                    assignment = await db.get(Assignment, report.assignment_id) if report else None
                    if (
                        not report
                        or report.ticket_id != row.ticket_id
                        or report.revision < row.report_revision
                        or not assignment
                        or assignment.ticket_id != row.ticket_id
                    ):
                        raise ValueError("residual_parent_report_invalid")
        return by_ticket
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_insertion_residual_invalid") from None


async def load_first_parent_residuals(db, parent):
    """Validate the residual ledger from its first child through the requested parent."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.models import DispatchPlan, ServicePlanResidual

    lineage = []
    seen = set()
    cursor = parent
    while cursor and cursor.kind == "insertion":
        if cursor.id in seen or not cursor.parent_plan_id:
            raise HTTPException(409, "service_insertion_residual_invalid")
        seen.add(cursor.id)
        lineage.append(cursor)
        cursor = await db.get(DispatchPlan, cursor.parent_plan_id)
    if not cursor or cursor.kind != "initial" or cursor.parent_plan_id or not lineage:
        raise HTTPException(409, "service_insertion_residual_invalid")
    lineage.reverse()
    rows = await _load_first_parent_residuals(db, lineage[0])
    for plan in lineage[1:]:
        await validate_locked_residual_records(db, plan.id, historical=True)
        values = list(
            (
                await db.scalars(
                    select(ServicePlanResidual)
                    .where(ServicePlanResidual.plan_id == plan.id)
                    .order_by(ServicePlanResidual.ticket_id)
                )
            ).all()
        )
        rows = {row.ticket_id: row for row in values}
    return rows


async def validate_locked_residual_records(db, plan_id, *, historical=False):
    """Validate stored candidate evidence under the gate; internal prototype only.

    Call only after current dispatcher authorization and CSRF in this transaction.
    Current availability is intentionally checked, so this is not a historical
    execution validator. A missing residual snapshot fails closed.
    """
    import json

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.dispatch_lock import dispatch_lock
    from app.dispatch_schemas import InsertionCreate
    from app.insertion_evidence import require_clean_session
    from app.models import (
        Assignment,
        CompletionReport,
        DispatchPlan,
        PlanMembership,
        ServicePlanDisposition,
        ServicePlanExecutionEntry,
        ServicePlanResidual,
        Ticket,
        TicketEvent,
    )
    from app.planning import PlanningInput, PlanResult, input_hash, validate_plan

    require_clean_session(db)
    await dispatch_lock(db)
    with db.no_autoflush:
        try:
            plan = await db.get(DispatchPlan, plan_id, populate_existing=True)
            if not plan or not plan.parent_plan_id:
                raise ValueError("residual_plan_invalid")
            rows = tuple(
                (
                    await db.scalars(
                        select(ServicePlanResidual)
                        .where(ServicePlanResidual.plan_id == plan_id)
                        .order_by(ServicePlanResidual.ticket_id)
                    )
                ).all()
            )
            parent_rows = tuple(
                (
                    await db.scalars(
                        select(ServicePlanResidual).where(
                            ServicePlanResidual.plan_id == plan.parent_plan_id
                        )
                    )
                ).all()
            )
            problem = PlanningInput.model_validate_json(json.dumps(plan.input_data))
            result = PlanResult.model_validate_json(json.dumps(plan.result_data))
            if input_hash(problem) != plan.input_hash or result.input_hash != plan.input_hash:
                raise ValueError("residual_plan_hash_invalid")
            validate_plan(problem, result)
            request = InsertionCreate.model_validate(plan.request_data)
            request_ids = [str(item.ticket_id) for item in request.urgent_tickets]
            if len(set(request_ids)) != len(request_ids):
                raise ValueError("residual_request_duplicate")
            parent = await db.get(DispatchPlan, plan.parent_plan_id)
            if not parent:
                raise ValueError("residual_parent_invalid")
            parent_problem = PlanningInput.model_validate_json(json.dumps(parent.input_data))
            parent_result = PlanResult.model_validate_json(json.dumps(parent.result_data))
            if (
                input_hash(parent_problem) != parent.input_hash
                or parent_result.input_hash != parent.input_hash
            ):
                raise ValueError("residual_parent_hash_invalid")
            validate_plan(parent_problem, parent_result)
            prior_unassigned = {UUID(item.ticket_id) for item in parent_result.unassigned}
            if parent.kind == "insertion":
                parent_snapshot = [
                    residual_snapshot_row(item)
                    for item in sorted(parent_rows, key=lambda item: str(item.ticket_id))
                ]
                if parent.snapshot_data.get("service_residuals") != parent_snapshot:
                    raise ValueError("residual_parent_snapshot_invalid")
                parent_excluded = {
                    UUID(item["ticket_id"])
                    for item in parent.insertion_data.get("preserved_diff", [])
                    if item.get("kind") == "unassigned" and item.get("ticket_id")
                }
                accounted = {item.ticket_id for item in parent_rows}
                if not (prior_unassigned | parent_excluded) <= accounted:
                    raise ValueError("residual_parent_ledger_incomplete")
            elif parent.kind != "initial":
                raise ValueError("residual_parent_invalid")
            result_routes = {
                visit.ticket_id: route.worker_id
                for route in result.routes
                for visit in route.visits
            }
            result_unassigned = {item.ticket_id for item in result.unassigned}
            input_ids = {item.id for item in problem.tickets}
            excluded_new = set()
            for tid in request_ids:
                if tid not in input_ids:
                    matches = [item for item in plan.excluded if item.get("ticket_id") == tid]
                    if len(matches) != 1:
                        raise ValueError("residual_request_exclusion_invalid")
                    excluded_new.add(tid)
            parent_ids = {item.ticket_id for item in parent_rows if item.outcome != "assigned"}
            required = {UUID(tid) for tid in result_unassigned | excluded_new}
            if parent.kind == "initial":
                required |= prior_unassigned
            new_ticket_ids = required - parent_ids
            validate_residual_lineage(
                plan_id=plan_id,
                parent_id=plan.parent_plan_id,
                rows=rows,
                parent_rows=parent_rows,
                new_ticket_ids=new_ticket_ids,
                snapshot=plan.snapshot_data["service_residuals"],
            )
            parent_by_ticket = {item.ticket_id: item for item in parent_rows}
            ancestors = set()
            cursor = plan
            while cursor.parent_plan_id:
                if cursor.id in ancestors:
                    raise ValueError("residual_ancestry_invalid")
                ancestors.add(cursor.id)
                cursor = await db.get(DispatchPlan, cursor.parent_plan_id)
                if not cursor:
                    raise ValueError("residual_ancestry_invalid")
            ancestors.add(cursor.id)
            for row in rows:
                prior = parent_by_ticket.get(row.ticket_id)
                if prior and prior.outcome == "assigned" and row.outcome == "unassigned":
                    previous_assignment = await db.get(Assignment, prior.assigned_assignment_id)
                    previous_entry = await db.scalar(
                        select(ServicePlanExecutionEntry).where(
                            ServicePlanExecutionEntry.plan_id == parent.id,
                            ServicePlanExecutionEntry.assignment_id == prior.assigned_assignment_id,
                        )
                    )
                    dispositions = list(
                        (
                            await db.scalars(
                                select(ServicePlanDisposition).where(
                                    ServicePlanDisposition.plan_id == plan.id,
                                    ServicePlanDisposition.parent_assignment_id
                                    == prior.assigned_assignment_id,
                                )
                            )
                        ).all()
                    )
                    current_entry = await db.scalar(
                        select(ServicePlanExecutionEntry.id).where(
                            ServicePlanExecutionEntry.plan_id == plan.id,
                            ServicePlanExecutionEntry.assignment_id == prior.assigned_assignment_id,
                        )
                    )
                    disposition = dispositions[0] if len(dispositions) == 1 else None
                    member = (
                        await db.get(PlanMembership, disposition.parent_membership_id)
                        if disposition
                        else None
                    )
                    if (
                        not previous_assignment
                        or previous_assignment.ticket_id != row.ticket_id
                        or not previous_entry
                        or not disposition
                        or disposition.classification != "unassigned"
                        or disposition.replacement_assignment_id is not None
                        or not member
                        or member.plan_id != parent.id
                        or member.assignment_id != prior.assigned_assignment_id
                        or current_entry
                    ):
                        raise ValueError("residual_reentry_disposition_invalid")
                source = await db.get(DispatchPlan, row.source_plan_id)
                ticket = await db.get(Ticket, row.ticket_id)
                if not source or source.id not in ancestors or not ticket:
                    raise ValueError("residual_source_invalid")
                source_problem = PlanningInput.model_validate_json(json.dumps(source.input_data))
                source_result = PlanResult.model_validate_json(json.dumps(source.result_data))
                if (
                    input_hash(source_problem) != source.input_hash
                    or source_result.input_hash != source.input_hash
                ):
                    raise ValueError("residual_source_hash_invalid")
                validate_plan(source_problem, source_result)
                source_parent = (
                    await db.get(DispatchPlan, source.parent_plan_id)
                    if source.parent_plan_id
                    else None
                )
                parent_ticket_ids = set()
                if source_parent:
                    parent_ticket_ids |= {
                        UUID(item["id"]) for item in source_parent.input_data["tickets"]
                    }
                    parent_ticket_ids |= {
                        UUID(tid) for tid in source_parent.insertion_data.get("task_timing", {})
                    }
                    parent_entries = list(
                        (
                            await db.scalars(
                                select(ServicePlanExecutionEntry).where(
                                    ServicePlanExecutionEntry.plan_id == source_parent.id
                                )
                            )
                        ).all()
                    )
                    memberships = list(
                        (
                            await db.scalars(
                                select(PlanMembership).where(
                                    PlanMembership.plan_id == source_parent.id
                                )
                            )
                        ).all()
                    )
                    for membership in memberships:
                        member_assignment = await db.get(Assignment, membership.assignment_id)
                        if not member_assignment:
                            raise ValueError("residual_parent_assignment_invalid")
                        parent_ticket_ids.add(member_assignment.ticket_id)
                    parent_residuals = list(
                        (
                            await db.scalars(
                                select(ServicePlanResidual).where(
                                    ServicePlanResidual.plan_id == source_parent.id
                                )
                            )
                        ).all()
                    )
                    parent_ticket_ids |= {item.ticket_id for item in parent_residuals}
                    for entry in parent_entries:
                        parent_assignment = await db.get(Assignment, entry.assignment_id)
                        if not parent_assignment:
                            raise ValueError("residual_parent_assignment_invalid")
                        parent_ticket_ids.add(parent_assignment.ticket_id)
                validate_residual_origin(row, source, source_parent, parent_ticket_ids)
                if row.previous_assignment_id:
                    assignment = await db.get(Assignment, row.previous_assignment_id)
                    disposition = await db.get(ServicePlanDisposition, row.previous_disposition_id)
                    disposition_plan = (
                        await db.get(DispatchPlan, disposition.plan_id) if disposition else None
                    )
                    member = (
                        await db.get(PlanMembership, disposition.parent_membership_id)
                        if disposition
                        else None
                    )
                    if (
                        not assignment
                        or assignment.ticket_id != row.ticket_id
                        or not disposition
                        or disposition.parent_assignment_id != assignment.id
                        or disposition.classification != "unassigned"
                        or not member
                        or member.assignment_id != assignment.id
                        or not disposition_plan
                        or member.plan_id != disposition_plan.parent_plan_id
                        or disposition.plan_id not in ancestors
                        or (row.previous_residual_id is None and disposition.plan_id != row.plan_id)
                    ):
                        raise ValueError("residual_previous_assignment_invalid")
                tid = str(row.ticket_id)
                if historical and row.outcome in {"external", "report", "unavailable"}:
                    exclusions = [item for item in plan.excluded if item.get("ticket_id") == tid]
                    preserved = [
                        item
                        for item in plan.insertion_data["preserved_diff"]
                        if item.get("ticket_id") == tid and item.get("kind") == "unassigned"
                    ]
                    if (
                        len(exclusions) != 1
                        or len(preserved) != 1
                        or exclusions[0].get("reason") != preserved[0].get("reason")
                    ):
                        raise ValueError("residual_historical_exclusion_invalid")
                external_id = (
                    None
                    if historical
                    else await db.scalar(
                        select(Assignment.id)
                        .where(
                            Assignment.ticket_id == row.ticket_id,
                            Assignment.revoked.is_(False),
                            Assignment.visit_ended_at.is_(None),
                            Assignment.active_plan_id.is_distinct_from(plan_id),
                        )
                        .limit(1)
                    )
                )
                pending_id = (
                    None
                    if historical
                    else await db.scalar(
                        select(CompletionReport.id)
                        .where(
                            CompletionReport.ticket_id == row.ticket_id,
                            CompletionReport.reviewed.is_(False),
                            CompletionReport.superseded_at.is_(None),
                            CompletionReport.analysis_status != "dismissed",
                        )
                        .limit(1)
                    )
                )
                if row.outcome in {"assigned", "unassigned", "unavailable"} and (
                    external_id or pending_id
                ):
                    raise ValueError("residual_current_conflict")
                if row.outcome == "report" and external_id:
                    raise ValueError("residual_current_conflict")
                if row.outcome == "assigned":
                    assignment = await db.get(Assignment, row.assigned_assignment_id)
                    entry = await db.scalar(
                        select(ServicePlanExecutionEntry).where(
                            ServicePlanExecutionEntry.plan_id == plan_id,
                            ServicePlanExecutionEntry.assignment_id == row.assigned_assignment_id,
                        )
                    )
                    if (
                        not assignment
                        or assignment.ticket_id != row.ticket_id
                        or not entry
                        or tid not in result_routes
                        or result_routes[tid] != str(entry.worker_id)
                        or tid in result_unassigned
                    ):
                        raise ValueError("residual_assigned_invalid")
                elif tid in result_routes or (
                    row.outcome != "unassigned" and tid in result_unassigned
                ):
                    raise ValueError("residual_result_invalid")
                elif row.outcome == "unassigned":
                    if tid not in result_unassigned or (
                        not historical and ticket.status != "confirmed"
                    ):
                        raise ValueError("residual_unassigned_invalid")
                elif row.outcome == "planning_data":
                    # Provider-based availability validation is not wired yet.
                    raise ValueError("residual_planning_data_unverified")
                elif row.outcome == "unavailable":
                    if not historical and ticket.status in {
                        "confirmed",
                        "completed",
                        "merged",
                        "cancelled",
                        "rejected",
                        "transferred",
                    }:
                        raise ValueError("residual_unavailable_invalid")
                elif row.outcome == "external":
                    assignment = await db.get(Assignment, row.external_assignment_id)
                    if (
                        not assignment
                        or assignment.ticket_id != row.ticket_id
                        or (not historical and assignment.revoked)
                        or (not historical and assignment.visit_ended_at is not None)
                        or (not historical and assignment.active_plan_id == plan_id)
                        or assignment.execution_revision < row.external_execution_revision
                        or (
                            not historical
                            and assignment.execution_revision != row.external_execution_revision
                        )
                        or (not historical and not external_id)
                    ):
                        raise ValueError("residual_external_invalid")
                elif row.outcome == "report":
                    report = await db.get(CompletionReport, row.report_id)
                    report_assignment = (
                        await db.get(Assignment, report.assignment_id) if report else None
                    )
                    if (
                        not report
                        or report.ticket_id != row.ticket_id
                        or not report_assignment
                        or report_assignment.ticket_id != row.ticket_id
                        or (not historical and report.reviewed)
                        or (not historical and report.analysis_status == "dismissed")
                        or report.revision < row.report_revision
                        or (not historical and report.revision != row.report_revision)
                    ):
                        raise ValueError("residual_report_invalid")
                elif row.outcome == "terminal":
                    event = await db.get(TicketEvent, row.terminal_event_id)
                    latest = await db.scalar(
                        select(TicketEvent.id)
                        .where(
                            TicketEvent.ticket_id == row.ticket_id,
                            TicketEvent.to_status.is_not(None),
                        )
                        .order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc())
                        .limit(1)
                    )
                    if (
                        (not historical and ticket.status != row.terminal_status)
                        or (not historical and ticket.updated_at != row.terminal_updated_at)
                        or not event
                        or event.ticket_id != row.ticket_id
                        or event.to_status != row.terminal_status
                        or (not historical and latest != event.id)
                    ):
                        raise ValueError("residual_terminal_invalid")
            return len(rows)
        except (
            HTTPException,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            OverflowError,
        ):
            raise HTTPException(409, "service_insertion_residual_invalid") from None
