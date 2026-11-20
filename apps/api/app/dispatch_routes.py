"""Dispatcher-only proposal, atomic approval, and explicit credential delivery."""

import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import or_, select
from starlette.concurrency import run_in_threadpool

from app.dispatch_schemas import (
    CatalogLocationCandidates,
    CatalogLocationConfirm,
    CredentialInput,
    CredentialView,
    DispatchPlanView,
    PlanApprovalView,
    PlanApprove,
    PlanCreate,
    PlanningCapabilities,
    ScheduleInput,
    ScheduleView,
    ServiceLocationConfirm,
    ServiceLocationDataset,
)
from app.dispatching import (
    DOMAINS,
    NODES,
    PRESET_ID,
    PROVENANCE,
    REGIONS,
    assignments_view,
    capture,
    current_snapshot,
    demo_only,
    fingerprint,
    get_plan,
    lock_schedules,
    plan_view,
    require_no_worker_reservations,
    schedule_view,
    valid_time,
    validate_dataset_request,
)
from app.models import (
    Assignment,
    AssignmentCredentialReceipt,
    CompletionReport,
    DispatchPlan,
    IdempotencyRecord,
    PlanMembership,
    SchoolCatalogActive,
    SchoolCatalogVersion,
    Ticket,
    TicketMerge,
    User,
    WorkerProfile,
    WorkerSchedule,
)
from app.planning import (
    PlanningInput,
    PlanResult,
    fifo,
    nearest,
    solve_ortools,
    urgency_fifo,
    validate_plan,
)
from app.security import assignment_current, authenticate, now, role, token_digest
from app.tickets import transition

router = APIRouter(prefix="/v1")


async def service_dataset(request, db):
    from app.planning.registry import active_generation, active_school_dataset

    if request.app.state.settings.planning_dataset_provider == "db":
        return await active_school_dataset(db, request.app.state.settings)

    if request.app.state.settings.deployment_mode == "prototype":
        raise HTTPException(503, "verified_planning_data_unavailable")
    if request.app.state.settings.deployment_mode != "service":
        raise HTTPException(404, "service_location_unavailable")
    provider = request.app.state.planning_dataset
    revision = request.app.state.planning_dataset_revision
    if provider is None or revision is None:
        raise HTTPException(503, "verified_planning_data_unavailable")
    await active_generation(db, request.app.state.settings, provider, revision)
    return provider, revision


async def planning_context(request, db):
    if (
        request.app.state.settings.deployment_mode == "demo"
        and request.app.state.settings.planning_dataset_provider == "file"
    ):
        return None, None
    from app.planning.datasets import has_unverified_travel

    provider, revision = await service_dataset(request, db)
    if has_unverified_travel(provider):
        raise HTTPException(503, "verified_planning_data_unavailable")
    return provider, revision


async def _catalog_candidates(db, ticket):
    from app.location_workflow import catalog_candidates_with_qr, merge_signal_candidates

    pointer = await db.get(SchoolCatalogActive, 1)
    catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
    if catalog is None or not catalog.content.get("locations"):
        return CatalogLocationCandidates(
            status="unavailable", catalog_revision=None,
            location_version=ticket.location_version, candidates=[],
        )
    hint = next((entry.get("code") for entry in reversed(ticket.location_history or [])
                 if entry.get("kind") == "qr_hint"), None)
    candidates, _ = catalog_candidates_with_qr(
        catalog.content, ticket.description, ticket.location_text, hint
    )
    by_code = {item["code"]: item for item in catalog.content.get("locations", [])}
    for source in ("current_gps", "photo_gps"):
        for entry in ticket.location_history or []:
            if entry.get("kind") != source or entry.get("catalog_revision") != catalog.revision:
                continue
            additions = []
            for code in entry.get("codes", []):
                location = by_code.get(code)
                if location:
                    additions.append({"code": code, "label": location["name"],
                                      "building_code": location["building_code"],
                                      "floor": (None if location["floor"] == "미확인"
                                                else str(location["floor"])),
                                      "space_code": location["space_code"]})
            merge_signal_candidates(candidates, additions, source)
    return CatalogLocationCandidates(
        status="no_match" if not candidates else "one" if len(candidates) == 1 else "multiple",
        catalog_revision=catalog.revision, location_version=ticket.location_version,
        candidates=candidates,
        qr_text_conflict=bool(candidates and "qr_hint" in candidates[0]["sources"] and any(
            "text" in item["sources"]
            and item["building_code"] != candidates[0]["building_code"]
            for item in candidates
        )),
    )


@router.get(
    "/tickets/{ticket_id}/catalog-location-candidates", response_model=CatalogLocationCandidates
)
async def catalog_location_candidates(ticket_id: uuid.UUID, request: Request):
    from app.tickets import get_ticket

    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        ticket = await get_ticket(db, ticket_id, principal)
        return await _catalog_candidates(db, ticket)


@router.post("/tickets/{ticket_id}/catalog-location", response_model=None)
async def confirm_catalog_location(
    ticket_id: uuid.UUID, payload: CatalogLocationConfirm, request: Request
):
    from app.location_workflow import check_version, preserve_legacy_input, save_location
    from app.routes import _photo_aware_transaction
    from app.schemas import LocationView
    from app.tickets import get_ticket, require_state, ticket_view

    async with _photo_aware_transaction(request) as db:
        principal = await dispatcher(request, db, require_demo=False)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        require_state(ticket, "received", "confirmed")
        check_version(ticket, payload.expected_location_version)
        current = await _catalog_candidates(db, ticket)
        if current.catalog_revision != payload.catalog_revision:
            raise HTTPException(409, "catalog_revision_conflict")
        selected = next((item for item in current.candidates if item.code == payload.code), None)
        if selected is None:
            raise HTTPException(422, "invalid_location_candidate")
        active_merge = await db.scalar(select(TicketMerge.id).where(
            TicketMerge.active.is_(True),
            or_(TicketMerge.source_id == ticket.id, TicketMerge.target_id == ticket.id),
        ).limit(1))
        active_assignment = await db.scalar(select(Assignment.id).where(
            Assignment.ticket_id == ticket.id,
            or_(Assignment.revoked.is_(False), Assignment.active_plan_id.is_not(None)),
            Assignment.visit_ended_at.is_(None),
        ).limit(1))
        pending_report = await db.scalar(select(CompletionReport.id).where(
            CompletionReport.ticket_id == ticket.id, CompletionReport.reviewed.is_(False),
            CompletionReport.superseded_at.is_(None),
            CompletionReport.analysis_status != "dismissed",
        ).limit(1))
        if active_merge or active_assignment or pending_report:
            raise HTTPException(409, "location_relationship_conflict")
        preserve_legacy_input(ticket)
        location = LocationView(
            state="confirmed", source_type="catalog", catalog_revision=current.catalog_revision,
            dictionary_version=f"school-catalog-{current.catalog_revision}",
            selected={**selected.model_dump(), "sources": ["dispatcher"]},
        ).model_dump()
        save_location(ticket, location)
        transition(db, ticket, principal.user, "location_updated", payload.reason.strip())
        await db.flush()
        await db.refresh(ticket, ["updated_at"])
        return await ticket_view(db, ticket, principal)


@router.get("/planning/location-dataset", response_model=ServiceLocationDataset)
async def service_location_dataset(request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        provider, revision = await service_dataset(request, db)
        pointer = await db.get(SchoolCatalogActive, 1)
        catalog = (
            await db.get(SchoolCatalogVersion, pointer.catalog_revision)
            if pointer and request.app.state.settings.planning_dataset_provider == "db"
            else None
        )
        if request.app.state.settings.planning_dataset_provider == "db" and catalog is None:
            raise HTTPException(503, "school_catalog_unavailable")
        locations = (
            [{"code": x["code"], "label": x["name"]} for x in catalog.content["locations"]]
            if catalog
            else [{"code": x.code, "label": x.label} for x in provider.dataset.locations]
        )
        return ServiceLocationDataset(
            version=provider.dataset.version,
            sha256=provider.sha256,
            revision=revision,
            locations=locations,
        )


@router.post("/tickets/{ticket_id}/service-location", response_model=None)
async def confirm_service_location(
    ticket_id: uuid.UUID, payload: ServiceLocationConfirm, request: Request
):
    from app.location_workflow import confirm_service_location as confirm_location
    from app.routes import _photo_aware_transaction
    from app.tickets import get_ticket, require_state, ticket_view

    async with _photo_aware_transaction(request) as db:
        principal = await dispatcher(request, db, require_demo=False)
        provider, revision = await service_dataset(request, db)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        require_state(ticket, "received", "confirmed")
        active_merge = await db.scalar(
            select(TicketMerge.id)
            .where(
                TicketMerge.active.is_(True),
                or_(TicketMerge.source_id == ticket.id, TicketMerge.target_id == ticket.id),
            )
            .limit(1)
        )
        active_assignment = await db.scalar(
            select(Assignment.id)
            .where(
                Assignment.ticket_id == ticket.id,
                or_(Assignment.revoked.is_(False), Assignment.active_plan_id.is_not(None)),
                Assignment.visit_ended_at.is_(None),
            )
            .limit(1)
        )
        pending_report = await db.scalar(
            select(CompletionReport.id)
            .where(
                CompletionReport.ticket_id == ticket.id,
                CompletionReport.reviewed.is_(False),
                CompletionReport.superseded_at.is_(None),
                CompletionReport.analysis_status != "dismissed",
            )
            .limit(1)
        )
        if active_merge or active_assignment or pending_report:
            raise HTTPException(409, "location_relationship_conflict")
        pointer = await db.get(SchoolCatalogActive, 1)
        catalog = (
            await db.get(SchoolCatalogVersion, pointer.catalog_revision)
            if pointer and request.app.state.settings.planning_dataset_provider == "db"
            else None
        )
        if request.app.state.settings.planning_dataset_provider == "db" and catalog is None:
            raise HTTPException(503, "school_catalog_unavailable")
        confirm_location(db, ticket, principal, payload, provider, revision, catalog)
        if request.app.state.settings.planning_dataset_provider == "db":
            pointer = await db.get(SchoolCatalogActive, 1)
            catalog = (
                await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
            )
            if (
                catalog
                and ticket.catalog_revision == catalog.revision
                and ticket.catalog_snapshot.get("catalog_backed")
            ):
                mappings = [
                    m
                    for m in catalog.content["mappings"]
                    if m["category_code"] == ticket.category_code
                ]
                chosen = next((m for m in mappings if m["location_code"] == payload.code), None)
                chosen = chosen or next((m for m in mappings if not m["location_code"]), None)
                department = chosen["department_code"] if chosen else "REVIEW"
                name = next(
                    (d["name"] for d in catalog.content["departments"] if d["code"] == department),
                    None,
                )
                ticket.catalog_snapshot = {
                    **ticket.catalog_snapshot,
                    "department_code": department,
                    "department_name": name,
                    "location_code": payload.code,
                    "location_label": ticket.location_data["selected"]["label"],
                }
                from app.services.routing import suggest

                ticket.routing_data = suggest(
                    ticket.category_code,
                    ticket.location_data,
                    ticket.safety_data,
                    ticket.catalog_snapshot,
                )
        await db.flush()
        await db.refresh(ticket, ["updated_at"])
        return await ticket_view(db, ticket, principal)


async def dispatcher(request, db, *, require_demo=True):
    principal = await authenticate(request, db)
    role(principal, "dispatcher")
    if require_demo:
        demo_only(request.app.state.settings)
    return principal


async def receipt(db, request, principal, action, payload):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 100:
        raise HTTPException(422, "idempotency_key_required")
    scope = f"{principal.user.id}:planning:{action}:{key}"
    hashed = fingerprint(payload.model_dump(mode="json"))
    row = await db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope))
    if row and row.payload_hash != hashed:
        raise HTTPException(409, "idempotency_conflict")
    return scope, hashed, row


def save_receipt(db, scope, hashed, response):
    db.add(
        IdempotencyRecord(
            scope=scope, payload_hash=hashed, response_json=response.model_dump_json()
        )
    )


@router.get("/planning/capabilities", response_model=PlanningCapabilities)
async def capabilities(request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        available = (
            request.app.state.settings.deployment_mode == "demo"
            and request.app.state.settings.planning_dataset_provider == "file"
        )
        travel_data_status = "available" if available else "unknown"
        if not available:
            try:
                provider, revision = await service_dataset(request, db)
            except HTTPException as exc:
                if exc.status_code != 503:
                    raise
                if request.app.state.settings.planning_dataset_provider == "db":
                    pointer = await db.get(SchoolCatalogActive, 1)
                    if pointer is None or pointer.dataset_revision is None:
                        travel_data_status = "unavailable"
            else:
                dataset = provider.dataset
                from app.planning.datasets import has_unverified_travel

                if has_unverified_travel(provider):
                    return PlanningCapabilities(
                        available=False,
                        travel_data_status="unavailable",
                        deployment_mode=request.app.state.settings.deployment_mode,
                        reasons=["verified_planning_data_unavailable", "institution_unverified"],
                        domains=[], regions=[], nodes=[], presets=[],
                    )
                return PlanningCapabilities(
                    available=True,
                    travel_data_status="available",
                    deployment_mode=request.app.state.settings.deployment_mode,
                    data_kind="service",
                    dataset_hash=provider.sha256,
                    dataset_revision=revision,
                    reasons=[],
                    domains=(
                        [
                            field["code"]
                            for field in (
                                await db.get(
                                    SchoolCatalogVersion,
                                    (await db.get(SchoolCatalogActive, 1)).catalog_revision,
                                )
                            ).content["fields"]
                        ]
                        if request.app.state.settings.planning_dataset_provider == "db"
                        else DOMAINS
                    ),
                    regions=sorted({node.region for node in dataset.locations}),
                    nodes=[
                        {"code": node.code, "label": node.label, "region": node.region}
                        for node in dataset.locations
                    ],
                    presets=[
                        {
                            "id": dataset.dataset_id,
                            "label": "운영 자료",
                            "version": dataset.version,
                            "provenance": "운영자가 지정한 계획 자료 · 현장 검증 여부 별도 확인",
                        }
                    ],
                )
        return PlanningCapabilities(
            available=available,
            travel_data_status=travel_data_status,
            deployment_mode=request.app.state.settings.deployment_mode,
            reasons=[]
            if available
            else ["service_dictionary_unavailable", "verified_planning_data_unavailable"],
            domains=(
                [] if request.app.state.settings.planning_dataset_provider == "db" else DOMAINS
            ),
            regions=REGIONS if available else [],
            nodes=NODES if available else [],
            presets=[
                {
                    "id": PRESET_ID,
                    "label": "합성 캠퍼스 이동시간",
                    "version": PRESET_ID,
                    "provenance": PROVENANCE,
                }
            ]
            if available
            else [],
        )


@router.get("/worker-schedules", response_model=list[ScheduleView])
async def schedules(request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        if request.app.state.settings.planning_dataset_provider != "db":
            await planning_context(request, db)
        from app.staff_codes import eligible_workers, staff_codes

        workers = await eligible_workers(db, request.app.state.settings)
        codes = staff_codes(workers)
        result = []
        for w in workers:
            profile = (
                await db.get(WorkerProfile, w.id)
                if request.app.state.settings.planning_dataset_provider == "db"
                else None
            )
            value = schedule_view(w, await db.get(WorkerSchedule, w.id), codes[w.id])
            value.profile_revision = profile.revision if profile else 0
            result.append(value)
        return result


@router.put("/worker-schedules/{worker_id}", response_model=ScheduleView)
async def update_schedule(worker_id: uuid.UUID, payload: ScheduleInput, request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        try:
            if request.app.state.settings.planning_dataset_provider == "db":
                provider, revision = await service_dataset(request, db)
            else:
                provider, revision = await planning_context(request, db)
        except HTTPException as exc:
            if (
                exc.status_code != 503
                or payload.enabled
                or request.app.state.settings.planning_dataset_provider != "db"
            ):
                raise
            provider, revision = None, None
        worker = await db.get(User, worker_id, with_for_update=True)
        if not worker or worker.role != "worker":
            raise HTTPException(422, "invalid_worker")
        db_provider = request.app.state.settings.planning_dataset_provider == "db"
        if not db_provider and not set(payload.skills) <= set(DOMAINS):
            raise HTTPException(422, "unregistered_schedule_skill")
        profile = (
            await db.get(WorkerProfile, worker_id, with_for_update=True) if db_provider else None
        )
        if db_provider and payload.expected_profile_revision != (
            profile.revision if profile else 0
        ):
            raise HTTPException(409, "profile_revision_conflict")
        if db_provider and payload.enabled:
            pointer = await db.get(SchoolCatalogActive, 1)
            catalog = (
                await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
            )
            if not catalog or not profile or profile.catalog_revision != pointer.catalog_revision:
                raise HTTPException(409, "profile_catalog_stale")
            if not set(payload.skills) <= set(profile.field_codes):
                raise HTTPException(422, "schedule_skills_outside_profile")
        nodes = (
            NODES
            if provider is None
            else [{"code": n.code, "region": n.region} for n in provider.dataset.locations]
        )
        if provider is None:
            if not db_provider and (
                payload.dataset_hash is not None or payload.dataset_revision is not None
            ):
                raise HTTPException(422, "invalid_demo_planning_dataset")
        elif payload.enabled and (
            payload.dataset_hash != provider.sha256 or payload.dataset_revision != revision
        ):
            raise HTTPException(409, "planning_dataset_changed")
        if payload.enabled and (
            payload.start_node not in {n["code"] for n in nodes}
            or payload.end_node not in {n["code"] for n in nodes}
            or payload.region not in {n["region"] for n in nodes}
        ):
            raise HTTPException(422, "unregistered_planning_node")
        row = await db.get(WorkerSchedule, worker_id, with_for_update=True)
        if payload.expected_revision != (row.revision if row else 0):
            raise HTTPException(409, "schedule_revision_conflict")
        # A live plan's time windows must remain true until completion or whole-plan cancellation.
        await require_no_worker_reservations(db, worker_id)
        data = payload.model_dump(
            mode="json", exclude={"expected_revision", "expected_profile_revision"}
        )
        if row and "days_off" not in payload.model_fields_set:
            data["days_off"] = (row.data or {}).get("days_off", [])
        if "hours" not in payload.model_fields_set:
            data["hours"] = row.data.get("hours") if row and row.data else None
        if data["hours"] is None:
            data.pop("hours")
        if row:
            row.revision += 1
            row.data = data
        else:
            row = WorkerSchedule(worker_id=worker_id, revision=1, data=data)
            db.add(row)
        await db.flush()
        from app.staff_codes import eligible_workers, staff_codes

        codes = staff_codes(await eligible_workers(db, request.app.state.settings))
        if worker.id not in codes:
            raise HTTPException(422, "invalid_worker")
        result = schedule_view(worker, row, codes[worker.id])
        result.profile_revision = profile.revision if profile else 0
        return result


@router.post("/dispatch-plans", response_model=DispatchPlanView)
async def create_plan(payload: PlanCreate, request: Request):
    database = request.app.state.database
    # Non-waiting bounded calculation capacity, shared by all requests in this API process.
    if getattr(request.app.state, "planning_busy", False):
        raise HTTPException(429, "planning_capacity_busy")
    request.app.state.planning_busy = True
    try:
        async with database.transaction() as db:
            principal = await dispatcher(request, db, require_demo=False)
            provider, revision = await planning_context(request, db)
            validate_dataset_request(payload, provider, revision)
            scope, hashed, old = await receipt(db, request, principal, "create", payload)
            if old:
                return plan_view(
                    await get_plan(db, DispatchPlanView.model_validate_json(old.response_json).id)
                )
            valid_time(payload, now())
            actor_id, actor_epoch = principal.user.id, principal.user.auth_epoch
            problem, snapshot, excluded = await capture(db, payload, provider, revision)
            captured_at = now()
        # No DB transaction/gate held while CPU-bound OR-Tools runs.
        solve = {
            "fifo": fifo,
            "urgency_fifo": urgency_fifo,
            "nearest": nearest,
            "ortools": solve_ortools,
        }[payload.policy]
        result = await run_in_threadpool(solve, problem)
        validate_plan(problem, result)
        async with database.transaction() as db:
            principal = await dispatcher(request, db, require_demo=False)
            provider, revision = await planning_context(request, db)
            if principal.user.id != actor_id or principal.user.auth_epoch != actor_epoch:
                raise HTTPException(409, "planning_actor_stale")
            _, current, _ = await capture(db, payload, provider, revision)
            if current != snapshot:
                raise HTTPException(409, "planning_snapshot_stale")
            expires = min(captured_at + timedelta(minutes=10), payload.plan_start_at)
            if now() >= expires:
                raise HTTPException(409, "planning_snapshot_expired")
            # A second request may have committed while this request calculated.
            scope, hashed, old = await receipt(db, request, principal, "create", payload)
            if old:
                return plan_view(
                    await get_plan(db, DispatchPlanView.model_validate_json(old.response_json).id)
                )
            plan = DispatchPlan(
                created_by=actor_id,
                captured_at=captured_at,
                plan_start_at=payload.plan_start_at,
                expires_at=expires,
                input_data=problem.model_dump(mode="json"),
                result_data=result.model_dump(mode="json"),
                snapshot_data=snapshot,
                request_data=payload.model_dump(mode="json"),
                excluded=excluded,
                input_hash=result.input_hash,
            )
            db.add(plan)
            await db.flush()
            view = plan_view(plan)
            save_receipt(db, scope, hashed, view)
            return view
    finally:
        request.app.state.planning_busy = False


@router.get("/dispatch-plans/{plan_id}", response_model=PlanApprovalView)
async def read_plan(plan_id: uuid.UUID, request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db, require_demo=False)
        plan = await get_plan(db, plan_id)
        return PlanApprovalView(plan=plan_view(plan), assignments=await assignments_view(db, plan))


@router.post("/dispatch-plans/{plan_id}/approve", response_model=PlanApprovalView)
async def approve_plan(plan_id: uuid.UUID, payload: PlanApprove, request: Request):
    settings = request.app.state.settings
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        provider, revision = await planning_context(request, db)
        scope, hashed, old = await receipt(db, request, principal, f"approve:{plan_id}", payload)
        if old:
            replay_plan = await get_plan(db, plan_id)
            return PlanApprovalView(
                plan=plan_view(replay_plan), assignments=await assignments_view(db, replay_plan)
            )
        plan = await get_plan(db, plan_id)
        if (
            plan.status != "proposed"
            or payload.expected_revision != plan.revision
            or (payload.input_hash != plan.input_hash)
        ):
            raise HTTPException(409, "plan_revision_conflict")
        if plan.kind == "insertion":
            if provider is not None:
                raise HTTPException(503, "service_insertion_unavailable")
            from app.insertion import approve_insertion

            response = await approve_insertion(db, plan, payload, principal, settings)
            save_receipt(db, scope, hashed, response)
            return response
        await current_snapshot(db, plan, provider, revision)
        problem = PlanningInput.model_validate_json(__import__("json").dumps(plan.input_data))
        result = PlanResult.model_validate_json(__import__("json").dumps(plan.result_data))
        validate_plan(problem, result)
        if not any(route.visits for route in result.routes):
            raise HTTPException(409, "plan_has_no_assignable_routes")
        worker_ids = [uuid.UUID(r.worker_id) for r in result.routes]
        users = {}
        for wid in sorted(worker_ids, key=str):
            users[wid] = await db.get(User, wid, with_for_update=True)
        schedule_rows = await lock_schedules(db, worker_ids)
        ticket_ids = sorted(
            [uuid.UUID(v.ticket_id) for r in result.routes for v in r.visits], key=str
        )
        tickets = {tid: await db.get(Ticket, tid, with_for_update=True) for tid in ticket_ids}
        # Recheck under the gate immediately before all-or-nothing writes.
        await current_snapshot(db, plan, provider, revision)
        from app.dispatching import is_day_off_during

        if any(is_day_off_during(
            schedule_rows[uuid.UUID(route.worker_id)].data,
            plan.plan_start_at + timedelta(minutes=visit.arrival_min),
            plan.plan_start_at + timedelta(minutes=visit.completion_min),
        ) for route in result.routes for visit in route.visits):
            raise HTTPException(409, "worker_day_off")
        for route in result.routes:
            previous_id = None
            worker = users[uuid.UUID(route.worker_id)]
            for sequence, visit in enumerate(route.visits):
                ticket = tickets[uuid.UUID(visit.ticket_id)]
                # An undisclosed random capability is not a delivered worker link.
                assignment = Assignment(
                    id=uuid.uuid4(),
                    catalog_revision=ticket.catalog_revision,
                    catalog_snapshot={
                        **ticket.catalog_snapshot,
                        "worker_name": worker.display_name,
                    },
                    active_plan_id=plan.id,
                    predecessor_id=previous_id,
                    ticket_id=ticket.id,
                    worker_id=worker.id,
                    auth_epoch=worker.auth_epoch,
                    plan_id=plan.id,
                    plan_sequence=sequence,
                    planned_arrival_min=visit.arrival_min,
                    planned_completion_min=visit.completion_min,
                    token_hash=token_digest(settings, secrets.token_urlsafe(32)),
                    expires_at=now() + timedelta(hours=settings.worker_token_hours),
                    revoked=False,
                )
                db.add(assignment)
                await db.flush()
                db.add(
                    PlanMembership(
                        plan_id=plan.id,
                        assignment_id=assignment.id,
                        kind="new",
                        sequence=sequence,
                        timing_data={},
                    )
                )
                previous_id = assignment.id
                transition(db, ticket, principal.user, "assigned", payload.reason, "assigned")
            schedule_rows[worker.id].revision += 1
        plan.status, plan.revision = "approved", plan.revision + 1
        plan.approved_by, plan.approved_at, plan.approval_reason = (
            principal.user.id,
            now(),
            payload.reason,
        )
        await db.flush()
        response = PlanApprovalView(
            plan=plan_view(plan), assignments=await assignments_view(db, plan)
        )
        save_receipt(db, scope, hashed, response)
        return response


@router.post("/assignments/{assignment_id}/credential", response_model=CredentialView)
async def issue_credential(assignment_id: uuid.UUID, payload: CredentialInput, request: Request):
    settings = request.app.state.settings
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        if settings.deployment_mode == "service":
            raise HTTPException(403, "worker_credential_unavailable")
        assignment = await db.get(Assignment, assignment_id)
        if not assignment:
            raise HTTPException(404, "assignment_not_found")
        worker = await db.get(User, assignment.worker_id, with_for_update=True)
        ticket = await db.get(Ticket, assignment.ticket_id, with_for_update=True)
        from app.auth_service import worker_eligible

        if not worker_eligible(worker, settings):
            raise HTTPException(409, "assignment_not_current")
        if not assignment_current(assignment, worker) or ticket.status not in {
            "assigned",
            "in_progress",
        }:
            raise HTTPException(409, "assignment_not_current")
        old = await db.scalar(
            select(AssignmentCredentialReceipt).where(
                AssignmentCredentialReceipt.assignment_id == assignment.id,
                AssignmentCredentialReceipt.request_id == payload.request_id,
            )
        )
        if old:
            if (
                old.actor_id != principal.user.id
                or old.expected_revision != payload.expected_credential_revision
            ):
                raise HTTPException(409, "credential_request_conflict")
            return CredentialView(
                assignment_id=assignment.id,
                credential_revision=assignment.credential_revision,
                status="already_issued",
            )
        if assignment.credential_revision != payload.expected_credential_revision:
            raise HTTPException(409, "credential_revision_conflict")
        token = secrets.token_urlsafe(32)
        assignment.token_hash = token_digest(settings, token)
        assignment.credential_revision += 1
        assignment.expires_at = now() + timedelta(hours=settings.worker_token_hours)
        db.add(
            AssignmentCredentialReceipt(
                assignment_id=assignment.id,
                request_id=payload.request_id,
                actor_id=principal.user.id,
                expected_revision=payload.expected_credential_revision,
                issued_revision=assignment.credential_revision,
            )
        )
        from app.models import TicketEvent

        db.add(
            TicketEvent(
                ticket_id=ticket.id,
                actor_id=principal.user.id,
                event_type="worker_credential_issued",
                reason="작업 링크 명시 발급·이전 링크 철회",
            )
        )
        return CredentialView(
            assignment_id=assignment.id,
            credential_revision=assignment.credential_revision,
            status="issued",
            worker_token=token,
        )


@router.post("/dispatch-plans/{plan_id}/cancel", response_model=PlanApprovalView)
async def cancel_plan(plan_id: uuid.UUID, payload: PlanApprove, request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db, require_demo=False)
        scope, hashed, old = await receipt(db, request, principal, f"cancel:{plan_id}", payload)
        if old:
            replay_plan = await get_plan(db, plan_id)
            return PlanApprovalView(
                plan=plan_view(replay_plan), assignments=await assignments_view(db, replay_plan)
            )
        plan = await get_plan(db, plan_id)
        if (
            plan.revision != payload.expected_revision
            or plan.input_hash != payload.input_hash
            or (plan.status not in {"proposed", "approved"})
        ):
            raise HTTPException(409, "plan_revision_conflict")
        assignments = list(
            (
                await db.scalars(
                    select(Assignment)
                    .where(Assignment.active_plan_id == plan.id)
                    .order_by(Assignment.ticket_id)
                )
            ).all()
        )
        for a in assignments:
            t = await db.get(Ticket, a.ticket_id)
            pending = await db.scalar(
                select(CompletionReport.id)
                .where(
                    CompletionReport.ticket_id == t.id,
                    CompletionReport.reviewed.is_(False),
                    CompletionReport.superseded_at.is_(None),
                    CompletionReport.analysis_status != "dismissed",
                )
                .limit(1)
            )
            from app.execution import phase

            if phase(a, t) in {"moving", "working"} or pending:
                raise HTTPException(409, "plan_has_work_or_pending_reports")
        rows = await lock_schedules(db, [a.worker_id for a in assignments])
        for a in assignments:
            t = await db.get(Ticket, a.ticket_id, with_for_update=True)
            if not a.visit_ended_at and (
                (not a.revoked and t.status == "assigned")
                or (a.stopped_at and t.status in {"assigned", "in_progress"})
            ):
                a.active_plan_id = None
                a.revoked = True
                transition(db, t, principal.user, "plan_cancelled", payload.reason, "confirmed")
        for row in rows.values():
            row.revision += 1
        plan.cancellation_data = {
            "actor_id": str(principal.user.id),
            "reason": payload.reason,
            "at": now().isoformat(),
        }
        plan.status, plan.revision = "cancelled", plan.revision + 1
        from app.models import TicketEvent

        for a in assignments:
            db.add(
                TicketEvent(
                    ticket_id=a.ticket_id,
                    actor_id=principal.user.id,
                    event_type="dispatch_plan_cancelled",
                    reason=payload.reason,
                )
            )
        await db.flush()
        response = PlanApprovalView(
            plan=plan_view(plan), assignments=await assignments_view(db, plan)
        )
        save_receipt(db, scope, hashed, response)
        return response
