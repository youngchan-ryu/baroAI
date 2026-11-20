"""Synthetic DB adapter. No source text, pictures, or credentials enter planning snapshots."""

import hashlib
import json
from datetime import datetime, time, timedelta
from math import ceil, floor
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.catalog import CATEGORY_MAP
from app.dispatch_schemas import DispatchPlanView, ScheduleInput, ScheduleView
from app.duplicates import aggregate_safety, merge_version
from app.models import (
    Assignment,
    CompletionReport,
    DispatchPlan,
    PlanMembership,
    SchoolCatalogActive,
    SchoolCatalogVersion,
    Ticket,
    User,
    WorkerProfile,
    WorkerSchedule,
)
from app.planning import PlanningInput, PlanResult, TicketSpec, TravelMatrix, WorkerSpec
from app.security import now
from app.services.location import BUILDINGS, REGISTERED_SPACES, registered

PRESET_ID = "demo-campus-v1"
REGIONS = ["DEMO-NORTH", "DEMO-SOUTH"]
DOMAINS = ["ARCH_CIVIL", "CIVIL", "ELEC", "MECH"]
CODES = sorted(
    set(REGISTERED_SPACES)
    | {b["code"] for b in BUILDINGS}
    | {b["code"] + ":" + f for b in BUILDINGS for f in b["floors"]}
)
NODES = [
    {
        "code": c,
        "label": registered(c)["label"],
        "region": "DEMO-SOUTH" if c.startswith("DEMO-DORM") else "DEMO-NORTH",
    }
    for c in CODES
]
PROVENANCE = "합성 시연 가정: 같은 건물 2분, 건물 간 8분. 실측 이동시간 아님."
SEOUL = ZoneInfo("Asia/Seoul")
TRAVEL = TravelMatrix(
    nodes=tuple(CODES),
    minutes=tuple(
        tuple(0 if a == b else 2 if a.split(":")[0] == b.split(":")[0] else 8 for b in CODES)
        for a in CODES
    ),
    version=PRESET_ID,
    provenance=PROVENANCE,
)


def demo_only(settings):
    if settings.deployment_mode != "demo":
        raise HTTPException(503, "verified_planning_data_unavailable")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_day_off(data, instant=None):
    """Dates are local calendar days; old schedules without days_off remain valid."""
    instant = instant or now()
    day = instant.astimezone(SEOUL).date().isoformat()
    return day in (data or {}).get("days_off", [])


def is_day_off_during(data, start, end):
    """Check every Seoul calendar day touched by the visit, including endpoints."""
    first = start.astimezone(SEOUL).date().isoformat()
    last = end.astimezone(SEOUL).date().isoformat()
    return any(first <= day <= last for day in (data or {}).get("days_off", []))


def worker_time_reason(data, instant=None):
    instant = instant or now()
    if is_day_off(data, instant):
        return "atomic_worker_day_off"
    if hours := (data or {}).get("hours"):
        current = instant.astimezone(SEOUL).strftime("%H:%M")
        if not hours["shift_start"] <= current < hours["shift_end"]:
            return "atomic_worker_outside_hours"
        if hours.get("lunch_start") and hours["lunch_start"] <= current < hours["lunch_end"]:
            return "atomic_worker_lunch"
    return None


def plan_working_window(data, base):
    if not (hours := data.get("hours")):
        return data["shift_start_min"], data["shift_end_min"], None
    day = base.astimezone(SEOUL).date()

    def offset(clock):
        return (datetime.combine(day, time.fromisoformat(clock), SEOUL) - base).total_seconds() / 60

    start = max(0, ceil(offset(hours["shift_start"])))
    end = floor(offset(hours["shift_end"]))
    if hours.get("lunch_start"):
        lunch_start = floor(offset(hours["lunch_start"]))
        lunch_end = ceil(offset(hours["lunch_end"]))
        # A plan beginning in the middle of lunch can only start work afterwards.
        if lunch_start <= start < lunch_end:
            start = lunch_end
        elif start < lunch_start < end:
            if lunch_end >= end:
                end = lunch_start
            else:
                return start, end, (lunch_start, lunch_end)
    return start, end, None


def schedule_view(worker, row, staff_code):
    data = ScheduleInput(expected_revision=row.revision, **row.data) if row and row.data else None
    return ScheduleView(
        worker_id=worker.id,
        display_name=worker.display_name,
        staff_code=staff_code,
        revision=row.revision if row else 0,
        configured=bool(row and row.data),
        data=data,
    )


async def lock_schedules(db, worker_ids):
    result = {}
    for wid in sorted(set(worker_ids), key=str):
        row = await db.get(WorkerSchedule, wid, with_for_update=True)
        if not row:
            row = WorkerSchedule(worker_id=wid, revision=1, data={})
            db.add(row)
            await db.flush()
        result[wid] = row
    return result


async def require_no_worker_reservations(db, worker_id):
    reserved = await db.scalar(
        select(Assignment.id)
        .join(Ticket, Ticket.id == Assignment.ticket_id)
        .where(
            Assignment.worker_id == worker_id,
            or_(Assignment.revoked.is_(False), Assignment.stopped_at.is_not(None)),
            Assignment.active_plan_id.is_not(None),
            Assignment.visit_ended_at.is_(None),
            Ticket.status.in_(["assigned", "in_progress"]),
        )
        .limit(1)
    )
    if reserved:
        raise HTTPException(409, "worker_has_plan_reservations")


async def manual_schedule_change(db, worker_id, ticket_id):
    # Called under the global gate and before ticket/assignment locks.
    worker = await db.get(User, worker_id)
    if not worker or worker.role != "worker":
        raise HTTPException(422, "invalid_worker")
    await require_no_worker_reservations(db, worker_id)
    source_reserved = await db.scalar(
        select(Assignment.id)
        .where(
            Assignment.ticket_id == ticket_id,
            Assignment.active_plan_id.is_not(None),
            Assignment.visit_ended_at.is_(None),
            or_(Assignment.revoked.is_(False), Assignment.stopped_at.is_not(None)),
        )
        .limit(1)
    )
    if source_reserved:
        raise HTTPException(409, "plan_partial_reassignment_forbidden")
    old_workers = list(
        (
            await db.scalars(
                select(Assignment.worker_id).where(
                    Assignment.ticket_id == ticket_id, Assignment.revoked.is_(False)
                )
            )
        ).all()
    )
    rows = await lock_schedules(db, [worker_id, *old_workers])
    if reason := worker_time_reason(rows[worker_id].data):
        raise HTTPException(409, reason)
    for row in rows.values():
        row.revision += 1


def validate_dataset_request(payload, provider, dataset_revision):
    if provider is not None:
        if (
            payload.preset_id != provider.dataset.dataset_id
            or payload.dataset_hash != provider.sha256
            or payload.dataset_revision != dataset_revision
        ):
            raise HTTPException(409, "planning_dataset_changed")
    elif (
        getattr(payload, "preset_id", PRESET_ID) != PRESET_ID
        or getattr(payload, "dataset_hash", None) is not None
        or getattr(payload, "dataset_revision", None) is not None
    ):
        raise HTTPException(422, "invalid_demo_planning_dataset")


async def capture(db, payload, provider=None, dataset_revision=None):
    """Snapshot every selected worker's capability, queue, reports, and selected ticket judgment."""
    validate_dataset_request(payload, provider, dataset_revision)
    workers, tickets, excluded, snapshot = [], [], [], {"workers": {}, "tickets": {}}
    settings = db.info.get("settings")
    db_provider = bool(settings and settings.planning_dataset_provider == "db")
    catalog = None
    if db_provider:
        pointer = await db.get(SchoolCatalogActive, 1)
        catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
        if not catalog:
            raise HTTPException(503, "planning_dataset_unavailable")
        snapshot["catalog_revision"] = catalog.revision
    field_codes = {item["code"] for item in catalog.content["fields"]} if catalog else set(DOMAINS)
    category_domains = (
        {item["code"]: item["field_code"] for item in catalog.content["categories"]}
        if catalog
        else {}
    )
    codes, nodes, travel = CODES, NODES, TRAVEL
    if provider is not None:
        codes = [item.code for item in provider.dataset.locations]
        nodes = [
            {"code": item.code, "label": item.label, "region": item.region}
            for item in provider.dataset.locations
        ]
        travel = TravelMatrix(
            nodes=tuple(codes),
            minutes=provider.dataset.travel_minutes,
            version=provider.dataset.version,
            estimate_parameters=provider.dataset.estimate_parameters,
            provenance=("estimated_straight_line · 건물 간 직선거리 기준 추정"
                        if provider.dataset.evidence.source == "estimated_straight_line"
                        else "운영자가 지정한 계획 자료 · 현장 검증 여부 별도 확인"),
        )
        snapshot["dataset"] = {"sha256": provider.sha256, "revision": dataset_revision}
    for wid in sorted(payload.worker_ids, key=str):
        worker = await db.get(User, wid)
        if not worker:
            raise HTTPException(422, "unknown_worker")
        schedule = await db.get(WorkerSchedule, wid)
        if provider is not None:
            from app.auth_service import worker_eligible

            eligible = worker_eligible(worker, settings)
        else:
            eligible = True
        assignments = list(
            (
                await db.scalars(
                    select(Assignment).where(Assignment.worker_id == wid).order_by(Assignment.id)
                )
            ).all()
        )
        records, busy = [], False
        for a in assignments:
            t = await db.get(Ticket, a.ticket_id)
            reports = list(
                (
                    await db.scalars(
                        select(CompletionReport)
                        .where(CompletionReport.assignment_id == a.id)
                        .order_by(CompletionReport.id)
                    )
                ).all()
            )
            pending = any(not r.reviewed and r.analysis_status != "dismissed" for r in reports)
            from app.execution import phase

            busy |= (
                (not a.revoked and phase(a, t) != "visit_ended")
                or bool(a.stopped_at and a.active_plan_id)
                or pending
            )
            records.append(
                [
                    str(a.id),
                    str(a.ticket_id),
                    a.revoked,
                    a.auth_epoch,
                    a.plan_id and str(a.plan_id),
                    a.credential_revision,
                    str(a.active_plan_id),
                    a.execution_revision,
                    a.remaining_data,
                    str(a.predecessor_id),
                    a.execution_location_code,
                    str(a.departed_at),
                    str(a.started_at),
                    str(a.visit_ended_at),
                    str(a.stopped_at),
                    t.status,
                    await merge_version(db, t),
                    [[str(r.id), r.revision, r.reviewed, r.analysis_status] for r in reports],
                ]
            )
        snapshot["workers"][str(wid)] = {
            "role": worker.role,
            "epoch": worker.auth_epoch,
            **({"service_eligible": eligible} if provider is not None else {}),
            "schedule_revision": schedule.revision if schedule else 0,
            "schedule": schedule.data if schedule else None,
            "assignments": records,
        }
        profile = await db.get(WorkerProfile, wid) if db_provider else None
        if db_provider:
            snapshot["workers"][str(wid)]["profile_revision"] = profile.revision if profile else 0
        data = schedule.data if schedule else {}
        if (
            provider is not None
            and data.get("enabled")
            and (
                data.get("dataset_hash") != provider.sha256
                or data.get("dataset_revision") != dataset_revision
            )
        ):
            raise HTTPException(409, "schedule_dataset_stale")
        if data.get("enabled") and (
            data.get("start_node") not in codes
            or data.get("end_node") not in codes
            or data.get("region") not in {node["region"] for node in nodes}
        ):
            raise HTTPException(409, "schedule_dataset_invalid")
        reason = (
            "worker_role_changed"
            if worker.role != "worker"
            else "worker_role_changed"
            if not eligible
            else "busy_existing_assignment"
            if busy
            else "schedule_unconfigured"
            if not data
            else "worker_unavailable"
            if not data.get("enabled")
            else "worker_day_off"
            if is_day_off(data, payload.plan_start_at)
            else "worker_unavailable"
            if db_provider
            and (
                not profile
                or profile.catalog_revision != catalog.revision
                or not set(data.get("skills", [])) <= set(profile.field_codes)
                or not set(profile.field_codes) <= field_codes
            )
            else None
        )
        if reason:
            excluded.append({"worker_id": str(wid), "reason": reason})
            continue
        shift_start, shift_end, lunch = plan_working_window(data, payload.plan_start_at)
        if shift_end <= shift_start:
            excluded.append({"worker_id": str(wid), "reason": "outside_shift"})
            continue
        workers.append(
            WorkerSpec(
                id=str(wid),
                skills=tuple(data["skills"]),
                region=data["region"],
                start_node=codes.index(data["start_node"]),
                end_node=codes.index(data["end_node"]),
                shift_start_min=shift_start,
                shift_end_min=shift_end,
                lunch_min=lunch,
            )
        )
    for item in payload.tickets:
        ticket = await db.get(Ticket, item.ticket_id)
        if not ticket:
            raise HTTPException(404, "ticket_not_found")
        snapshot["tickets"][str(ticket.id)] = await merge_version(db, ticket)
        selected = ticket.location_data.get("selected") or {}
        code = selected.get("code")
        if (
            provider is not None
            and ticket.location_data.get("state") == "confirmed"
            and (
                ticket.location_data.get("source_type") != "service"
                or ticket.location_data.get("dataset_hash") != provider.sha256
                or ticket.location_data.get("dataset_revision") != dataset_revision
            )
        ):
            raise HTTPException(409, "location_dataset_stale")
        domain = (
            category_domains.get(ticket.category_code)
            if db_provider
            else CATEGORY_MAP.get(ticket.category_code, {}).get("domain")
        )
        if db_provider and (
            ticket.catalog_revision != catalog.revision
            or ticket.catalog_snapshot.get("category_code") != ticket.category_code
            or ticket.catalog_snapshot.get("domain") != domain
            or not ticket.catalog_snapshot.get("catalog_backed")
        ):
            domain = None
        active = await db.scalar(
            select(Assignment.id)
            .join(User, User.id == Assignment.worker_id)
            .where(
                Assignment.ticket_id == ticket.id,
                Assignment.revoked.is_(False),
                User.role == "worker",
                Assignment.auth_epoch == User.auth_epoch,
            )
            .limit(1)
        )
        pending = await db.scalar(
            select(CompletionReport.id)
            .where(
                CompletionReport.ticket_id == ticket.id,
                CompletionReport.reviewed.is_(False),
                CompletionReport.superseded_at.is_(None),
                CompletionReport.analysis_status != "dismissed",
            )
            .limit(1)
        )
        reason = (
            "ticket_not_confirmed"
            if ticket.status != "confirmed"
            else "existing_assignment"
            if active
            else "pending_report"
            if pending
            else "location_unconfirmed"
            if ticket.location_data.get("state") != "confirmed"
            else "travel_data_missing"
            if code not in codes and db_provider
            else "location_unregistered"
            if code not in codes
            else "domain_unconfirmed"
            if domain not in field_codes
            else None
        )
        urgency = aggregate_safety(ticket)["effective_urgency"]
        if reason:
            excluded.append(
                {"ticket_id": str(ticket.id), "reason": reason, "urgent_escalation": urgency <= 1}
            )
            continue
        tickets.append(
            TicketSpec(
                id=str(ticket.id),
                node=codes.index(code),
                domain=domain,
                region=nodes[codes.index(code)]["region"],
                release_min=item.release_min,
                deadline_min=item.deadline_min,
                service_min=item.service_min,
                urgency=urgency,
            )
        )
    problem = PlanningInput(
        workers=tuple(workers),
        tickets=tuple(tickets),
        travel=travel,
        mode=payload.mode,
        solver_seconds=float(payload.solver_seconds),
        assumptions_version=payload.assumptions_version,
        data_kind="service" if provider is not None else "synthetic",
        dataset_hash=provider.sha256 if provider is not None else None,
        dataset_revision=dataset_revision,
    )
    return problem, snapshot, excluded


def plan_view(plan):
    return DispatchPlanView(
        id=plan.id,
        revision=plan.revision,
        status=plan.status,
        parent_plan_id=plan.parent_plan_id,
        kind=plan.kind,
        insertion=plan.insertion_data,
        input_hash=plan.input_hash,
        assumptions=plan.request_data,
        input=PlanningInput.model_validate_json(json.dumps(plan.input_data)),
        result=PlanResult.model_validate_json(json.dumps(plan.result_data)),
        excluded=plan.excluded,
        plan_start_at=plan.plan_start_at,
        expires_at=plan.expires_at,
        created_at=plan.created_at,
        captured_at=plan.captured_at,
        approved_at=plan.approved_at,
    )


def valid_time(payload, stamp):
    if payload.plan_start_at <= stamp or payload.plan_start_at > stamp + timedelta(days=1):
        raise HTTPException(422, "plan_start_must_be_future_within_one_day")


async def current_snapshot(db, plan, provider=None, dataset_revision=None):
    from app.dispatch_schemas import PlanCreate

    payload = PlanCreate.model_validate(plan.request_data)
    _, snapshot, _ = await capture(db, payload, provider, dataset_revision)
    if snapshot != plan.snapshot_data:
        raise HTTPException(409, "planning_snapshot_stale")
    if now() >= plan.expires_at or now() >= plan.plan_start_at:
        raise HTTPException(409, "planning_snapshot_expired")


async def assignments_view(db, plan):
    from app.dispatch_schemas import PlannedAssignmentView
    from app.execution import execution_view

    memberships = list(
        (
            await db.scalars(
                select(PlanMembership)
                .where(PlanMembership.plan_id == plan.id)
                .order_by(PlanMembership.sequence)
            )
        ).all()
    )
    if memberships:
        pairs = [(await db.get(Assignment, m.assignment_id), m) for m in memberships]
    else:
        pairs = [
            (a, None)
            for a in (
                await db.scalars(
                    select(Assignment)
                    .where(Assignment.plan_id == plan.id)
                    .order_by(Assignment.worker_id, Assignment.plan_sequence)
                )
            ).all()
        ]
    return [
        PlannedAssignmentView(
            assignment_id=a.id,
            ticket_id=a.ticket_id,
            worker_id=a.worker_id,
            credential_revision=a.credential_revision,
            revoked=a.revoked,
            membership_kind=m.kind if m else "new",
            original_assignment_id=m.original_assignment_id if m else None,
            execution=await execution_view(db, a, await db.get(Ticket, a.ticket_id), staff=True),
        )
        for a, m in pairs
    ]


async def get_plan(db, plan_id, *, lock=False):
    plan = await db.get(DispatchPlan, plan_id, with_for_update=lock)
    if not plan:
        raise HTTPException(404, "plan_not_found")
    return plan
