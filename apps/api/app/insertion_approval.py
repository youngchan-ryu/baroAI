"""Internal first- and second-generation service approval, not a public route.

Child execution remains disabled until its separately reviewed execution validator exists.
"""

import hashlib
import json
import secrets
from datetime import timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect, select

from app.dispatch_routes import dispatcher, save_receipt
from app.dispatch_schemas import PlanApprovalView
from app.dispatching import (
    assignments_view,
    fingerprint,
    is_day_off_during,
    lock_schedules,
    plan_view,
)
from app.insertion_evidence import (
    _assemble_initial_snapshot,
    _capture_initial_evidence,
    _child_parent_evidence,
    _initial_parent_evidence,
)
from app.models import (
    Assignment,
    CompletionReport,
    DispatchPlan,
    IdempotencyRecord,
    PlanMembership,
    ServicePlanDisposition,
    ServicePlanExecutionEntry,
    Ticket,
    TicketEvent,
    User,
)
from app.security import assignment_current, now
from app.tickets import transition


async def _preserved_fingerprint(db, assignment_ids):
    """In-memory only; active-plan ownership and revocation have explicit checks."""
    rows = []
    for assignment_id in sorted(assignment_ids, key=str):
        assignment = await db.get(Assignment, assignment_id, populate_existing=True)
        if not assignment:
            raise HTTPException(409, "service_insertion_preservation_failed")
        reports = list(
            (
                await db.scalars(
                    select(CompletionReport)
                    .where(CompletionReport.assignment_id == assignment_id)
                    .order_by(CompletionReport.id)
                )
            ).all()
        )

        def values(row, excluded=()):
            return {
                col.key: getattr(row, col.key)
                for col in inspect(type(row)).columns
                if col.key not in excluded
            }

        rows.append(
            {
                "assignment": values(assignment, {"active_plan_id", "revoked"}),
                "reports": [values(report) for report in reports],
            }
        )
    return fingerprint(jsonable_encoder(rows))


async def _receipt(db, request, principal, child_id, payload):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 100:
        raise HTTPException(422, "idempotency_key_required")
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    scope = f"{principal.user.id}:sia:{child_id}:{key_hash}"
    hashed = fingerprint(payload.model_dump(mode="json"))
    row = await db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope))
    if row and row.payload_hash != hashed:
        raise HTTPException(409, "idempotency_conflict")
    return scope, hashed, row


async def _current_approval_view(db, child):
    views = await assignments_view(db, child)
    for view in views:
        assignment = await db.get(Assignment, view.assignment_id)
        worker = await db.get(User, assignment.worker_id) if assignment else None
        if not assignment or (
            not assignment.revoked and not assignment_current(assignment, worker)
        ):
            raise HTTPException(409, "service_insertion_assignment_access_stale")
    return PlanApprovalView(plan=plan_view(child), assignments=views)


async def approve_initial_service_insertion(request, child_id, payload):
    """Atomically approve an initial or first-child parent in an authenticated transaction.

    Internal test-only function: caller must not expose this until service child
    execution, multi-generation lineage and the remaining concurrency checks pass.
    """
    settings = request.app.state.settings
    if settings.deployment_mode != "service" or request.method != "POST":
        raise HTTPException(404, "service_insertion_unavailable")
    async with request.app.state.database.transaction() as db:
        with db.no_autoflush:
            principal = await dispatcher(request, db, require_demo=False)
            scope, hashed, old = await _receipt(db, request, principal, child_id, payload)
            if old:
                try:
                    saved_id = json.loads(old.response_json)["plan"]["id"]
                except (ValueError, TypeError, KeyError):
                    raise HTTPException(409, "idempotency_conflict") from None
                if saved_id != str(child_id):
                    raise HTTPException(409, "idempotency_conflict")
                child = await db.get(DispatchPlan, child_id, populate_existing=True)
                if not child:
                    raise HTTPException(404, "plan_not_found")
                response = await _current_approval_view(db, child)
            else:
                child = await db.get(DispatchPlan, child_id, with_for_update=True)
                if (
                    not child
                    or child.status != "proposed"
                    or child.revision != payload.expected_revision
                    or child.input_hash != payload.input_hash
                ):
                    raise HTTPException(409, "plan_revision_conflict")
                parent = await db.get(DispatchPlan, child.parent_plan_id, with_for_update=True)
                if not parent or parent.kind not in {"initial", "insertion"}:
                    raise HTTPException(409, "service_insertion_parent_invalid")
                child_parent = parent.kind == "insertion"
                (
                    child,
                    result,
                    parents,
                    routes,
                    generated,
                    baseline,
                ) = await _capture_initial_evidence(
                    request, db, child_id, child_parent=child_parent
                )
                snapshot = _assemble_initial_snapshot(child, result, parents, routes, generated)
                if not routes:
                    raise HTTPException(409, "plan_has_no_assignable_routes")
                parent_ids = [item.origin.assignment_id for item in parents]
                worker_ids = {item.origin.worker_id for item in parents} | {
                    route.worker_id for route in routes
                }
                workers = {
                    wid: await db.get(User, wid, with_for_update=True)
                    for wid in sorted(worker_ids, key=str)
                }
                schedules = await lock_schedules(db, worker_ids)
                if any(is_day_off_during(
                    schedules[route.worker_id].data, route.planned_arrival,
                    route.planned_completion,
                ) for route in routes):
                    raise HTTPException(409, "worker_day_off")
                ticket_ids = {item.origin.ticket_id for item in parents} | {
                    route.ticket_id for route in routes
                }
                # Unassigned and excluded tickets have no execution entry but must be
                # locked with the same approval transaction as the resulting ledger.
                from uuid import UUID

                ticket_ids |= {UUID(item["id"]) for item in parent.input_data["tickets"]}
                ticket_ids |= {
                    UUID(item["ticket_id"]) for item in child.request_data["urgent_tickets"]
                }
                if child_parent:
                    from app.insertion_residual import load_first_parent_residuals

                    ticket_ids |= set(await load_first_parent_residuals(db, parent))
                tickets = {
                    tid: await db.get(Ticket, tid, with_for_update=True)
                    for tid in sorted(ticket_ids, key=str)
                }
                existing = {
                    aid: await db.get(Assignment, aid, with_for_update=True)
                    for aid in sorted(parent_ids, key=str)
                }
                original_revoked = {aid: assignment.revoked for aid, assignment in existing.items()}
                before = await _preserved_fingerprint(db, parent_ids)
                disposition_by_id = {
                    item.parent_assignment_id: item for item in snapshot.dispositions
                }
                # Recheck after every relevant row lock, before any business mutation.
                current_parents, current_baseline = await (
                    _child_parent_evidence if child_parent else _initial_parent_evidence
                )(
                    db,
                    child,
                    service_context=(
                        settings,
                        request.app.state.planning_dataset,
                        request.app.state.planning_dataset_revision,
                    ),
                )
                if parents != current_parents or baseline != current_baseline:
                    raise HTTPException(409, "planning_snapshot_stale")
                del baseline, current_baseline
                for path in snapshot.workers:
                    for entry in path.entries:
                        aid = entry.origin.assignment_id
                        previous = existing.get(aid)
                        if previous:
                            previous.active_plan_id = child.id
                            member_kind = "fixed" if entry.classification == "fixed" else "retained"
                            original_id = aid
                        else:
                            if aid not in generated or not workers.get(path.worker_id):
                                raise HTTPException(409, "service_insertion_evidence_invalid")
                            ticket = tickets.get(entry.origin.ticket_id)
                            if not ticket:
                                raise HTTPException(409, "service_insertion_evidence_invalid")
                            member_kind = generated[aid][1]
                            original_id = entry.replaces_assignment_id
                            arrival_delta = entry.origin.planned_arrival - child.plan_start_at
                            completion_delta = entry.origin.planned_completion - child.plan_start_at
                            minute = timedelta(minutes=1)
                            if arrival_delta % minute or completion_delta % minute:
                                raise HTTPException(409, "service_insertion_evidence_invalid")
                            previous = Assignment(
                                id=aid,
                                active_plan_id=child.id,
                                predecessor_id=entry.predecessor_id,
                                ticket_id=entry.origin.ticket_id,
                                worker_id=path.worker_id,
                                auth_epoch=workers[path.worker_id].auth_epoch,
                                plan_id=child.id,
                                plan_sequence=entry.origin.sequence,
                                planned_arrival_min=arrival_delta // minute,
                                planned_completion_min=completion_delta // minute,
                                token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                                expires_at=now() + timedelta(hours=settings.worker_token_hours),
                                revoked=False,
                            )
                            db.add(previous)
                            transition(
                                db,
                                ticket,
                                principal.user,
                                "urgent_plan_assigned",
                                payload.reason,
                                "assigned",
                            )
                        db.add(
                            PlanMembership(
                                plan_id=child.id,
                                assignment_id=aid,
                                kind=member_kind,
                                original_assignment_id=original_id,
                                sequence=entry.execution_order,
                                timing_data=child.insertion_data["task_timing"].get(
                                    str(entry.origin.ticket_id), {}
                                ),
                            )
                        )
                        db.add(
                            ServicePlanExecutionEntry(
                                plan_id=child.id,
                                assignment_id=aid,
                                worker_id=path.worker_id,
                                execution_order=entry.execution_order,
                                predecessor_assignment_id=entry.predecessor_id,
                                parent_membership_id=entry.parent_membership_id,
                                classification=entry.classification,
                                origin_data={
                                    "origin": entry.origin.model_dump(mode="json"),
                                    "replaces_assignment_id": str(entry.replaces_assignment_id)
                                    if entry.replaces_assignment_id
                                    else None,
                                },
                            )
                        )
                for disposition in snapshot.dispositions:
                    old = existing[disposition.parent_assignment_id]
                    if old.departed_at or old.started_at or old.visit_ended_at or old.stopped_at:
                        raise HTTPException(409, "service_insertion_preservation_failed")
                    old.revoked, old.active_plan_id = True, None
                    if disposition.classification == "unassigned":
                        transition(
                            db,
                            tickets[old.ticket_id],
                            principal.user,
                            "urgent_plan_unassigned",
                            payload.reason,
                            "confirmed",
                        )
                    db.add(
                        ServicePlanDisposition(
                            plan_id=child.id,
                            parent_assignment_id=old.id,
                            parent_membership_id=disposition.parent_membership_id,
                            classification=disposition.classification,
                            replacement_assignment_id=disposition.replacement_assignment_id,
                        )
                    )
                for tid in sorted(ticket_ids, key=str):
                    db.add(
                        TicketEvent(
                            ticket_id=tid,
                            actor_id=principal.user.id,
                            event_type="service_insertion_approved",
                            reason=payload.reason,
                        )
                    )
                for schedule in schedules.values():
                    schedule.revision += 1
                parent.status, parent.revision = "superseded", parent.revision + 1
                child.status, child.revision = "approved", child.revision + 1
                child.approved_by, child.approved_at, child.approval_reason = (
                    principal.user.id,
                    now(),
                    payload.reason,
                )
                child.snapshot_data = child.snapshot_data | {
                    "service_execution": snapshot.model_dump(mode="json")
                }
                await db.flush()
                from app.insertion_residual import save_initial_residuals

                await save_initial_residuals(db, parent, child)
                if before != await _preserved_fingerprint(db, parent_ids):
                    raise HTTPException(409, "service_insertion_preservation_failed")
                for aid, a in existing.items():
                    if aid in disposition_by_id:
                        if not a.revoked or a.active_plan_id is not None:
                            raise HTTPException(409, "service_insertion_preservation_failed")
                    elif a.revoked != original_revoked[aid] or a.active_plan_id != child.id:
                        raise HTTPException(409, "service_insertion_preservation_failed")
                response = await _current_approval_view(db, child)
                save_receipt(db, scope, hashed, response)
                await db.flush()
    return response
