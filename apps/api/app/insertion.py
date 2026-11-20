"""DB-only urgent insertion adapter. The pure engine and original plan provenance stay intact."""

import json
import math
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_, select

from app.catalog import CATEGORY_MAP
from app.dispatch_schemas import InsertionCreate, PlanApprovalView
from app.dispatching import (
    CODES,
    DOMAINS,
    NODES,
    TRAVEL,
    assignments_view,
    capture,
    fingerprint,
    get_plan,
    is_day_off,
    is_day_off_during,
    lock_schedules,
    plan_view,
)
from app.duplicates import aggregate_safety, merge_version
from app.execution import pending_report, phase
from app.models import (
    Assignment,
    CompletionReport,
    DispatchPlan,
    PlanMembership,
    Ticket,
    User,
    WorkerSchedule,
)
from app.planning import (
    PlanningInput,
    PlanResult,
    TicketSpec,
    WorkerSpec,
    input_hash,
    validate_plan,
)
from app.security import now, token_digest
from app.tickets import transition


def at(base, minutes):
    return base + timedelta(minutes=minutes)


def offset(base, instant):
    return math.ceil((instant - base).total_seconds() / 60)


async def active_assignments(db, plan):
    return list(
        (
            await db.scalars(
                select(Assignment)
                .where(Assignment.active_plan_id == plan.id)
                .order_by(Assignment.worker_id, Assignment.id)
            )
        ).all()
    )


async def task_timing(db, parent, tid):
    # Each descendant carries the original absolute release/deadline, without repeated rounding.
    stored = parent.insertion_data.get("task_timing", {}).get(str(tid))
    if stored:
        return stored
    item = next((t for t in parent.input_data["tickets"] if t["id"] == str(tid)), None)
    if not item:
        raise HTTPException(409, "parent_task_timing_missing")
    return {
        "release_at": at(parent.plan_start_at, item["release_min"]).isoformat(),
        "deadline_at": at(parent.plan_start_at, item["deadline_min"]).isoformat(),
        "service_min": item["service_min"],
        "source_plan_id": str(parent.id),
    }


async def capture_insertion(
    db, parent, payload, reference_at, dispatch_not_before_at, *, service_context=None
):
    codes, nodes, travel = CODES, NODES, TRAVEL
    provider, dataset_revision = None, None
    parent_execution_snapshot = None
    parent_residuals = {}
    if service_context is not None:
        from app.planning.datasets import has_unverified_travel

        if has_unverified_travel(service_context[1]):
            raise HTTPException(503, "verified_planning_data_unavailable")
        if dispatch_not_before_at != reference_at + timedelta(minutes=2):
            raise HTTPException(422, "invalid_insertion_dispatch_bound")
        from app.dispatch_lock import dispatch_lock
        from app.insertion_service import (
            validate_first_child_as_parent,
            validate_initial_service_parent,
        )

        settings, provider, dataset_revision = service_context
        await dispatch_lock(db)
        await db.refresh(parent)
        if parent.kind == "initial":
            parent_input, _ = await validate_initial_service_parent(
                db, settings, provider, dataset_revision, parent.id
            )
        elif parent.kind == "insertion":
            parent_input, _, parent_execution_snapshot = await validate_first_child_as_parent(
                db, settings, provider, dataset_revision, parent.id
            )
            from app.insertion_residual import load_first_parent_residuals

            parent_residuals = await load_first_parent_residuals(db, parent)
        else:
            raise HTTPException(409, "service_insertion_parent_invalid")
        codes = list(parent_input.travel.nodes)
        nodes = [{"code": item.code, "region": item.region} for item in provider.dataset.locations]
        travel = parent_input.travel
    if parent.status != "approved" or parent.revision != payload.expected_parent_revision:
        raise HTTPException(409, "parent_plan_revision_conflict")

    def valid_location(ticket, code):
        if ticket.location_data.get("state") != "confirmed" or code not in codes:
            return False
        if provider is None:
            return True
        return (
            ticket.location_data.get("source_type") == "service"
            and ticket.location_data.get("dataset_hash") == provider.sha256
            and ticket.location_data.get("dataset_revision") == dataset_revision
        )

    if len({t.ticket_id for t in payload.urgent_tickets}) != len(payload.urgent_tickets):
        raise HTTPException(422, "duplicate_id")
    all_assignments = await active_assignments(db, parent)
    if any(a.stopped_at for a in all_assignments):
        raise HTTPException(409, "stopped_plan_requires_cancellation")
    parent_workers = {w["id"]: w for w in parent.input_data["workers"]}
    for a in all_assignments:
        if str(a.worker_id) not in parent_workers:
            # Excluded workers preserved by earlier insertions still belong to the full scope.
            old = parent.insertion_data.get("worker_specs", {}).get(str(a.worker_id))
            if old:
                parent_workers[str(a.worker_id)] = old
    if not parent_workers:
        raise HTTPException(409, "parent_has_no_workers")
    parent_ids = {t["id"] for t in parent.input_data["tickets"]}
    parent_ids |= set(parent.insertion_data.get("task_timing", {}))
    parent_ids |= {str(a.ticket_id) for a in all_assignments}
    parent_ids |= {str(tid) for tid, row in parent_residuals.items() if row.outcome != "assigned"}
    if parent_ids & {str(t.ticket_id) for t in payload.urgent_tickets}:
        raise HTTPException(422, "urgent_ticket_already_in_parent")
    # Reuse full queue/role/report/B15 fencing, without B16 busy filtering.
    from types import SimpleNamespace

    stub = SimpleNamespace(
        worker_ids=[uuid.UUID(w) for w in parent_workers],
        tickets=[],
        mode=parent.input_data["mode"],
        solver_seconds=payload.solver_seconds,
        assumptions_version=payload.assumptions_version,
        preset_id=provider.dataset.dataset_id if provider else "demo-campus-v1",
        dataset_hash=provider.sha256 if provider else None,
        dataset_revision=dataset_revision,
    )
    _, state, _ = await capture(db, stub, provider, dataset_revision)
    state["parent"] = [
        str(parent.id),
        parent.revision,
        parent.status,
        parent.input_hash,
        parent.insertion_data,
    ]
    if parent_residuals:
        from app.insertion_residual import residual_snapshot_row

        state["parent_residuals"] = [
            residual_snapshot_row(row) for row in parent_residuals.values()
        ]
    if provider is not None:
        memberships = list(
            (
                await db.scalars(
                    select(PlanMembership)
                    .where(PlanMembership.plan_id == parent.id)
                    .order_by(PlanMembership.id)
                )
            ).all()
        )
        evidence = {
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
                for m in memberships
            ],
        }
        if parent_execution_snapshot is not None:
            from app.models import ServicePlanDisposition, ServicePlanExecutionEntry

            entries = list(
                (
                    await db.scalars(
                        select(ServicePlanExecutionEntry)
                        .where(ServicePlanExecutionEntry.plan_id == parent.id)
                        .order_by(ServicePlanExecutionEntry.id)
                    )
                ).all()
            )
            dispositions = list(
                (
                    await db.scalars(
                        select(ServicePlanDisposition)
                        .where(ServicePlanDisposition.plan_id == parent.id)
                        .order_by(ServicePlanDisposition.id)
                    )
                ).all()
            )
            evidence["execution_entries"] = [
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
                for e in entries
            ]
            evidence["dispositions"] = [
                [
                    str(d.id),
                    str(d.parent_assignment_id),
                    str(d.parent_membership_id),
                    d.classification,
                    str(d.replacement_assignment_id),
                ]
                for d in dispositions
            ]
        state["parent_evidence"] = fingerprint(evidence)
    delta = max(
        0, -min([0] + [n for t in payload.urgent_tickets for n in (t.release_min, t.deadline_min)])
    )
    base = parent.plan_start_at - timedelta(minutes=delta)
    diff, excluded, workers, specs, timings, work_items = [], [], [], {}, {}, {}
    eligible_workers = set()
    fixed_by_worker = {}
    for wid, original in parent_workers.items():
        user = await db.get(User, uuid.UUID(wid))
        schedule = await db.get(WorkerSchedule, uuid.UUID(wid))
        owned = [
            a
            for a in all_assignments
            if str(a.worker_id) == wid and (not a.revoked or (provider and a.visit_ended_at))
        ]
        ongoing = [
            a for a in owned if phase(a, await db.get(Ticket, a.ticket_id)) in {"moving", "working"}
        ]
        assignments_condition = (
            or_(Assignment.revoked.is_(False), Assignment.active_plan_id.is_not(None))
            if provider is not None
            else Assignment.revoked.is_(False)
        )
        all_worker_assignments = list(
            (
                await db.scalars(
                    select(Assignment).where(
                        Assignment.worker_id == uuid.UUID(wid), assignments_condition
                    )
                )
            ).all()
        )
        if provider is not None:
            for assignment in all_worker_assignments:
                related_ticket = await db.get(Ticket, assignment.ticket_id)
                if related_ticket.status == "completed" and assignment.visit_ended_at is None:
                    raise HTTPException(409, "service_visit_end_evidence_missing")
        external = [
            a
            for a in all_worker_assignments
            if a.active_plan_id != parent.id
            and (
                a.visit_ended_at is None
                if provider is not None
                else phase(a, await db.get(Ticket, a.ticket_id)) != "visit_ended"
            )
        ]
        pending = any([bool(await pending_report(db, a)) for a in all_worker_assignments])
        reason = (
            "worker_role_changed"
            if not user or user.role != "worker"
            else "schedule_unavailable"
            if not schedule or not schedule.data.get("enabled")
            else "worker_day_off"
            if is_day_off(schedule.data, base)
            else "external_assignment"
            if external
            else "report_pending_review"
            if pending
            else "multiple_ongoing_visits"
            if len(ongoing) > 1
            else None
        )
        if not reason and any(a.auth_epoch != user.auth_epoch for a in owned):
            reason = "worker_assignment_epoch_changed"
        if not reason:
            for queued in owned:
                qt = await db.get(Ticket, queued.ticket_id)
                if phase(queued, qt) == "visit_ended":
                    continue
                qc = (qt.location_data.get("selected") or {}).get("code")
                qd = CATEGORY_MAP.get(qt.category_code, {}).get("domain")
                if not valid_location(qt, qc) or qd not in DOMAINS:
                    reason = "planning_data_unconfirmed_queue_preserved"
                    break
        start_node = original["start_node"]
        available = max(original["shift_start_min"] + delta, offset(base, dispatch_not_before_at))
        if ongoing:
            fixed = ongoing[0]
            fixed_by_worker[wid] = str(fixed.id)
            data = fixed.remaining_data
            if provider is not None:
                if not isinstance(data, dict):
                    raise HTTPException(409, "service_remaining_evidence_invalid")
                if data:
                    from app.insertion_service import validate_service_remaining

                    approved_code = None
                    if parent_execution_snapshot is not None:
                        origins = [
                            e.origin.location_code
                            for path in parent_execution_snapshot.workers
                            for e in path.entries
                            if e.origin.assignment_id == fixed.id
                        ]
                        if len(origins) != 1:
                            raise HTTPException(409, "service_insertion_parent_invalid")
                        approved_code = origins[0]
                    await validate_service_remaining(
                        db, fixed, data, parent_input, reference_at, approved_code=approved_code
                    )
            if not reason:
                if not data:
                    reason = "remaining_unconfirmed"
                elif reference_at >= datetime.fromisoformat(data["expires_at"]) or (
                    reference_at >= datetime.fromisoformat(data["expected_end_at"])
                ):
                    reason = "remaining_reconfirmation_required"
                elif data.get("location_code") not in codes:
                    reason = "execution_location_unconfirmed"
                else:
                    start_node = codes.index(data["location_code"])
                    available = max(
                        available, offset(base, datetime.fromisoformat(data["expected_end_at"]))
                    )
        else:
            ended = [
                a for a in owned if phase(a, await db.get(Ticket, a.ticket_id)) == "visit_ended"
            ]
            if ended:
                ended.sort(key=lambda a: a.visit_ended_at or reference_at)
                last = ended[-1]
                if last.execution_location_code not in codes:
                    reason = reason or "last_visit_location_unconfirmed"
                else:
                    start_node = codes.index(last.execution_location_code)
        spec = dict(original)
        spec.update(
            start_node=start_node,
            shift_start_min=available,
            shift_end_min=original["shift_end_min"] + delta,
        )
        if lunch := original.get("lunch_min"):
            lunch_start, lunch_end = (value + delta for value in lunch)
            if lunch_start <= available < lunch_end:
                available = lunch_end
                spec["shift_start_min"] = available
            if available < lunch_start:
                spec["lunch_min"] = [lunch_start, lunch_end]
            else:
                spec.pop("lunch_min")
        specs[wid] = spec
        if not reason and spec["shift_end_min"] <= available:
            reason = "outside_shift"
        if reason:
            excluded.append({"worker_id": wid, "reason": reason})
        else:
            eligible_workers.add(wid)
            try:
                workers.append(WorkerSpec.model_validate_json(json.dumps(spec)))
            except ValidationError:
                raise HTTPException(422, "insertion_time_range_exceeded") from None
    for tid in sorted(parent_ids):
        t = await db.get(Ticket, uuid.UUID(tid))
        if not t:
            raise HTTPException(409, "parent_ticket_missing")
        state["tickets"][tid] = await merge_version(db, t)
        old = next(
            (
                a
                for a in all_assignments
                if str(a.ticket_id) == tid and (not a.revoked or (provider and a.visit_ended_at))
            ),
            None,
        )
        if old and phase(old, t) == "visit_ended":
            diff.append(
                {
                    "ticket_id": tid,
                    "kind": "retained",
                    "original_assignment_id": str(old.id),
                    "worker_id": str(old.worker_id),
                    "reason": "visit_ended_followup_required"
                    if t.status != "completed"
                    else "visit_completed",
                }
            )
            continue
        if t.status in {"completed", "merged", "cancelled", "rejected", "transferred"}:
            continue
        if old is None:
            external_active = await db.scalar(
                select(Assignment.id)
                .where(
                    Assignment.ticket_id == t.id,
                    Assignment.revoked.is_(False),
                    Assignment.visit_ended_at.is_(None),
                )
                .limit(1)
            )
            unresolved = await db.scalar(
                select(CompletionReport.id)
                .where(
                    CompletionReport.ticket_id == t.id,
                    CompletionReport.reviewed.is_(False),
                    CompletionReport.superseded_at.is_(None),
                    CompletionReport.analysis_status != "dismissed",
                )
                .limit(1)
            )
            if t.status != "confirmed" or external_active or unresolved:
                excluded.append(
                    {
                        "ticket_id": tid,
                        "reason": "parent_task_no_longer_available",
                        "urgent_escalation": aggregate_safety(t)["effective_urgency"] <= 1,
                    }
                )
                diff.append(
                    {
                        "ticket_id": tid,
                        "kind": "unassigned",
                        "reason": "parent_task_no_longer_available",
                    }
                )
                continue
        residual = parent_residuals.get(uuid.UUID(tid))
        if residual and tid not in parent.insertion_data.get("task_timing", {}):
            source = await db.get(DispatchPlan, residual.source_plan_id)
            if not source:
                raise HTTPException(409, "service_insertion_residual_invalid")
            timing = {
                "release_at": residual.release_at.isoformat(),
                "deadline_at": residual.deadline_at.isoformat(),
                "service_min": residual.service_min,
                "source_plan_id": str(
                    source.parent_plan_id
                    if residual.origin_kind == "insertion_urgent"
                    else source.id
                ),
            }
        else:
            timing = await task_timing(db, parent, tid)
        timings[tid] = timing
        if old and (
            phase(old, t) in {"moving", "working"} or str(old.worker_id) not in eligible_workers
        ):
            diff.append(
                {
                    "ticket_id": tid,
                    "kind": "fixed" if phase(old, t) in {"moving", "working"} else "retained",
                    "original_assignment_id": str(old.id),
                    "worker_id": str(old.worker_id),
                    "reason": "ongoing_visit"
                    if phase(old, t) in {"moving", "working"}
                    else "excluded_worker_queue_preserved",
                }
            )
            continue
        work_items[tid] = (t, timing, old)
    for urgent in payload.urgent_tickets:
        t = await db.get(Ticket, urgent.ticket_id)
        if not t:
            raise HTTPException(404, "ticket_not_found")
        tid = str(t.id)
        state["tickets"][tid] = await merge_version(db, t)
        risk = aggregate_safety(t)["effective_urgency"]
        active = await db.scalar(
            select(Assignment.id)
            .where(
                Assignment.ticket_id == t.id,
                Assignment.revoked.is_(False),
                Assignment.visit_ended_at.is_(None),
            )
            .limit(1)
        )
        if t.status != "confirmed" or active or risk > 1:
            excluded.append(
                {
                    "ticket_id": tid,
                    "reason": "urgent_ticket_not_eligible",
                    "urgent_escalation": risk <= 1,
                }
            )
            diff.append(
                {"ticket_id": tid, "kind": "unassigned", "reason": "urgent_ticket_not_eligible"}
            )
            continue
        timing = {
            "release_at": at(parent.plan_start_at, urgent.release_min).isoformat(),
            "deadline_at": at(parent.plan_start_at, urgent.deadline_min).isoformat(),
            "service_min": urgent.service_min,
            "assumptions_source": urgent.assumptions_source,
            "source_plan_id": str(parent.id),
        }
        timings[tid] = timing
        work_items[tid] = (t, timing, None)
    tickets = []
    for tid, (t, timing, old) in work_items.items():
        selected = t.location_data.get("selected") or {}
        domain = CATEGORY_MAP.get(t.category_code, {}).get("domain")
        risk = aggregate_safety(t)["effective_urgency"]
        if not valid_location(t, selected.get("code")) or domain not in DOMAINS:
            excluded.append(
                {
                    "ticket_id": tid,
                    "reason": "planning_data_unconfirmed",
                    "urgent_escalation": risk <= 1,
                }
            )
            diff.append(
                {
                    "ticket_id": tid,
                    "kind": "retained" if old else "unassigned",
                    "original_assignment_id": str(old.id) if old else None,
                    "worker_id": str(old.worker_id) if old else None,
                    "reason": "planning_data_unconfirmed",
                }
            )
            continue
        node = codes.index(selected["code"])
        try:
            tickets.append(
                TicketSpec(
                    id=tid,
                    node=node,
                    domain=domain,
                    region=nodes[node]["region"],
                    release_min=offset(base, datetime.fromisoformat(timing["release_at"])),
                    deadline_min=offset(base, datetime.fromisoformat(timing["deadline_at"])),
                    service_min=timing["service_min"],
                    urgency=risk,
                )
            )
        except ValidationError:
            raise HTTPException(422, "insertion_time_range_exceeded") from None
    if len(tickets) > 50:
        raise HTTPException(422, "insertion_task_limit")
    try:
        problem = PlanningInput(
            workers=tuple(workers),
            tickets=tuple(tickets),
            travel=travel,
            mode=parent.input_data["mode"],
            solver_seconds=float(payload.solver_seconds),
            assumptions_version=payload.assumptions_version,
            data_kind="service" if provider else "synthetic",
            dataset_hash=provider.sha256 if provider else None,
            dataset_revision=dataset_revision,
        )
    except ValidationError:
        raise HTTPException(422, "insertion_time_range_exceeded") from None
    metadata = {
        "parent_base": parent.plan_start_at.isoformat(),
        "offset_shift_min": delta,
        "dispatch_not_before_at": dispatch_not_before_at.isoformat(),
        "reference_at": reference_at.isoformat(),
        "diff": diff,
        "task_timing": timings,
        "worker_specs": specs,
        "fixed_by_worker": fixed_by_worker,
        "remaining_snapshot": {
            str(a.id): a.remaining_data
            for a in all_assignments
            if a.remaining_data and str(a.worker_id) in eligible_workers
        },
        "parent_all_assignment_ids": [str(a.id) for a in all_assignments],
        "rounding": "ceil minute offsets; integer parallel translation; durations unchanged",
    }
    return problem, state, excluded, metadata, base


async def recheck_service_insertion_calculation(
    db,
    parent,
    payload,
    reference_at,
    dispatch_not_before_at,
    problem,
    result,
    snapshot,
    excluded,
    metadata,
    base,
    *,
    service_context,
):
    """Recheck after solving; caller must authenticate dispatcher before entering the gate.

    The caller must persist in this same transaction. Public service insertion remains disabled.
    """
    if dispatch_not_before_at != reference_at + timedelta(minutes=2):
        raise HTTPException(409, "planning_snapshot_stale")
    current, state, current_excluded, current_metadata, current_base = await capture_insertion(
        db, parent, payload, reference_at, dispatch_not_before_at, service_context=service_context
    )
    try:
        validate_plan(problem, result)
        if (
            current != problem
            or state != snapshot
            or current_excluded != excluded
            or current_metadata != metadata
            or current_base != base
        ):
            raise ValueError("insertion_calculation_stale")
        expiry = min(
            [reference_at + timedelta(minutes=2)]
            + [
                datetime.fromisoformat(item["expires_at"])
                for item in metadata["remaining_snapshot"].values()
            ]
            + [at(base, route.departure_min) for route in result.routes if route.visits]
        )
        if now() >= expiry:
            raise ValueError("insertion_calculation_expired")
        return expiry
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, "planning_snapshot_stale") from None


async def insertion_snapshot_current(db, plan, *, service_context=None):
    if service_context is not None:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        await db.refresh(plan)
        try:
            if datetime.fromisoformat(plan.insertion_data["dispatch_not_before_at"]) != (
                plan.captured_at + timedelta(minutes=2)
            ):
                raise ValueError("invalid_dispatch_bound")
        except (ValueError, TypeError, KeyError):
            raise HTTPException(409, "planning_snapshot_stale") from None
    parent = await get_plan(db, plan.parent_plan_id)
    payload = InsertionCreate.model_validate(plan.request_data)
    problem, state, excluded, metadata, base = await capture_insertion(
        db,
        parent,
        payload,
        plan.captured_at,
        datetime.fromisoformat(plan.insertion_data["dispatch_not_before_at"]),
        service_context=service_context,
    )
    if state != plan.snapshot_data:
        raise HTTPException(409, "planning_snapshot_stale")
    if service_context is not None:
        try:
            stored_input = PlanningInput.model_validate_json(json.dumps(plan.input_data))
            stored_result = PlanResult.model_validate_json(json.dumps(plan.result_data))
            validate_plan(stored_input, stored_result)
            latest_expiry = min(
                [plan.captured_at + timedelta(minutes=2)]
                + [
                    datetime.fromisoformat(item["expires_at"])
                    for item in metadata["remaining_snapshot"].values()
                ]
                + [at(base, route.departure_min) for route in stored_result.routes if route.visits]
            )
            fields = (
                "parent_base",
                "offset_shift_min",
                "dispatch_not_before_at",
                "reference_at",
                "task_timing",
                "worker_specs",
                "fixed_by_worker",
                "remaining_snapshot",
                "parent_all_assignment_ids",
                "rounding",
            )
            if (
                plan.kind != "insertion"
                or plan.status != "proposed"
                or stored_input != problem
                or input_hash(stored_input) != plan.input_hash
                or plan.plan_start_at != base
                or plan.expires_at > latest_expiry
                or plan.excluded != excluded
                or any(plan.insertion_data.get(key) != metadata[key] for key in fields)
                or plan.insertion_data.get("preserved_diff") != metadata["diff"]
            ):
                raise ValueError("service_insertion_evidence_mismatch")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(409, "planning_snapshot_stale") from None
    stamp = now()
    if stamp >= plan.expires_at:
        raise HTTPException(409, "planning_snapshot_expired")
    for data in plan.insertion_data["remaining_snapshot"].values():
        if stamp >= datetime.fromisoformat(data["expires_at"]) or stamp >= datetime.fromisoformat(
            data["expected_end_at"]
        ):
            raise HTTPException(409, "remaining_reconfirmation_required")
    return parent


async def approve_insertion(db, plan, payload, principal, settings):
    parent = await insertion_snapshot_current(db, plan)
    problem = PlanningInput.model_validate_json(json.dumps(plan.input_data))
    result = PlanResult.model_validate_json(json.dumps(plan.result_data))
    validate_plan(problem, result)
    old_assignments = await active_assignments(db, parent)
    old_by_ticket = {str(a.ticket_id): a for a in old_assignments if not a.revoked}
    workers = [uuid.UUID(wid) for wid in plan.insertion_data["worker_specs"]]
    for wid in sorted(workers, key=str):
        await db.get(User, wid, with_for_update=True)
    schedules = await lock_schedules(db, workers)
    await insertion_snapshot_current(db, plan)
    if any(is_day_off_during(
        schedules[uuid.UUID(route.worker_id)].data,
        at(plan.plan_start_at, visit.arrival_min),
        at(plan.plan_start_at, visit.completion_min),
    ) for route in result.routes for visit in route.visits):
        raise HTTPException(409, "worker_day_off")
    diff = list(plan.insertion_data["preserved_diff"])
    preserved = {d["original_assignment_id"]: d for d in diff if d.get("original_assignment_id")}
    for a in old_assignments:
        if str(a.id) in preserved:
            a.active_plan_id = plan.id
            d = preserved[str(a.id)]
            db.add(
                PlanMembership(
                    plan_id=plan.id,
                    assignment_id=a.id,
                    kind=d["kind"],
                    original_assignment_id=a.id,
                    sequence=a.plan_sequence or 0,
                    timing_data={},
                )
            )
            d["assignment_id"] = str(a.id)
    assigned_ids = set()
    for route in result.routes:
        worker = await db.get(User, uuid.UUID(route.worker_id))
        predecessor_id = plan.insertion_data["fixed_by_worker"].get(route.worker_id)
        predecessor_id = uuid.UUID(predecessor_id) if predecessor_id else None
        for sequence, visit in enumerate(route.visits):
            tid = visit.ticket_id
            old = old_by_ticket.get(tid)
            ticket = await db.get(Ticket, uuid.UUID(tid), with_for_update=True)
            arrival, completion = (
                at(plan.plan_start_at, visit.arrival_min),
                at(plan.plan_start_at, visit.completion_min),
            )
            old_plan = await db.get(DispatchPlan, old.plan_id) if old else None
            unchanged = bool(
                old
                and old.worker_id == worker.id
                and old_plan
                and at(old_plan.plan_start_at, old.planned_arrival_min) == arrival
                and at(old_plan.plan_start_at, old.planned_completion_min) == completion
                and old.predecessor_id == predecessor_id
            )
            if unchanged:
                a, kind = old, "retained"
                a.active_plan_id = plan.id
            else:
                if old:
                    old.revoked = True
                kind = "replaced" if old else "new"
                a = Assignment(
                    id=uuid.uuid4(),
                    ticket_id=ticket.id,
                    worker_id=worker.id,
                    auth_epoch=worker.auth_epoch,
                    plan_id=plan.id,
                    active_plan_id=plan.id,
                    predecessor_id=predecessor_id,
                    plan_sequence=sequence,
                    planned_arrival_min=visit.arrival_min,
                    planned_completion_min=visit.completion_min,
                    token_hash=token_digest(settings, secrets.token_urlsafe(32)),
                    expires_at=now() + timedelta(hours=settings.worker_token_hours),
                )
                db.add(a)
                await db.flush()
                transition(
                    db, ticket, principal.user, "urgent_plan_assigned", payload.reason, "assigned"
                )
            db.add(
                PlanMembership(
                    plan_id=plan.id,
                    assignment_id=a.id,
                    kind=kind,
                    original_assignment_id=old.id if old else None,
                    sequence=sequence,
                    timing_data=plan.insertion_data["task_timing"][tid],
                )
            )
            diff.append(
                {
                    "ticket_id": tid,
                    "kind": kind,
                    "worker_id": str(worker.id),
                    "original_assignment_id": str(old.id) if old else None,
                    "assignment_id": str(a.id),
                }
            )
            predecessor_id = a.id
            assigned_ids.add(tid)
    for unassigned in result.unassigned:
        old = old_by_ticket.get(unassigned.ticket_id)
        if old:
            old.revoked = True
            old.active_plan_id = plan.id
            ticket = await db.get(Ticket, old.ticket_id, with_for_update=True)
            transition(
                db, ticket, principal.user, "urgent_plan_unassigned", payload.reason, "confirmed"
            )
            db.add(
                PlanMembership(
                    plan_id=plan.id,
                    assignment_id=old.id,
                    kind="unassigned",
                    original_assignment_id=old.id,
                    sequence=old.plan_sequence or 0,
                    timing_data={},
                )
            )
        diff.append(
            {
                "ticket_id": unassigned.ticket_id,
                "kind": "unassigned",
                "reason": unassigned.reason,
                "original_assignment_id": str(old.id) if old else None,
                "urgent_escalation": unassigned.urgent_escalation,
            }
        )
    metadata = dict(plan.insertion_data)
    metadata["diff"] = diff
    plan.insertion_data = metadata
    for row in schedules.values():
        row.revision += 1
    parent.status, parent.revision = "superseded", parent.revision + 1
    plan.status, plan.revision = "approved", plan.revision + 1
    plan.approved_by, plan.approved_at, plan.approval_reason = (
        principal.user.id,
        now(),
        payload.reason,
    )
    await db.flush()
    return PlanApprovalView(plan=plan_view(plan), assignments=await assignments_view(db, plan))


async def proposal_diff(db, parent, result, base, metadata):
    old_by_ticket = {
        str(a.ticket_id): a for a in await active_assignments(db, parent) if not a.revoked
    }
    diff = list(metadata["diff"])
    service_child = parent.kind == "insertion" and parent.input_data.get("data_kind") == "service"
    child_entries = {}
    if service_child:
        from app.models import ServicePlanExecutionEntry

        child_entries = {
            row.assignment_id: row
            for row in (
                await db.scalars(
                    select(ServicePlanExecutionEntry).where(
                        ServicePlanExecutionEntry.plan_id == parent.id
                    )
                )
            ).all()
        }
    for route in result.routes:
        predecessor = metadata["fixed_by_worker"].get(route.worker_id)
        if service_child and predecessor is None:
            completed = []
            for assignment_id, entry in child_entries.items():
                if str(entry.worker_id) != route.worker_id:
                    continue
                assignment = await db.get(Assignment, assignment_id)
                if assignment and assignment.visit_ended_at:
                    completed.append(entry)
            if completed:
                predecessor = str(
                    max(completed, key=lambda item: item.execution_order).assignment_id
                )
        for visit in route.visits:
            old = old_by_ticket.get(visit.ticket_id)
            old_plan = await db.get(DispatchPlan, old.plan_id) if old else None
            old_predecessor = old.predecessor_id if old else None
            if service_child and old:
                entry = child_entries.get(old.id)
                if not entry:
                    raise HTTPException(409, "service_insertion_parent_invalid")
                old_predecessor = entry.predecessor_assignment_id
            unchanged = bool(
                old
                and str(old.worker_id) == route.worker_id
                and old_plan
                and at(old_plan.plan_start_at, old.planned_arrival_min)
                == at(base, visit.arrival_min)
                and at(old_plan.plan_start_at, old.planned_completion_min)
                == at(base, visit.completion_min)
                and (str(old_predecessor) if old_predecessor else None) == predecessor
            )
            diff.append(
                {
                    "ticket_id": visit.ticket_id,
                    "kind": "retained" if unchanged else "replaced" if old else "new",
                    "original_assignment_id": str(old.id) if old else None,
                    "worker_id": route.worker_id,
                }
            )
            predecessor = str(old.id) if unchanged else "new:" + visit.ticket_id
    for item in result.unassigned:
        old = old_by_ticket.get(item.ticket_id)
        diff.append(
            {
                "ticket_id": item.ticket_id,
                "kind": "unassigned",
                "reason": item.reason,
                "urgent_escalation": item.urgent_escalation,
                "original_assignment_id": str(old.id) if old else None,
            }
        )
    return diff
