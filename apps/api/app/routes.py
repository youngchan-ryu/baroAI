import json
import math
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy import func, select, text, update

from app.catalog import CATEGORIES, CATEGORY_MAP
from app.location_search import LocationSearchQuery, LocationSearchResult, search_locations
from app.location_workflow import (
    check_version,
    initialize_location,
    preserve_legacy_input,
    refresh_safety,
    save_location,
    update_location,
)
from app.models import (
    Assignment,
    AuthSession,
    CompletionReport,
    DeploymentIdentity,
    ImageApproval,
    ImageVariant,
    ProcessingJob,
    SchoolCatalogActive,
    Ticket,
    TicketEvent,
    Upload,
    User,
    WorkerProfile,
    WorkerSchedule,
)
from app.protection import get_upload, process_upload
from app.schemas import (
    ApprovalInput,
    AssignInput,
    AssignmentWorkerCandidates,
    AssignView,
    CatalogView,
    ConfirmAssignInput,
    ConfirmAssignView,
    CorrectionInput,
    DemoLogin,
    DemoWorkers,
    ExecutionInput,
    LocationResolve,
    LocationUpdate,
    LocationView,
    PhotoView,
    ProcessInput,
    Reason,
    ReportConfirmInput,
    ReportDismissInput,
    ReporterIdentity,
    ReportInput,
    ReportStructure,
    ReviewInput,
    SessionView,
    TicketCreate,
    TicketList,
    TicketPatch,
    TicketPhotoCoordinatesView,
    TicketView,
    UrgencyInput,
    UserView,
    WorkerAssignmentList,
    WorkerAssignmentSummary,
    WorkerAssignmentView,
    WorkerOwnHoursInput,
    WorkerOwnSettings,
    WorkerView,
)
from app.security import (
    COOKIE,
    assignment_current,
    authenticate,
    cookie_name,
    current_assignment_clause,
    digest,
    lock_report_worker,
    lock_worker_user,
    locked_assignment,
    now,
    require_origin,
    role,
    secure_session_cookie,
    token_digest,
)
from app.services.location import resolve
from app.storage import MAX_FILE, LocalPrivateStorage, normalize_image
from app.tickets import (
    bind_uploads,
    get_ticket,
    idempotency,
    photo_view,
    require_state,
    save_idempotency,
    ticket_view,
    transition,
)

router = APIRouter(prefix="/v1")


def _demo_db_locations(settings):
    return settings.deployment_mode == "demo" and settings.planning_dataset_provider == "db"


PERSONAS = {
    "reporter_a": ("reporter", "합성 신고자 A"),
    "reporter_b": ("reporter", "합성 신고자 B"),
    "dispatcher": ("dispatcher", "합성 접수 담당자"),
    "worker_a": ("worker", "합성 작업자 가"),
    "worker_b": ("worker", "합성 작업자 나"),
}


async def seed_personas(db, settings):
    if settings.deployment_mode != "demo" or not settings.demo_auth:
        raise HTTPException(404, "demo_unavailable")
    from app.auth_service import ensure_namespace

    await ensure_namespace(db, settings)
    # Multiple login/catalog requests can safely race on first run.
    await db.execute(text("SELECT pg_advisory_xact_lock(24002201)"))
    for persona, (user_role, name) in PERSONAS.items():
        if not await db.scalar(select(User).where(User.persona == persona)):
            db.add(User(role=user_role, display_name=name, persona=persona))
    await db.flush()


def session_view(user, session, settings):
    return SessionView(
        user=UserView(id=user.id, role=user.role, display_name=user.display_name),
        csrf_token=session.csrf_token,
        auth_mode={
            "service": "email_otp",
            "prototype": "prototype_admin",
            "demo": "demo",
            "public_demo": "public_session",
        }[settings.deployment_mode],
        data_scope=settings.deployment_mode,
    )


@router.get("/auth/demo-workers", response_model=DemoWorkers)
async def demo_workers(request: Request):
    from app.auth_service import demo_worker_session_eligible

    settings = request.app.state.settings
    if settings.deployment_mode != "demo" or settings.app_env != "dev" or not settings.demo_auth:
        raise HTTPException(404, "demo_unavailable")
    if settings.planning_dataset_provider != "db":
        return DemoWorkers(personas=[])
    async with request.app.state.database.transaction() as db:
        identity = await db.get(DeploymentIdentity, 1)
        if not identity or identity.mode != "demo" or identity.namespace != "demo":
            return DemoWorkers(personas=[])
        users = (await db.scalars(
            select(User).where(
                User.persona.in_(["worker_c", "worker_d", "worker_e", "worker_f", "worker_g"])
            )
        )).all()
        return DemoWorkers(personas=sorted(
            user.persona for user in users if demo_worker_session_eligible(user, settings)
        ))


@router.post("/auth/demo", response_model=SessionView)
async def demo_login(payload: DemoLogin, request: Request, response: Response):
    settings = request.app.state.settings
    if settings.deployment_mode != "demo" or settings.app_env != "dev" or not settings.demo_auth:
        raise HTTPException(404, "demo_unavailable")
    require_origin(request)
    async with request.app.state.database.transaction() as db:
        if payload.persona in {"worker_c", "worker_d", "worker_e", "worker_f", "worker_g"}:
            from app.auth_service import demo_worker_session_eligible
            from app.dispatch_lock import dispatch_lock

            await dispatch_lock(db)
            identity = await db.get(DeploymentIdentity, 1)
            user = await db.scalar(
                select(User).where(User.persona == payload.persona).with_for_update()
            )
            if (not identity or identity.mode != "demo" or identity.namespace != "demo"
                    or not demo_worker_session_eligible(user, settings)):
                raise HTTPException(404, "demo_unavailable")
        else:
            await seed_personas(db, settings)
            user = await db.scalar(select(User).where(User.persona == payload.persona))
        previous = request.cookies.get(COOKIE)
        if previous:
            await db.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == digest(previous))
                .values(revoked=True)
            )
        token = secrets.token_urlsafe(32)
        session = AuthSession(
            user_id=user.id,
            token_hash=digest(token),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now() + timedelta(hours=settings.session_hours),
            revoked=False,
        )
        db.add(session)
        await db.flush()
        result = session_view(user, session, settings)
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookie or settings.app_env == "prod",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/auth/me", response_model=SessionView)
async def me(request: Request, response: Response):
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        response.headers["Cache-Control"] = "no-store"
        return session_view(principal.user, principal.session, request.app.state.settings)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        principal.session.revoked = True
    response.delete_cookie(
        cookie_name(request.app.state.settings), path="/", secure=secure_session_cookie(request)
    )


@router.get("/catalog", response_model=CatalogView)
async def catalog(request: Request):
    async with request.app.state.database.transaction() as db:
        await authenticate(request, db)
        from app.auth_service import local_staff_identity
        from app.staff_codes import eligible_workers, staff_codes

        workers = await eligible_workers(db, request.app.state.settings)
        codes = staff_codes(workers)
        categories = CATEGORIES
        catalog_revision = None
        if request.app.state.settings.planning_dataset_provider == "db":
            from app.school_catalog_routes import active_catalog

            pointer, active = await active_catalog(db)
            catalog_revision = pointer.catalog_revision if pointer else 0
            categories = [CATEGORY_MAP["UNKNOWN"]]
            if active:
                defaults = {
                    m["category_code"]: m["department_code"]
                    for m in active.content["mappings"]
                    if not m["location_code"]
                }
                categories += [
                    {
                        "code": c["code"],
                        "label": c["name"],
                        "domain": c["field_code"],
                        "department_code": defaults.get(c["code"], "REVIEW"),
                    }
                    for c in active.content["categories"]
                ]
        return CatalogView(
            categories=categories,
            catalog_revision=catalog_revision,
            workers=[
                WorkerView(
                    id=w.id, display_name=w.display_name, staff_code=codes[w.id],
                    is_local_test_account=local_staff_identity(w, request.app.state.settings),
                )
                for w in workers
            ],
        )


async def _use_managed_photos(request):
    from app.protection import managed_db_present

    async with request.app.state.database.transaction() as db:
        return await managed_db_present(db)


@asynccontextmanager
async def _photo_aware_transaction(request):
    if await _use_managed_photos(request):
        from app.managed_access import access_transaction

        context = access_transaction(request.app.state.database, request.app.state.settings)
        try:
            db, _, _ = await context.__aenter__()
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            async with request.app.state.database.transaction() as db:
                yield db
            return
        try:
            yield db
        except BaseException as exc:
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await context.__aexit__(None, None, None)
    else:
        async with request.app.state.database.transaction() as db:
            yield db


@router.post("/uploads", response_model=PhotoView)
async def upload(request: Request, file: Annotated[UploadFile, File()]):
    if await _use_managed_photos(request):
        from app.managed_photo import managed_operation_upload

        return await managed_operation_upload(request, file)
    storage = LocalPrivateStorage(request.app.state.settings.storage_path)
    key = f"{uuid.uuid4().hex}.jpg"
    wrote = False
    try:
        async with request.app.state.database.transaction() as db:
            principal = await authenticate(request, db, worker_allowed=True)
            if principal.assignment:
                ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
                assignment = await locked_assignment(db, principal, principal.assignment.id)
                correction = await db.scalar(select(CompletionReport.id).where(
                    CompletionReport.assignment_id == assignment.id,
                    CompletionReport.reviewed.is_(False),
                    CompletionReport.superseded_at.is_(None),
                    CompletionReport.correction_requested_at.is_not(None),
                ).limit(1))
                if (
                    ticket.status not in {"assigned", "in_progress"}
                    or (assignment.visit_ended_at and not correction)
                    or assignment.stopped_at
                ):
                    raise HTTPException(409, "worker_upload_closed")
            elif principal.user.role != "reporter":
                raise HTTPException(403, "role_forbidden")
            from app.protection import require_legacy_storage

            await require_legacy_storage(db, storage, allow_new_storage=True)
            raw = await file.read(MAX_FILE + 1)
            from starlette.concurrency import run_in_threadpool

            normalized = await run_in_threadpool(normalize_image, raw)
            signal = None
            if (request.headers.get("x-baro-photo-location") == "intake"
                    and not principal.assignment):
                from app.location_workflow import photo_location_signal

                signal = await photo_location_signal(db, raw)
            coordinates = None
            if (request.app.state.settings.deployment_mode == "service"
                    and not principal.assignment
                    and request.headers.get("x-baro-photo-coordinates") == "consent"):
                from app.storage import exif_gps

                point = exif_gps(raw)
                if point is not None:
                    coordinates = {"latitude": point[0], "longitude": point[1]}
            del raw
            try:
                await run_in_threadpool(storage.put, key, normalized)
            except OSError:
                raise HTTPException(503, "photo_storage_unavailable") from None
            wrote = True
            record = Upload(
                owner_id=principal.user.id,
                assignment_id=principal.assignment.id if principal.assignment else None,
                purpose="report" if principal.assignment else "ticket",
                location_signal=signal,
                photo_coordinates=coordinates,
                object_key=key,
                size=len(normalized),
                content_type="image/jpeg",
            )
            db.add(record)
            await db.flush()
            await process_upload(db, record, storage, [])
            result = await photo_view(db, record)
        return result
    except BaseException:
        if wrote:
            try:
                await run_in_threadpool(storage.delete, key)
            except OSError:
                pass
        raise
    finally:
        await file.close()


@router.get("/tickets/{ticket_id}/photo-coordinates", response_model=TicketPhotoCoordinatesView)
async def ticket_photo_coordinates(ticket_id: uuid.UUID, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if request.app.state.settings.deployment_mode != "service":
        raise HTTPException(404, "ticket_not_found")
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        if principal.assignment or principal.user.role != "dispatcher":
            raise HTTPException(404, "ticket_not_found")
        await get_ticket(db, ticket_id, principal)
        uploads = (await db.scalars(select(Upload).where(
            Upload.ticket_id == ticket_id, Upload.purpose == "ticket",
            Upload.assignment_id.is_(None), Upload.report_id.is_(None),
            Upload.information_request_id.is_(None), Upload.deleted.is_(False),
            Upload.delete_after > now(), Upload.photo_coordinates.is_not(None),
        ))).all()
        photos = [{"id": str(upload.id), "delete_after": upload.delete_after,
                   **upload.photo_coordinates}
                  for upload in uploads if isinstance(upload.photo_coordinates, dict)]
        if photos:
            db.add(TicketEvent(ticket_id=ticket_id, actor_id=principal.user.id,
                               event_type="photo_coordinates_viewed",
                               reason="사진 촬영 위치 조회"))
        return {"photos": photos}


@router.get("/uploads/{upload_id}")
async def download(upload_id: uuid.UUID, request: Request):
    if await _use_managed_photos(request):
        from app.managed_access import managed_image

        return await managed_image(request, upload_id)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        record = await get_upload(db, upload_id, principal)
        return await image_response(db, record, request)


@router.post("/tickets", response_model=TicketView)
async def create_ticket(payload: TicketCreate, request: Request):
    if request.app.state.settings.deployment_mode == "public_demo":
        if not payload.text.strip():
            raise HTTPException(422, "text_required")
        if payload.upload_ids or payload.gps is not None:
            raise HTTPException(403, "public_input_unavailable")
    if payload.upload_ids and await _use_managed_photos(request):
        from app.managed_access import managed_create_ticket

        return await managed_create_ticket(request, payload)
    async with _photo_aware_transaction(request) as db:
        return await _create_ticket_tx(db, payload, request)


async def _create_ticket_tx(db, payload, request, *, managed=None):
    principal = await authenticate(request, db)
    role(principal, "reporter")
    from app.services.llm import runtime_mode

    if runtime_mode(request.app.state.settings) == "unavailable":
        raise HTTPException(503, "live_processing_unavailable")
    scope, payload_hash, prior = await idempotency(
        db, request, f"create:{principal.actor_scope}", payload
    )
    if prior:
        ticket = await get_ticket(db, prior.id, principal, lock=True)
        return await ticket_view(db, ticket, principal)
    if request.app.state.settings.deployment_mode == "public_demo":
        from app.public_limits import check_ticket

        await check_ticket(db, request, principal)
    ticket = Ticket(
        analysis_revision=1,
        reporter_id=principal.user.id,
        information_mail_allowed=payload.information_mail_allowed is True,
        description=payload.text,
        location_text=payload.location_text,
        category_code="UNKNOWN",
        ai_status="unknown"
        if request.app.state.settings.deployment_mode == "public_demo"
        else "queued",
        status="created",
        public_no=f"B-{uuid.uuid4().hex[:12].upper()}",
    )
    if (request.app.state.settings.deployment_mode in {"service", "public_demo"}
            or _demo_db_locations(request.app.state.settings)):
        from app.auth_service import service_location

        ticket.location_history = [{"kind": "initial", "text": payload.location_text}]
        ticket.location_answer_count = ticket.location_version = ticket.safety_version = 0
        refresh_safety(ticket)
        save_location(ticket, service_location())
        if request.app.state.settings.deployment_mode != "public_demo":
            from app.location_workflow import record_catalog_qr

            await record_catalog_qr(db, ticket, payload.loc_hint)
            if payload.gps is not None:
                from app.location_workflow import preview_service_location
                from app.schemas import LocationResolve

                preview = await preview_service_location(
                    db, LocationResolve(gps=payload.gps), now()
                )
                codes = [item.code for item in preview.candidates if "current_gps" in item.sources]
                if codes and preview.catalog_revision is not None:
                    ticket.location_history = ticket.location_history + [{
                        "kind": "current_gps", "codes": codes,
                        "catalog_revision": preview.catalog_revision,
                    }]
    else:
        initialize_location(ticket, payload)
    if getattr(request.app.state.settings, "booth_free_input_live", False):
        ticket.safety_data = {**(ticket.safety_data or {}), "booth_live_intake": True}
    db.add(ticket)
    await db.flush()
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=principal.user.id,
            event_type="created",
            created_at=now(),
            reason=(
                # Live mode must not keep claiming replay in the audit trail.
                "서비스 접수 · AI 초안 대기 · 사람 확인 필요"
                if request.app.state.settings.deployment_mode == "service"
                and not request.app.state.settings.llm_fake
                else "서비스 접수 · 재생 모드 · AI 실호출 아님"
                if request.app.state.settings.deployment_mode == "service"
                else "공개 시연 접수 · AI 판단 미정 · 사람 확인 필요"
                if request.app.state.settings.deployment_mode == "public_demo"
                else "접수 · AI 초안 대기"
                if not request.app.state.settings.llm_fake
                else "접수 · 재생 모드"
            ),
            from_status=None,
            to_status="created",
        )
    )
    if ticket.information_mail_allowed:
        db.add(TicketEvent(ticket_id=ticket.id, actor_id=principal.user.id,
                           event_type="information_mail_consent", reason="추가 정보 요청 메일 동의",
                           from_status=ticket.status, to_status=ticket.status))
    transition(
        db,
        ticket,
        principal.user,
        "received",
        "접수 저장 · AI 판단 미정 · 사람 확인 필요"
        if request.app.state.settings.deployment_mode == "public_demo"
        else "접수 저장 · AI 분석 대기 · 사람 확인 필요"
        if not request.app.state.settings.llm_fake
        else "접수 저장 · 재생 분석 대기 · 사람 확인 필요",
        "received",
    )
    if managed:
        from app.managed_access import _bind

        await _bind(db, *managed, payload.upload_ids, principal, ticket)
        if payload.upload_ids:
            ticket.analysis_revision += 1
            ticket.updated_at = now()
    else:
        if payload.upload_ids:
            from app.protection import require_legacy_storage

            await require_legacy_storage(
                db, LocalPrivateStorage(request.app.state.settings.storage_path)
            )
        await bind_uploads(db, payload.upload_ids, principal, ticket)
    if payload.upload_ids and request.app.state.settings.deployment_mode != "public_demo":
        from app.models import SchoolCatalogActive

        pointer = await db.get(SchoolCatalogActive, 1)
        for upload_id in payload.upload_ids:
            upload = await db.get(Upload, upload_id)
            signal = upload.location_signal if upload else None
            if signal and pointer and signal.get("catalog_revision") == pointer.catalog_revision:
                ticket.location_history = ticket.location_history + [
                    {"kind": "photo_gps", **signal}
                ]
                ticket.updated_at = now()
    if request.app.state.settings.deployment_mode != "public_demo":
        job = ProcessingJob(ticket_id=ticket.id, input_revision=ticket.analysis_revision)
        if not request.app.state.settings.llm_fake and not managed and payload.upload_ids:
            from app.jobs import ticket_photo_pending

            if await ticket_photo_pending(db, ticket.id):
                job.next_attempt_at = now() + timedelta(
                    seconds=request.app.state.settings.llm_photo_wait_s
                )
        db.add(job)
    await db.flush()
    result = await ticket_view(db, ticket, principal)
    save_idempotency(db, scope, payload_hash, result)
    return result


@router.get("/tickets", response_model=TicketList)
async def list_tickets(request: Request, operations_start: date | None = None,
                       operations_end: date | None = None, category_group: str | None = None,
                       building: str | None = None, urgency: int | None = None,
                       category_code: str | None = None):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        query = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())
        if request.app.state.settings.deployment_mode == "public_demo":
            from app.tickets import public_visible_ticket

            query = query.where(public_visible_ticket(now()))
        filters_requested = any(value is not None for value in (
            operations_start, operations_end, category_group, building, urgency, category_code
        ))
        if filters_requested:
            role(principal, "dispatcher")
            if operations_start is None or operations_end is None:
                raise HTTPException(422, "operations_period_required")
            from app.operations_filters import conditions

            query = query.where(*conditions(
                operations_start, operations_end, category_group, building, urgency, category_code
            ))
        if principal.user.role != "dispatcher":
            query = query.where(Ticket.reporter_id == principal.user.id)
        items = (await db.scalars(query)).all()
        return TicketList(items=[await ticket_view(db, t, principal) for t in items])


@router.get("/tickets/{ticket_id}", response_model=TicketView)
async def read_ticket(ticket_id: uuid.UUID, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        ticket = await get_ticket(db, ticket_id, principal)
        view = await ticket_view(db, ticket, principal, include_map=True)
        if (
            request.app.state.settings.deployment_mode == "service"
            and principal.user.role == "dispatcher"
            and principal.assignment is None
        ):
            reporter = await db.get(User, ticket.reporter_id)
            if reporter is not None:
                view.reporter_identity = ReporterIdentity(
                    id=reporter.id, email=reporter.verified_email
                )
        return view


@router.patch("/tickets/{ticket_id}", response_model=TicketView)
async def edit_ticket(ticket_id: uuid.UUID, payload: TicketPatch, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        scope, payload_hash, prior = await idempotency(
            db, request, f"edit:{ticket.id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, ticket, principal)
        require_state(ticket, "received", "confirmed")
        check_version(ticket, payload.expected_location_version)
        if (
            payload.expected_category_code is not None
            and payload.expected_category_code != ticket.category_code
        ):
            raise HTTPException(409, "category_revision_conflict")
        category_code = payload.category_code or ticket.category_code
        location_text = payload.location_text or ticket.location_text
        category = CATEGORY_MAP.get(category_code)
        catalog_revision = None
        db_catalog = request.app.state.settings.planning_dataset_provider == "db"
        active = None
        if db_catalog:
            from app.school_catalog_routes import active_catalog

            pointer, active = await active_catalog(db)
            catalog_revision = pointer.catalog_revision if pointer else 0
            if payload.expected_catalog_revision is None or payload.expected_category_code is None:
                raise HTTPException(422, "catalog_precondition_required")
            if payload.expected_catalog_revision != catalog_revision:
                raise HTTPException(409, "catalog_revision_conflict")
            category = CATEGORY_MAP["UNKNOWN"] if category_code == "UNKNOWN" else None
            if active:
                item = next(
                    (c for c in active.content["categories"] if c["code"] == category_code),
                    None,
                )
                if item:
                    mapping = next(
                        (
                            m
                            for m in active.content["mappings"]
                            if m["category_code"] == item["code"] and not m["location_code"]
                        ),
                        None,
                    )
                    category = {
                        "code": item["code"],
                        "label": item["name"],
                        "domain": item["field_code"],
                        "department_code": mapping["department_code"] if mapping else "REVIEW",
                    }
        if payload.category_code is None:
            category = ticket.catalog_snapshot or category
            catalog_revision = ticket.catalog_revision
        if not category and payload.category_code is not None:
            raise HTTPException(422, "unknown_category")
        preserve_legacy_input(ticket)
        before_category = ticket.category_code
        before_location = ticket.location_data
        department_name = None
        if db_catalog and active and payload.category_code and category_code != "UNKNOWN":
            location_code = (
                (ticket.location_data.get("selected") or {}).get("code")
                if ticket.location_text == location_text
                else None
            )
            mappings = [
                m for m in active.content["mappings"] if m["category_code"] == category_code
            ]
            chosen = next(
                (m for m in mappings if location_code and m["location_code"] == location_code), None
            )
            chosen = chosen or next((m for m in mappings if not m["location_code"]), None)
            category["department_code"] = chosen["department_code"] if chosen else "REVIEW"
            department_name = next(
                (
                    d["name"]
                    for d in active.content["departments"]
                    if d["code"] == category["department_code"]
                ),
                None,
            )
        if payload.category_code is not None:
            ticket.category_code = category_code
            ticket.catalog_revision = catalog_revision
            ticket.catalog_snapshot = {
                "category_code": category_code,
                "label": category["label"],
                "domain": category["domain"],
                "department_code": category["department_code"],
                "location_text": location_text,
                "catalog_backed": db_catalog,
                "department_name": department_name,
            }
        if ticket.location_text != location_text:
            ticket.location_text = location_text
            ticket.location_history = ticket.location_history + [
                {"kind": "dispatcher_edit", "text": location_text}
            ]
            refresh_safety(ticket)
            if (request.app.state.settings.deployment_mode != "demo"
                    or _demo_db_locations(request.app.state.settings)):
                from app.auth_service import service_location

                location = service_location()
            else:
                location = resolve(location_text=location_text)
        else:
            location = dict(ticket.location_data) or LocationView().model_dump()
        save_location(ticket, location)
        transition(
            db,
            ticket,
            principal.user,
            "edited",
            json.dumps(
                {
                    "reason": payload.reason,
                    "before_category": before_category,
                    "after_category": ticket.category_code,
                    "before_location": before_location,
                    "after_location": ticket.location_data,
                    "routing": ticket.routing_data,
                    "version": ticket.location_version,
                },
                ensure_ascii=False,
            ),
        )
        await db.flush()
        result = await ticket_view(db, ticket, principal)
        save_idempotency(db, scope, payload_hash, result)
        return result


@router.post("/locations/catalog", response_model=LocationSearchResult)
async def location_catalog_search(payload: LocationSearchQuery, request: Request):
    from app.models import SchoolCatalogActive, SchoolCatalogVersion

    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        if principal.assignment or principal.user.role not in {"reporter", "dispatcher"}:
            raise HTTPException(403, "role_forbidden")
        settings = request.app.state.settings
        if not (settings.deployment_mode == "service" or _demo_db_locations(settings)):
            return LocationSearchResult(revision=None, locations=[])
        pointer = (await db.get(SchoolCatalogActive, 1)
                   if settings.planning_dataset_provider == "db" else None)
        catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
        return LocationSearchResult(
            revision=pointer.catalog_revision if catalog else None,
            locations=(search_locations(catalog.content.get("locations") or [], payload.query)
                       if catalog else []),
        )


@router.post("/locations/resolve", response_model=LocationView)
async def resolve_location(payload: LocationResolve, request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        if principal.assignment or principal.user.role not in {"reporter", "dispatcher"}:
            raise HTTPException(403, "role_forbidden")
        if (request.app.state.settings.deployment_mode == "service"
                or _demo_db_locations(request.app.state.settings)):
            from app.location_workflow import preview_service_location

            return await preview_service_location(db, payload, now())
        if (request.app.state.settings.deployment_mode != "demo"
                or _demo_db_locations(request.app.state.settings)):
            from app.auth_service import service_location

            return service_location()
        return resolve(payload.text, payload.location_text, payload.loc_hint, payload.gps)


@router.post("/tickets/{ticket_id}/location", response_model=TicketView)
async def change_location(ticket_id: uuid.UUID, payload: LocationUpdate, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        if principal.assignment or principal.user.role not in {"reporter", "dispatcher"}:
            raise HTTPException(403, "role_forbidden")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        scope, payload_hash, prior = await idempotency(
            db, request, f"location:{ticket.id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, ticket, principal)
        require_state(
            ticket,
            *(["received", "confirmed"] if principal.user.role == "dispatcher" else ["received"]),
        )
        if _demo_db_locations(request.app.state.settings) and principal.user.role == "reporter":
            from app.location_workflow import check_version

            check_version(ticket, payload.expected_version)
            location = ticket.location_data or {}
            if location.get("state") == "confirmed" or location.get("selected"):
                raise HTTPException(409, "location_locked")
        if (request.app.state.settings.deployment_mode != "demo"
                or _demo_db_locations(request.app.state.settings)):
            from app.auth_service import update_service_location

            update_service_location(db, ticket, principal, payload)
            if _demo_db_locations(request.app.state.settings) and payload.action == "answer":
                from app.location_workflow import preview_service_location
                from app.schemas import LocationResolve

                preview = await preview_service_location(
                    db, LocationResolve(location_text=payload.answer), now()
                )
                location = dict(ticket.location_data)
                location.update(
                    candidates=[candidate.model_dump() for candidate in preview.candidates],
                    source_type=preview.source_type,
                    dictionary_version=preview.dictionary_version,
                    catalog_revision=preview.catalog_revision,
                )
                ticket.location_data = location
        else:
            update_location(db, ticket, principal, payload)
        await db.flush()
        await db.refresh(ticket)
        result = await ticket_view(db, ticket, principal)
        save_idempotency(db, scope, payload_hash, result)
        return result


@router.post("/tickets/{ticket_id}/confirm", response_model=TicketView)
async def confirm_ticket(ticket_id: uuid.UUID, payload: Reason, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        require_state(ticket, "received")
        transition(db, ticket, principal.user, "confirmed", payload.reason, "confirmed")
        await db.flush()
        return await ticket_view(db, ticket, principal)


def make_assignment(db, ticket, worker, settings, *, token):
    assignment = Assignment(
        ticket_id=ticket.id,
        catalog_revision=ticket.catalog_revision,
        catalog_snapshot={**ticket.catalog_snapshot, "worker_name": worker.display_name},
        worker_id=worker.id,
        auth_epoch=worker.auth_epoch,
        token_hash=token_digest(settings, token),
        expires_at=now() + timedelta(hours=settings.worker_token_hours),
        revoked=False,
    )
    db.add(assignment)
    return assignment


async def atomic_eligibility_reason(db, settings, ticket, worker, catalog):
    from app.dispatch_schemas import ScheduleInput
    from app.services.location import registered

    selected = ticket.location_data.get("selected") or {}
    code = selected.get("code")
    if settings.planning_dataset_provider == "db":
        from app.models import SchoolCatalogActive

        active = await db.get(SchoolCatalogActive, 1)
        if not catalog or not active or not active.dataset_hash or not active.dataset_revision:
            return "school_catalog_unavailable"
        content = catalog.content
        category = next(
            (c for c in content["categories"] if c["code"] == ticket.category_code), None
        )
        location = next((loc for loc in content["locations"] if loc["code"] == code), None)
        if (
            ticket.location_data.get("state") != "confirmed"
            or ticket.location_data.get("source_type") != "service"
            or ticket.location_data.get("dataset_hash") != active.dataset_hash
            or ticket.location_data.get("dataset_revision") != active.dataset_revision
            or not location
            or not category
            or ticket.catalog_revision != catalog.revision
            or ticket.catalog_snapshot.get("category_code") != ticket.category_code
            or ticket.catalog_snapshot.get("domain") != category["field_code"]
            or not ticket.catalog_snapshot.get("catalog_backed")
        ):
            return "atomic_location_category_unconfirmed"
        from app.worker_candidates import mapped_department

        department = mapped_department(content, category["code"], code)
        if (
            department is None
            or ticket.catalog_snapshot.get("department_code") != department
        ):
            return "atomic_location_category_unconfirmed"
        profile = await db.get(WorkerProfile, worker.id)
        fields = {field["code"] for field in content["fields"]}
        if (
            not profile
            or profile.catalog_revision != catalog.revision
            or not profile.field_codes
            or not set(profile.field_codes) <= fields
            or category["field_code"] not in profile.field_codes
            or profile.department_code != department
        ):
            return "atomic_worker_field_unavailable"
        domain = category["field_code"]
    elif settings.deployment_mode == "demo":
        category = CATEGORY_MAP.get(ticket.category_code)
        if (
            ticket.location_data.get("state") != "confirmed"
            or not code
            or not registered(code)
            or not category
            or category["code"] == "UNKNOWN"
        ):
            return "atomic_location_category_unconfirmed"
        domain = category["domain"]
    else:
        return "school_catalog_unavailable"
    schedule = await db.get(WorkerSchedule, worker.id)
    try:
        parsed = ScheduleInput.model_validate(
            {**(schedule.data if schedule else {}), "expected_revision": 0}
        )
    except (ValidationError, TypeError):
        return "atomic_worker_field_unavailable"
    from app.dispatching import worker_time_reason

    if reason := worker_time_reason(schedule.data):
        return reason
    if not parsed.enabled or domain not in parsed.skills:
        return "atomic_worker_field_unavailable"
    if settings.planning_dataset_provider == "db":
        if (parsed.dataset_hash, parsed.dataset_revision) != (
            active.dataset_hash,
            active.dataset_revision,
        ):
            return "atomic_worker_schedule_stale"


async def require_atomic_eligibility(db, settings, ticket, worker, catalog):
    reason = await atomic_eligibility_reason(db, settings, ticket, worker, catalog)
    if reason:
        raise HTTPException(409, reason)


async def manual_field_override(db, settings, ticket, worker, override_reason):
    """Permit only explicit field/department mismatch on otherwise current records."""
    if settings.planning_dataset_provider != "db":
        return None
    from app.dispatch_schemas import ScheduleInput
    from app.school_catalog_routes import active_catalog

    pointer, catalog = await active_catalog(db)
    selected = (ticket.location_data or {}).get("selected") or {}
    if (not pointer or not catalog or not pointer.dataset_hash or not pointer.dataset_revision
            or ticket.category_code == "UNKNOWN"
            or not any(c["code"] == ticket.category_code for c in catalog.content["categories"])
            or (ticket.location_data or {}).get("source_type") != "service"
            or not selected.get("code")):
        return None
    reason = await atomic_eligibility_reason(db, settings, ticket, worker, catalog)
    if reason not in (None, "atomic_worker_field_unavailable"):
        raise HTTPException(409, reason)
    profile = await db.get(WorkerProfile, worker.id)
    fields = {field["code"] for field in catalog.content["fields"]}
    departments = {department["code"] for department in catalog.content["departments"]}
    if (not profile or profile.catalog_revision != catalog.revision or not profile.field_codes
            or not set(profile.field_codes) <= fields
            or profile.department_code not in departments):
        raise HTTPException(409, "atomic_worker_field_unavailable")
    schedule = await db.get(WorkerSchedule, worker.id)
    try:
        parsed = ScheduleInput.model_validate(
            {**(schedule.data if schedule else {}), "expected_revision": 0}
        )
    except (ValidationError, TypeError):
        raise HTTPException(409, "atomic_worker_field_unavailable") from None
    if not parsed.enabled or not set(parsed.skills) <= set(profile.field_codes):
        raise HTTPException(409, "atomic_worker_field_unavailable")
    if (parsed.dataset_hash, parsed.dataset_revision) != (
        pointer.dataset_hash, pointer.dataset_revision
    ):
        raise HTTPException(409, "atomic_worker_schedule_stale")
    from app.planning.registry import active_school_dataset

    try:
        provider, revision = await active_school_dataset(db, settings)
    except HTTPException:
        raise HTTPException(409, "atomic_worker_schedule_stale") from None
    nodes = {node.code for node in provider.dataset.locations}
    regions = {node.region for node in provider.dataset.locations}
    if (provider.sha256, revision) != (pointer.dataset_hash, pointer.dataset_revision):
        raise HTTPException(409, "atomic_worker_schedule_stale")
    if (parsed.start_node not in nodes or parsed.end_node not in nodes
            or parsed.region not in regions):
        raise HTTPException(409, "atomic_worker_field_unavailable")
    if reason is None:
        return None
    category = next(c for c in catalog.content["categories"] if c["code"] == ticket.category_code)
    mismatch = (category["field_code"] not in profile.field_codes
                or profile.department_code != ticket.catalog_snapshot.get("department_code"))
    if not mismatch:
        raise HTTPException(409, "atomic_worker_field_unavailable")
    if not override_reason or not override_reason.strip():
        raise HTTPException(422, "assignment_field_override_required")
    return catalog.revision


async def candidate_eligibility_reasons(
    db, settings, projected, worker, catalog, active, context, field_code, schedule,
):
    """Expose independently checkable reasons without changing assignment enforcement."""
    from app.dispatch_schemas import ScheduleInput
    from app.dispatching import worker_time_reason

    first = context or await atomic_eligibility_reason(db, settings, projected, worker, catalog)
    reasons = [first] if first else []
    if context or first in {"school_catalog_unavailable", "atomic_location_category_unconfirmed"}:
        return reasons

    def add(reason):
        if reason not in reasons:
            reasons.append(reason)

    if settings.planning_dataset_provider == "db" and catalog and active:
        profile = await db.get(WorkerProfile, worker.id)
        fields = {item["code"] for item in catalog.content["fields"]}
        if (not profile or profile.catalog_revision != catalog.revision or not profile.field_codes
                or not set(profile.field_codes) <= fields or field_code not in profile.field_codes
                or profile.department_code != projected.catalog_snapshot.get("department_code")):
            add("atomic_worker_field_unavailable")
    try:
        parsed = ScheduleInput.model_validate(
            {**(schedule.data if schedule else {}), "expected_revision": 0}
        )
    except (ValidationError, TypeError):
        add("atomic_worker_field_unavailable")
        return reasons
    if reason := worker_time_reason(schedule.data):
        add(reason)
    if not parsed.enabled or field_code not in parsed.skills:
        add("atomic_worker_field_unavailable")
    elif settings.planning_dataset_provider == "db" and active and (
        parsed.dataset_hash, parsed.dataset_revision
    ) != (active.dataset_hash, active.dataset_revision):
        add("atomic_worker_schedule_stale")
    return reasons


@router.get("/tickets/{ticket_id}/worker-candidates", response_model=AssignmentWorkerCandidates)
async def assignment_worker_candidates(
    ticket_id: uuid.UUID,
    request: Request,
    category_code: Annotated[str | None, Query(max_length=100)] = None,
    location_code: Annotated[str | None, Query(max_length=100)] = None,
):
    from app.auth_service import local_staff_identity
    from app.dispatch_routes import service_dataset
    from app.dispatching import NODES
    from app.execution import ongoing_visit
    from app.school_catalog_routes import active_catalog
    from app.staff_codes import eligible_workers, staff_codes
    from app.worker_candidates import order_candidates, project_ticket

    settings = request.app.state.settings
    if settings.deployment_mode == "prototype":
        raise HTTPException(404, "atomic_assignment_unavailable")
    async with request.app.state.database.transaction() as db:
        # Authentication can initialize a missing identity; this read-only route cannot.
        if not await db.get(DeploymentIdentity, 1):
            raise HTTPException(503, "deployment_identity_unavailable")
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        ticket = await get_ticket(db, ticket_id, principal)
        active, catalog, travel_provider = None, None, None
        nodes = {node["code"]: node["region"] for node in NODES}
        if settings.planning_dataset_provider == "db":
            active, catalog = await active_catalog(db)
            nodes = {}
            if active and catalog:
                try:
                    provider, revision = await service_dataset(request, db)
                    if (provider.sha256, revision) == (
                        active.dataset_hash, active.dataset_revision
                    ):
                        nodes = {node.code: node.region for node in provider.dataset.locations}
                        if provider.dataset.evidence.source == "estimated_straight_line":
                            travel_provider = provider
                except HTTPException as exc:
                    if exc.status_code != 503:
                        raise
        projected, context, field_code = project_ticket(
            ticket, settings, catalog, active, category_code, location_code, set(nodes)
        )
        workers = await eligible_workers(db, settings)
        codes = staff_codes(workers)
        counts_query = (
            select(Assignment.worker_id, func.count(Assignment.id))
            .where(Assignment.worker_id.in_([worker.id for worker in workers]),
                   Assignment.revoked.is_(False), Assignment.visit_ended_at.is_(None))
            .group_by(Assignment.worker_id)
        )
        if settings.deployment_mode == "public_demo":
            from app.tickets import public_visible_ticket

            counts_query = counts_query.join(Ticket).where(public_visible_ticket(now()))
        counts = dict((await db.execute(counts_query)).all())
        visit_rows = (await db.execute(
            select(Assignment, Ticket).join(Ticket, Ticket.id == Assignment.ticket_id).where(
                Assignment.worker_id.in_([worker.id for worker in workers]),
                Assignment.ticket_id != ticket.id, Assignment.revoked.is_(False),
            )
        )).all()
        workers_by_id = {worker.id: worker for worker in workers}
        busy_workers = {
            assignment.worker_id for assignment, other_ticket in visit_rows
            if ongoing_visit(assignment, other_ticket, workers_by_id[assignment.worker_id])
        }
        target = ((projected.location_data.get("selected") or {}).get("code")
                  if projected else None)
        buildings = ({loc["code"]: loc["building_code"] for loc in catalog.content["locations"]}
                     if catalog else {code: code.split(":")[0] for code in nodes})
        result = []
        for worker in workers:
            schedule = await db.get(WorkerSchedule, worker.id)
            reasons = await candidate_eligibility_reasons(
                db, settings, projected, worker, catalog, active, context, field_code, schedule,
            )
            start = (schedule.data or {}).get("start_node") if schedule else None
            same_building = bool(target and start and buildings.get(target)
                                 and buildings.get(target) == buildings.get(start))
            schedule_region = (schedule.data or {}).get("region") if schedule else None
            same_region = bool(target and nodes.get(target) and schedule_region
                               and nodes.get(target) == schedule_region)
            estimated = None
            if travel_provider is not None:
                travel_codes = [node.code for node in travel_provider.dataset.locations]
                if start in travel_codes and target in travel_codes:
                    estimated = travel_provider.dataset.travel_minutes[travel_codes.index(start)][
                        travel_codes.index(target)
                    ]
            result.append({"worker_id": worker.id, "display_name": worker.display_name,
                           "staff_code": codes[worker.id],
                           "is_local_test_account": local_staff_identity(worker, settings),
                           "eligible": not reasons,
                           "reasons": reasons,
                           "open_assignments": counts.get(worker.id, 0),
                           "has_ongoing_visit": worker.id in busy_workers,
                           "same_building": same_building, "same_region": same_region,
                           "estimated_travel_min": estimated,
                           "rank": None, "field_code": field_code})
        return {"context_reason": context, "candidates": order_candidates(result)}


async def apply_atomic_choices(db, request, ticket, principal, payload, catalog):
    """Apply choices inside the caller's locked transaction; never commit here."""
    from sqlalchemy import or_

    from app.models import TicketMerge
    from app.tickets import current_ai_draft_view

    settings = request.app.state.settings
    choice = payload.location_choice
    dataset = payload.dataset_location
    if not (payload.category_code is not None or choice or dataset):
        return
    if choice or dataset:
        merge = await db.scalar(
            select(TicketMerge.id)
            .where(
                TicketMerge.active.is_(True),
                or_(TicketMerge.source_id == ticket.id, TicketMerge.target_id == ticket.id),
            )
            .limit(1)
        )
        assignment = await db.scalar(
            select(Assignment.id)
            .where(
                Assignment.ticket_id == ticket.id,
                or_(Assignment.revoked.is_(False), Assignment.active_plan_id.is_not(None)),
                Assignment.visit_ended_at.is_(None),
            )
            .limit(1)
        )
        report = await db.scalar(
            select(CompletionReport.id)
            .where(
                CompletionReport.ticket_id == ticket.id,
                CompletionReport.reviewed.is_(False),
                CompletionReport.superseded_at.is_(None),
                CompletionReport.analysis_status != "dismissed",
            )
            .limit(1)
        )
        if merge or assignment or report:
            raise HTTPException(409, "location_relationship_conflict")
    if choice and choice.kind == "candidate" and dataset and choice.code != dataset.code:
        raise HTTPException(422, "location_code_mismatch")
    draft = await current_ai_draft_view(db, ticket, principal)
    before_category = ticket.category_code
    if payload.category_code is not None:
        code = payload.category_code
        if settings.planning_dataset_provider == "db":
            item = (
                next((x for x in catalog.content["categories"] if x["code"] == code), None)
                if catalog
                else None
            )
            if not item:
                raise HTTPException(422, "unknown_category")
            category = {"label": item["name"], "domain": item["field_code"]}
        else:
            category = CATEGORY_MAP.get(code)
            if not category or code == "UNKNOWN":
                raise HTTPException(422, "unknown_category")
        ticket.category_code = code
        ticket.catalog_revision = catalog.revision if catalog else None
        ticket.catalog_snapshot = {
            "category_code": code,
            "label": category["label"],
            "domain": category["domain"],
            "department_code": category.get("department_code", "REVIEW"),
            "location_text": ticket.location_text,
            "catalog_backed": settings.planning_dataset_provider == "db",
        }
        preserve_legacy_input(ticket)
        save_location(ticket, dict(ticket.location_data) or LocationView().model_dump())
        transition(
            db,
            ticket,
            principal.user,
            "edited",
            json.dumps(
                {
                    "reason": payload.reason,
                    "before_category": before_category,
                    "after_category": code,
                    "category_decision": "ai_suggestion_accepted"
                    if draft and not draft.stale and draft.category_code == code
                    else "dispatcher_changed",
                },
                ensure_ascii=False,
            ),
        )
    if choice:
        if choice.kind == "candidate" and settings.planning_dataset_provider == "db":
            from app.dispatch_routes import _catalog_candidates

            current = await _catalog_candidates(db, ticket)
            if not catalog or current.catalog_revision != catalog.revision:
                raise HTTPException(409, "catalog_revision_conflict")
            selected = next((x for x in current.candidates if x.code == choice.code), None)
            if selected is None:
                raise HTTPException(422, "invalid_location_candidate")
            preserve_legacy_input(ticket)
            save_location(
                ticket,
                LocationView(
                    state="confirmed",
                    source_type="catalog",
                    catalog_revision=catalog.revision,
                    dictionary_version=f"school-catalog-{catalog.revision}",
                    selected={**selected.model_dump(), "sources": ["dispatcher"]},
                ).model_dump(),
            )
            transition(db, ticket, principal.user, "location_updated", payload.reason)
        else:
            update = LocationUpdate(
                action="confirm" if choice.kind == "candidate" else "answer",
                expected_version=ticket.location_version,
                candidate_code=choice.code if choice.kind == "candidate" else None,
                answer=choice.text if choice.kind == "text" else None,
                reason=payload.reason,
            )
            if settings.deployment_mode == "demo" and not _demo_db_locations(settings):
                update_location(db, ticket, principal, update)
            else:
                from app.auth_service import update_service_location

                update_service_location(db, ticket, principal, update)
    if dataset:
        from app.dispatch_routes import service_dataset
        from app.dispatch_schemas import ServiceLocationConfirm
        from app.location_workflow import confirm_service_location

        provider, revision = await service_dataset(request, db)
        confirm_service_location(
            db,
            ticket,
            principal,
            ServiceLocationConfirm(
                expected_location_version=ticket.location_version,
                code=dataset.code,
                dataset_revision=dataset.dataset_revision,
                dataset_hash=dataset.sha256,
                reason=payload.reason,
            ),
            provider,
            revision,
            catalog if settings.planning_dataset_provider == "db" else None,
        )
    # Recompute the branch from the final location, including when only location changed.
    if settings.planning_dataset_provider == "db" and catalog:
        code = (ticket.location_data.get("selected") or {}).get("code")
        category = next(
            (x for x in catalog.content["categories"] if x["code"] == ticket.category_code), None
        )
        if category:
            mappings = [
                x for x in catalog.content["mappings"] if x["category_code"] == ticket.category_code
            ]
            mapping = next((x for x in mappings if code and x["location_code"] == code), None)
            mapping = mapping or next((x for x in mappings if not x["location_code"]), None)
            department = mapping["department_code"] if mapping else "REVIEW"
            ticket.catalog_snapshot = {
                **ticket.catalog_snapshot,
                "department_code": department,
                "department_name": next(
                    (x["name"] for x in catalog.content["departments"] if x["code"] == department),
                    None,
                ),
                "location_code": code,
                "location_label": (ticket.location_data.get("selected") or {}).get("label"),
                "location_text": ticket.location_text,
            }
    from app.services.routing import suggest

    ticket.routing_data = suggest(
        ticket.category_code, ticket.location_data, ticket.safety_data, ticket.catalog_snapshot
    )


@router.post("/tickets/{ticket_id}/confirm-assign", response_model=ConfirmAssignView)
async def confirm_and_assign(ticket_id: uuid.UUID, payload: ConfirmAssignInput, request: Request):
    from app.auth_service import worker_eligible
    from app.dispatching import manual_schedule_change
    from app.duplicates import merge_version

    settings = request.app.state.settings
    if settings.deployment_mode == "prototype":
        raise HTTPException(404, "atomic_assignment_unavailable")
    if not payload.reason.strip():
        raise HTTPException(422, "reason_required")
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        scope, payload_hash, prior = await idempotency(
            db,
            request,
            f"ca:{ticket_id.hex}:{principal.user.id.hex}",
            payload,
            view_type=ConfirmAssignView,
        )
        if prior:
            if settings.deployment_mode == "public_demo":
                await get_ticket(db, ticket_id, principal, lock=True)
            return prior
        worker = await lock_worker_user(db, settings, payload.worker_id)
        if not worker or not worker_eligible(worker, settings):
            raise HTTPException(422, "invalid_worker")
        if settings.deployment_mode == "public_demo":
            from app.public_staff import staff_session_active

            if not await staff_session_active(db, worker.id):
                raise HTTPException(422, "invalid_worker")
        await manual_schedule_change(db, worker.id, ticket_id)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        require_state(ticket, "received", "confirmed")
        already_confirmed = ticket.status == "confirmed"
        if already_confirmed and await db.scalar(
            select(Assignment.id).where(Assignment.ticket_id == ticket.id).limit(1)
        ):
            raise HTTPException(409, "assignment_snapshot_conflict")
        if await merge_version(db, ticket) != payload.expected_ticket_version:
            raise HTTPException(409, "ticket_revision_conflict")
        catalog_revision = None
        active = None
        if settings.planning_dataset_provider == "db":
            from app.school_catalog_routes import active_catalog

            pointer, active = await active_catalog(db)
            catalog_revision = pointer.catalog_revision if pointer else 0
        if payload.expected_catalog_revision != catalog_revision:
            raise HTTPException(409, "catalog_revision_conflict")
        await apply_atomic_choices(db, request, ticket, principal, payload, active)
        await require_atomic_eligibility(db, settings, ticket, worker, active)
        existing = await db.scalar(
            select(Assignment.id)
            .where(Assignment.ticket_id == ticket.id, Assignment.revoked.is_(False))
            .limit(1)
        )
        if existing:
            raise HTTPException(409, "assignment_snapshot_conflict")
        if not already_confirmed:
            transition(db, ticket, principal.user, "confirmed", payload.reason, "confirmed")
        token = secrets.token_urlsafe(32)
        assignment = make_assignment(db, ticket, worker, settings, token=token)
        transition(db, ticket, principal.user, "assigned", payload.reason, "assigned")
        await db.flush()
        result = ConfirmAssignView(ticket_id=ticket.id, assignment_id=assignment.id)
        save_idempotency(db, scope, payload_hash, result)
        return result


@router.post("/tickets/{ticket_id}/assign", response_model=AssignView)
async def assign_ticket(ticket_id: uuid.UUID, payload: AssignInput, request: Request):
    from app.auth_service import worker_eligible

    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        receipt_scope = receipt_hash = None
        if "idempotency-key" in request.headers:
            receipt_scope, receipt_hash, prior = await idempotency(
                db, request, f"assign:{ticket_id.hex}:{principal.user.id.hex}", payload,
                view_type=ConfirmAssignView,
            )
            if prior:
                from app.execution import phase

                ticket = await get_ticket(db, ticket_id, principal, lock=True)
                assignments = list(await db.scalars(
                    select(Assignment).where(
                        Assignment.ticket_id == ticket.id, Assignment.revoked.is_(False)
                    ).with_for_update()
                ))
                worker = (
                    await db.get(User, assignments[0].worker_id) if len(assignments) == 1 else None
                )
                if (
                    len(assignments) != 1
                    or assignments[0].id != prior.assignment_id
                    or not assignment_current(assignments[0], worker)
                    or not worker_eligible(worker, request.app.state.settings)
                    or phase(assignments[0], ticket) in {"stopped", "visit_ended"}
                ):
                    raise HTTPException(409, "assignment_snapshot_conflict")
                view = await ticket_view(db, ticket, principal)
                if view.assignment_id != prior.assignment_id:
                    raise HTTPException(409, "assignment_snapshot_conflict")
                return AssignView(
                    ticket=view,
                    worker_token=None,
                    assignment_id=prior.assignment_id,
                )
        worker = await lock_worker_user(db, request.app.state.settings, payload.worker_id)
        from app.dispatching import manual_schedule_change

        # The gate is already held by authenticate; old/new schedule revisions roll back together.
        await manual_schedule_change(db, payload.worker_id, ticket_id)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        require_state(ticket, "confirmed", "assigned", "in_progress")
        if (
            not worker
            or worker.role != "worker"
            or not worker_eligible(worker, request.app.state.settings)
        ):
            raise HTTPException(422, "invalid_worker")
        if request.app.state.settings.deployment_mode == "public_demo":
            from app.public_staff import staff_session_active

            if not await staff_session_active(db, worker.id):
                raise HTTPException(422, "invalid_worker")
        old = (
            await db.scalars(
                select(Assignment)
                .where(Assignment.ticket_id == ticket.id, Assignment.revoked.is_(False))
                .with_for_update()
            )
        ).all()
        from app.execution import execution_version, phase, stop_visit

        current = [
            assignment
            for assignment in old
            if assignment_current(assignment, await db.get(User, assignment.worker_id))
        ]
        explicit_invalid_stop = bool(
            not current
            and len(old) == 1
            and phase(old[0], ticket) in {"moving", "working"}
            and payload.stop_current_visit
            and payload.expected_assignment_id == old[0].id
            and payload.expected_execution_revision is not None
        )
        if len(current) > 1 or (
            not current
            and not explicit_invalid_stop
            and (
                payload.expected_assignment_id is not None
                or payload.expected_execution_revision is not None
            )
        ):
            raise HTTPException(409, "assignment_snapshot_conflict")
        if current:
            if (
                payload.expected_assignment_id != current[0].id
                or payload.expected_execution_revision is None
            ):
                raise HTTPException(409, "assignment_snapshot_conflict")
            execution_version(current[0], payload.expected_execution_revision)
        for assignment in old:
            correction_pending = await db.scalar(
                select(CompletionReport.id)
                .where(
                    CompletionReport.assignment_id == assignment.id,
                    CompletionReport.reviewed.is_(False),
                    CompletionReport.superseded_at.is_(None),
                    CompletionReport.analysis_status != "dismissed",
                    (CompletionReport.correction_requested_at.is_not(None))
                    | (CompletionReport.supersedes_report_id.is_not(None))
                    | (CompletionReport.assignment_id == assignment.id if assignment.visit_ended_at
                       else CompletionReport.supersedes_report_id.is_not(None)),
                )
                .limit(1)
            )
            if correction_pending:
                raise HTTPException(409, "report_pending_review")
            if phase(assignment, ticket) in {"moving", "working"}:
                if (
                    not payload.stop_current_visit
                    or payload.expected_assignment_id != assignment.id
                    or payload.expected_execution_revision is None
                ):
                    raise HTTPException(409, "execution_stop_confirmation_required")
                execution_version(assignment, payload.expected_execution_revision)
                stop_visit(db, assignment, ticket, principal.user, payload.reason)
            assignment.revoked = True
        override_revision = await manual_field_override(
            db, request.app.state.settings, ticket, worker, payload.override_reason
        )
        if override_revision is not None:
            db.add(TicketEvent(
                ticket_id=ticket.id, actor_id=principal.user.id,
                event_type="assignment_field_override",
                reason=json.dumps({"reason": payload.override_reason.strip(),
                                   "catalog_revision": override_revision,
                                   "worker_id": str(worker.id)}, ensure_ascii=False),
                from_status=ticket.status, to_status=ticket.status,
            ))
        token = secrets.token_urlsafe(32)
        assignment = make_assignment(db, ticket, worker, request.app.state.settings, token=token)
        transition(
            db,
            ticket,
            principal.user,
            "reassigned" if old else "assigned",
            payload.reason,
            "assigned",
        )
        await db.flush()
        if receipt_scope is not None:
            save_idempotency(
                db, receipt_scope, receipt_hash,
                ConfirmAssignView(ticket_id=ticket.id, assignment_id=assignment.id),
            )
        return AssignView(
            ticket=await ticket_view(db, ticket, principal),
            worker_token=(
                None
                if request.app.state.settings.deployment_mode in {"public_demo", "service"}
                else token
            ),
            assignment_id=assignment.id,
        )


def _work_order_key(state, group, sequence, approved_at, urgency, assigned_at, assignment_id):
    if state in {"visit_ended", "stopped"}:
        group, sequence = None, None
    return (
        0 if state in {"moving", "working"} else 2 if state in {"visit_ended", "stopped"} else 1,
        0 if group else 1,
        str(approved_at) if group else "",
        group or "",
        sequence if sequence is not None else 0,
        urgency,
        assigned_at,
        str(assignment_id),
    )


async def worker_own_settings_view(db, worker_id, schedule, *, demo_clock=False):
    profile = await db.get(WorkerProfile, worker_id)
    active = await db.get(SchoolCatalogActive, 1)
    data = schedule.data if schedule and schedule.data else {}
    hours = data.get("hours")
    start, end = data.get("shift_start_min"), data.get("shift_end_min")
    campus_source = "합성 시연 가정 · 실제 근무 일정이나 이동시간 아님"
    if (demo_clock and hours is None and data.get("assumptions_source") == campus_source
            and type(start) is int and type(end) is int and 0 <= start < end < 1440):
        # The campus fixture stores minutes from Seoul midnight. Other legacy
        # datasets can be relative to a different planning origin.
        hours = {"shift_start": f"{start // 60:02}:{start % 60:02}",
                 "shift_end": f"{end // 60:02}:{end % 60:02}"}
    return WorkerOwnSettings(
        field_codes=profile.field_codes if profile else [],
        department_code=profile.department_code if profile else None,
        schedule_configured=bool(data),
        schedule_enabled=data.get("enabled"),
        schedule_revision=schedule.revision if schedule else 0,
        hours=hours,
        shift_start_min=start,
        shift_end_min=end,
        days_off=data.get("days_off", []),
        profile_stale=bool(
            profile and active and profile.catalog_revision != active.catalog_revision
        ),
        schedule_stale=bool(data and active and (
            data.get("dataset_revision") != active.dataset_revision
            or data.get("dataset_hash") != active.dataset_hash
        )),
    )


@router.get("/worker/my-settings", response_model=WorkerOwnSettings)
async def worker_own_settings(request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "worker")
        worker_id = principal.user.id
        schedule = await db.get(WorkerSchedule, worker_id)
        return await worker_own_settings_view(
            db, worker_id, schedule, demo_clock=request.app.state.settings.deployment_mode == "demo"
        )


@router.put("/worker/my-settings", response_model=WorkerOwnSettings)
async def update_worker_own_hours(payload: WorkerOwnHoursInput, request: Request):
    from app.dispatching import require_no_worker_reservations

    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "worker")
        worker_id = principal.user.id
        schedule = await db.get(WorkerSchedule, worker_id, with_for_update=True)
        if not schedule or not schedule.data:
            raise HTTPException(409, "worker_schedule_unconfigured")
        if schedule.revision != payload.expected_revision:
            raise HTTPException(409, "schedule_revision_conflict")
        hours = payload.hours.model_dump(mode="json")
        if schedule.data.get("hours") != hours:
            await require_no_worker_reservations(db, worker_id)
            schedule.data = {**schedule.data, "hours": hours}
            schedule.revision += 1
        return await worker_own_settings_view(
            db, worker_id, schedule, demo_clock=request.app.state.settings.deployment_mode == "demo"
        )


@router.get("/worker/assignments", response_model=WorkerAssignmentList)
async def worker_assignments(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: Literal["urgent", "work_order"] | None = None,
):
    from app.auth_service import demo_worker_session_eligible

    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        if principal.user.role != "worker" or not (
            request.app.state.settings.deployment_mode in {"service", "public_demo"}
            or demo_worker_session_eligible(principal.user, request.app.state.settings)
        ):
            raise HTTPException(403, "worker_session_required")
        filters = (
            Assignment.worker_id == principal.user.id,
            Assignment.revoked.is_(False),
            Assignment.auth_epoch == principal.user.auth_epoch,
        )
        if request.app.state.settings.deployment_mode == "public_demo":
            from app.tickets import public_visible_ticket

            filters += (
                Assignment.ticket_id.in_(select(Ticket.id).where(public_visible_ticket(now()))),
            )
        total = await db.scalar(select(func.count()).select_from(Assignment).where(*filters))
        query = select(Assignment).where(*filters)
        if sort == "urgent":
            query = query.join(Ticket, Ticket.id == Assignment.ticket_id).order_by(
                func.least(Ticket.effective_urgency, Ticket.linked_safety_floor)
            )
        query = query.order_by(Assignment.created_at.desc(), Assignment.id.desc())
        assignments = list(
            await db.scalars(query if sort == "work_order" else query.offset(offset).limit(limit))
        )
        from app.duplicates import aggregate_safety
        from app.execution import execution_view, phase, service_plan_execution_valid
        from app.models import (
            DispatchPlan,
            PlanMembership,
            SchoolCatalogActive,
            SchoolCatalogVersion,
        )

        ids = [a.id for a in assignments]
        tickets = {
            t.id: t
            for t in await db.scalars(
                select(Ticket).where(Ticket.id.in_([a.ticket_id for a in assignments]))
            )
        }
        plan_ids = {a.plan_id for a in assignments if a.plan_id and a.plan_id == a.active_plan_id}
        plans = {
            p.id: p
            for p in await db.scalars(select(DispatchPlan).where(DispatchPlan.id.in_(plan_ids)))
        }
        from app.planning import PlanningInput, PlanResult, input_hash, validate_plan

        checked_plans = {}
        for plan in plans.values():
            try:
                problem = PlanningInput.model_validate_json(json.dumps(plan.input_data))
                result = PlanResult.model_validate_json(json.dumps(plan.result_data))
                validate_plan(problem, result)
                dataset = plan.snapshot_data.get("dataset") or {}
                if (
                    input_hash(problem) != plan.input_hash
                    or result.input_hash != plan.input_hash
                    or dataset.get("sha256") != problem.dataset_hash
                    or dataset.get("revision") != problem.dataset_revision
                ):
                    continue
                checked_plans[plan.id] = (problem, result)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
        members = {}
        for member in await db.scalars(
            select(PlanMembership).where(PlanMembership.assignment_id.in_(ids))
        ):
            members.setdefault(member.assignment_id, []).append(member)
        pointer = await db.get(SchoolCatalogActive, 1)
        catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
        locations = {
            loc["code"]: loc for loc in (catalog.content.get("locations", []) if catalog else [])
        }
        positions = {}
        for item in assignments:
            if item.plan_id and item.plan_sequence is not None:
                positions.setdefault((item.plan_id, item.plan_sequence), []).append(item)
        rows = []
        for assignment in assignments:
            ticket = tickets[assignment.ticket_id]
            location = ticket.location_data or {}
            selected = location.get("selected") or {}
            current_location = bool(
                pointer
                and (
                    (
                        location.get("source_type") == "service"
                        and location.get("dataset_hash") == pointer.dataset_hash
                        and location.get("dataset_revision") == pointer.dataset_revision
                        and assignment.catalog_revision == pointer.catalog_revision
                    )
                    or (
                        location.get("source_type") == "catalog"
                        and location.get("catalog_revision") == pointer.catalog_revision
                    )
                )
            )
            point = (
                locations.get(selected.get("code"))
                if location.get("state") == "confirmed" and current_location
                else None
            )
            if point:
                lat, lon = point.get("latitude"), point.get("longitude")
                if not (
                    point.get("coordinate_system") == "WGS84"
                    and type(lat) in (int, float) and type(lon) in (int, float)
                    and math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180
                ):
                    point = None
            group = None
            sequence = None
            if (
                assignment.plan_id
                and assignment.plan_id == assignment.active_plan_id
                and assignment.plan_sequence is not None
            ):
                plan = plans.get(assignment.plan_id)
                memberships = members.get(assignment.id, [])
                valid = bool(
                    plan
                    and plan.kind == "initial"
                    and plan.status == "approved"
                    and plan.approved_by
                    and plan.approved_at
                    and len(memberships) == 1
                    and memberships[0].plan_id == plan.id
                    and memberships[0].sequence == assignment.plan_sequence
                    and memberships[0].kind == "new"
                    and memberships[0].original_assignment_id is None
                )
                if valid:
                    checked = checked_plans.get(plan.id)
                    valid = bool(checked)
                    if checked:
                        problem, result = checked
                        routes = [
                            r for r in result.routes if r.worker_id == str(assignment.worker_id)
                        ]
                        specs = [spec for spec in problem.tickets if spec.id == str(ticket.id)]
                        n = assignment.plan_sequence
                        valid = bool(
                            pointer
                            and pointer.dataset_hash == problem.dataset_hash
                            and pointer.dataset_revision == problem.dataset_revision
                            and current_location
                            and len(routes) == 1
                            and len(specs) == 1
                            and isinstance(n, int)
                            and 0 <= n < len(routes[0].visits)
                            and routes[0].visits[n].ticket_id == str(ticket.id)
                            and routes[0].visits[n].arrival_min == assignment.planned_arrival_min
                            and routes[0].visits[n].completion_min
                            == assignment.planned_completion_min
                            and location.get("state") == "confirmed"
                            and specs[0].node in range(len(problem.travel.nodes))
                            and selected.get("code") == problem.travel.nodes[specs[0].node]
                            and (
                                assignment.execution_location_code is None
                                or assignment.execution_location_code == selected.get("code")
                            )
                        )
                if valid:
                    same_position = positions.get((plan.id, assignment.plan_sequence), [])
                    valid = len(same_position) == 1 and same_position[0].id == assignment.id
                    if assignment.plan_sequence == 0:
                        valid = valid and assignment.predecessor_id is None
                    else:
                        previous = positions.get((plan.id, assignment.plan_sequence - 1), [])
                        valid = (
                            valid
                            and len(previous) == 1
                            and previous[0].id == assignment.predecessor_id
                            and previous[0].active_plan_id == plan.id
                            and len(members.get(previous[0].id, [])) == 1
                            and members[previous[0].id][0].plan_id == plan.id
                            and members[previous[0].id][0].sequence == assignment.plan_sequence - 1
                            and members[previous[0].id][0].kind == "new"
                            and members[previous[0].id][0].original_assignment_id is None
                            and str(previous[0].ticket_id)
                            == routes[0].visits[assignment.plan_sequence - 1].ticket_id
                            and previous[0].planned_arrival_min
                            == routes[0].visits[assignment.plan_sequence - 1].arrival_min
                            and previous[0].planned_completion_min
                            == routes[0].visits[assignment.plan_sequence - 1].completion_min
                        )
                if valid and request.app.state.settings.deployment_mode == "service":
                    valid = await service_plan_execution_valid(db, assignment, ticket)
                if valid:
                    group, sequence = str(plan.id), assignment.plan_sequence
            if (
                assignment.plan_id is not None
                or assignment.active_plan_id is not None
                or assignment.plan_sequence is not None
            ) and not group:
                point = None
            state = phase(assignment, ticket)
            ordering = _work_order_key(
                state, group, sequence, plan.approved_at if group else None,
                min(ticket.effective_urgency, ticket.linked_safety_floor),
                assignment.created_at, assignment.id,
            )
            rows.append((ordering, assignment, ticket, point, group, sequence))
        if sort == "work_order":
            rows.sort(key=lambda row: row[0])
            rows = rows[offset : offset + limit]
        items = []
        for _, assignment, ticket, point, group, sequence in rows:
            items.append(
                WorkerAssignmentSummary(
                    assignment_id=assignment.id,
                    assigned_at=assignment.created_at,
                    ticket_id=ticket.id,
                    public_no=ticket.public_no or f"B-{str(ticket.id)[:8]}",
                    status=ticket.status,
                    location_text=ticket.location_text,
                    confirmed_location_label=(
                        (ticket.location_data.get("selected") or {}).get("label")
                        if (ticket.location_data or {}).get("state") == "confirmed"
                        else None
                    ),
                    safety={
                        **ticket.safety_data,
                        **aggregate_safety(ticket),
                        "override_applied": bool(ticket.override_reason),
                    },
                    execution=await execution_view(db, assignment, ticket),
                    map_latitude=point.get("latitude") if point else None,
                    map_longitude=point.get("longitude") if point else None,
                    plan_group=group,
                    plan_sequence=sequence,
                )
            )
        return WorkerAssignmentList(items=items, total=total)


@router.get("/worker/assignments/{assignment_id}", response_model=WorkerAssignmentView)
async def worker_assignment_detail(assignment_id: uuid.UUID, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db, worker_allowed=True)
        if not principal.assignment or principal.assignment.id != assignment_id:
            raise HTTPException(403, "worker_session_required")
        ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        assignment = await locked_assignment(db, principal, assignment_id)
        from app.execution import execution_view

        return WorkerAssignmentView(
            execution=await execution_view(db, assignment, ticket),
            assignment_id=assignment.id,
            ticket=await ticket_view(db, ticket, principal, include_map=True),
        )


@router.get("/worker/assignment", response_model=WorkerAssignmentView)
async def worker_assignment(request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db, worker_allowed=True)
        if not principal.assignment:
            raise HTTPException(403, "worker_token_required")
        ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        await locked_assignment(db, principal, principal.assignment.id)
        from app.execution import execution_view

        return WorkerAssignmentView(
            execution=await execution_view(db, principal.assignment, ticket),
            assignment_id=principal.assignment.id,
            ticket=await ticket_view(db, ticket, principal, include_map=True),
        )


@router.post("/worker/assignments/{assignment_id}/start", response_model=TicketView)
async def start_work(assignment_id: uuid.UUID, payload: ExecutionInput, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db, worker_allowed=True)
        if not principal.assignment or principal.assignment.id != assignment_id:
            raise HTTPException(403, "assignment_forbidden")
        ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        await locked_assignment(db, principal, assignment_id)
        require_state(ticket, "assigned")
        from app.execution import require_action

        a = principal.assignment
        await require_action(db, a, ticket, payload.expected_execution_revision, "start")
        a.started_at = now()
        a.execution_revision += 1
        selected = ticket.location_data.get("selected") or {}
        a.execution_location_code = selected.get("code")
        transition(
            db, ticket, principal.user, "started", "작업자가 착수를 확인했어요", "in_progress"
        )
        await db.flush()
        return await ticket_view(db, ticket, principal)


@router.post("/worker/assignments/{assignment_id}/report", response_model=TicketView)
async def report_work(assignment_id: uuid.UUID, payload: ReportInput, request: Request):
    if request.app.state.settings.deployment_mode == "public_demo" and payload.upload_ids:
        raise HTTPException(403, "public_upload_unavailable")
    if payload.upload_ids and await _use_managed_photos(request):
        from app.managed_access import managed_report_work

        return await managed_report_work(request, assignment_id, payload)
    async with _photo_aware_transaction(request) as db:
        return await _report_work_tx(db, assignment_id, payload, request)


async def _report_work_tx(db, assignment_id, payload, request, *, managed=None):
    principal = await authenticate(request, db, worker_allowed=True)
    if not principal.assignment or principal.assignment.id != assignment_id:
        raise HTTPException(403, "assignment_forbidden")
    scope, payload_hash, prior = await idempotency(
        db, request, f"report:{principal.actor_scope}", payload
    )
    ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
    await locked_assignment(db, principal, assignment_id)
    if prior:
        return await ticket_view(db, ticket, principal)
    require_state(ticket, "in_progress")
    from app.execution import end_visit, execution_version, require_action

    # A correction changes the submitted document, not the already ended visit.
    if principal.assignment.visit_ended_at and payload.supersedes_report_id:
        execution_version(principal.assignment, payload.expected_execution_revision)
    else:
        await require_action(
            db, principal.assignment, ticket, payload.expected_execution_revision, "report"
        )
    unreviewed = await db.scalar(
        select(CompletionReport).where(
            CompletionReport.assignment_id == assignment_id,
            CompletionReport.reviewed.is_(False),
            CompletionReport.superseded_at.is_(None),
        )
    )
    if unreviewed:
        if (
            not payload.supersedes_report_id
            or unreviewed.id != payload.supersedes_report_id
            or not unreviewed.correction_requested_at
            or unreviewed.worker_confirmed_revision != unreviewed.revision
        ):
            raise HTTPException(409, "report_pending_review")
        unreviewed.superseded_at = now()
    elif payload.supersedes_report_id:
        raise HTTPException(409, "report_not_correctable")
    if (
        principal.assignment.visit_ended_at and not payload.supersedes_report_id
    ) or principal.assignment.stopped_at:
        raise HTTPException(409, "visit_already_ended")
    report = CompletionReport(
        supersedes_report_id=payload.supersedes_report_id,
        ticket_id=ticket.id,
        assignment_id=assignment_id,
        result=payload.result,
        text=payload.text,
        reviewed=False,
    )
    db.add(report)
    await db.flush()
    if managed:
        from app.managed_access import _bind

        await _bind(db, *managed, payload.upload_ids, principal, ticket, report.id)
        if payload.upload_ids:
            report.revision += 1
    else:
        if payload.upload_ids:
            from app.protection import require_legacy_storage

            await require_legacy_storage(
                db, LocalPrivateStorage(request.app.state.settings.storage_path)
            )
        await bind_uploads(db, payload.upload_ids, principal, ticket, report.id)
    db.add(
        ProcessingJob(
            ticket_id=ticket.id,
            report_id=report.id,
            kind="report_analysis",
            input_revision=report.revision,
        )
    )
    transition(db, ticket, principal.user, "reported", "작업 보고 초안 저장 · 작업자 확인 필요")
    end_visit(
        db, principal.assignment, ticket, principal.user, "작업 보고 저장으로 방문 종료"
    )
    await db.flush()
    result = await ticket_view(db, ticket, principal)
    save_idempotency(db, scope, payload_hash, result)
    return result


@router.post(
    "/worker/assignments/{assignment_id}/reports/{report_id}/confirm", response_model=TicketView
)
async def confirm_report(
    assignment_id: uuid.UUID, report_id: uuid.UUID, payload: ReportConfirmInput, request: Request
):
    from app.reporting import confirm_structure

    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db, worker_allowed=True)
        if not principal.assignment or principal.assignment.id != assignment_id:
            raise HTTPException(403, "assignment_forbidden")
        ticket = await get_ticket(db, principal.assignment.ticket_id, principal, lock=True)
        await locked_assignment(db, principal, assignment_id)
        report = await db.scalar(
            select(CompletionReport).where(CompletionReport.id == report_id).with_for_update()
        )
        if not report or report.assignment_id != assignment_id or report.ticket_id != ticket.id:
            raise HTTPException(404, "report_not_found")
        scope, payload_hash, prior = await idempotency(
            db, request, f"reportconfirm:{report.id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, ticket, principal)
        require_state(ticket, "in_progress")
        if (
            report.reviewed
            or report.superseded_at is not None
            or report.correction_requested_at is not None
            or (
                report.worker_confirmed_revision is not None
                and not (report.legacy_migrated and report.confirmed_data.get("source") == "legacy")
            )
            or report.revision != payload.expected_revision
        ):
            raise HTTPException(409, "report_revision_conflict")
        structure = confirm_structure(report, payload)
        report.revision += 1
        report.confirmed_data = structure.model_dump()
        report.confirmed_result = payload.confirmed_result
        report.worker_confirmed_revision = report.revision
        report.analysis_status = "worker_confirmed"
        from app.execution import end_visit

        end_visit(
            db, principal.assignment, ticket, principal.user, "작업자 보고 제출로 방문 종료"
        )
        transition(db, ticket, principal.user, "report_confirmed", payload.reason)
        await db.flush()
        result = await ticket_view(db, ticket, principal)
        save_idempotency(db, scope, payload_hash, result)
        return result


@router.post(
    "/tickets/{ticket_id}/reports/{report_id}/request-correction", response_model=TicketView
)
async def request_report_correction(
    ticket_id: uuid.UUID, report_id: uuid.UUID, payload: CorrectionInput, request: Request
):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await lock_report_worker(db, request.app.state.settings, report_id)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        assignment = await db.scalar(
            select(Assignment)
            .join(User, User.id == Assignment.worker_id)
            .where(Assignment.ticket_id == ticket.id, current_assignment_clause())
            .with_for_update(of=Assignment)
        )
        report = await db.scalar(
            select(CompletionReport).where(CompletionReport.id == report_id).with_for_update()
        )
        if (
            not assignment
            or not report
            or report.ticket_id != ticket.id
            or report.assignment_id != assignment.id
        ):
            raise HTTPException(409, "report_not_correctable")
        scope, payload_hash, prior = await idempotency(
            db, request, f"correction:{report.id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, ticket, principal)
        require_state(ticket, "in_progress")
        current_report_id = await db.scalar(
            select(CompletionReport.id)
            .where(
                CompletionReport.assignment_id == assignment.id,
                CompletionReport.superseded_at.is_(None),
                CompletionReport.analysis_status != "dismissed",
            )
            .order_by(CompletionReport.created_at.desc(), CompletionReport.id.desc())
            .limit(1)
        )
        if (
            report.id != current_report_id
            or assignment.stopped_at
            or report.reviewed
            or report.superseded_at
            or report.correction_requested_at
            or report.worker_confirmed_revision != report.revision
            or report.revision != payload.expected_report_revision
        ):
            raise HTTPException(409, "report_not_correctable")
        report.correction_requested_at = now()
        report.correction_reason = payload.reason
        report.correction_requested_by = principal.user.id
        transition(db, ticket, principal.user, "report_correction_requested", payload.reason)
        await db.flush()
        result = await ticket_view(db, ticket, principal)
        save_idempotency(db, scope, payload_hash, result)
        return result


@router.post("/tickets/{ticket_id}/review-report", response_model=TicketView)
async def review_report(ticket_id: uuid.UUID, payload: ReviewInput, request: Request):
    from app.reporting import REPLIES, conflicts

    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await lock_report_worker(db, request.app.state.settings, payload.report_id)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        assignment = await db.scalar(
            select(Assignment)
            .join(User, User.id == Assignment.worker_id)
            .where(Assignment.ticket_id == ticket.id, current_assignment_clause())
            .with_for_update(of=Assignment)
        )
        report = await db.scalar(
            select(CompletionReport)
            .where(CompletionReport.id == payload.report_id)
            .with_for_update()
        )
        if (
            not assignment
            or not report
            or report.ticket_id != ticket.id
            or report.assignment_id != assignment.id
        ):
            raise HTTPException(409, "report_not_reviewable")
        scope, payload_hash, prior = await idempotency(
            db, request, f"review:{report.id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, ticket, principal)
        require_state(ticket, "in_progress")
        current_report_id = await db.scalar(
            select(CompletionReport.id)
            .where(
                CompletionReport.assignment_id == assignment.id,
                CompletionReport.superseded_at.is_(None),
                CompletionReport.analysis_status != "dismissed",
            )
            .order_by(CompletionReport.created_at.desc(), CompletionReport.id.desc())
            .limit(1)
        )
        if (
            report.id != current_report_id
            or report.reviewed
            or report.superseded_at is not None
            or report.correction_requested_at is not None
            or report.revision != payload.expected_report_revision
            or report.worker_confirmed_revision != report.revision
        ):
            raise HTTPException(409, "report_not_reviewable")
        if (
            conflicts(
                report.result, report.text, ReportStructure.model_validate(report.confirmed_data)
            )
            and report.confirmed_result != "needs_review"
        ):
            raise HTTPException(422, "report_completion_conflict")
        report.reviewed = True
        outcome = payload.review_outcome or report.confirmed_result
        report.approved_snapshot = {
            "revision": report.revision,
            "confirmed_result": outcome,
            "worker_confirmed_result": report.confirmed_result,
            "review_outcome": payload.review_outcome,
            "structure": report.confirmed_data,
            "legacy_migrated": report.legacy_migrated,
        }
        from app.auth_service import LOCAL_STAFF_IDS

        if principal.user.id in LOCAL_STAFF_IDS or assignment.worker_id in LOCAL_STAFF_IDS:
            report.approved_snapshot["external_notification_suppressed"] = True
        report.reply_text = REPLIES[outcome]
        report.approved_by, report.approved_at = principal.user.id, now()
        ticket.reply_text = report.reply_text
        target = "completed" if outcome == "completed" else "in_progress"
        if outcome == "completed":
            from app.execution import end_visit

            end_visit(
                db, assignment, ticket, principal.user, "완료 보고 담당자 승인으로 이번 방문 종료"
            )
        from app.mail_outbox import reply_intents

        await reply_intents(db, request.app.state.settings, ticket, report)
        transition(db, ticket, principal.user, "report_reviewed", payload.reason, target)
        await db.flush()
        result = await ticket_view(db, ticket, principal)
        save_idempotency(db, scope, payload_hash, result)
        return result


@router.post("/tickets/{ticket_id}/reports/{report_id}/dismiss", response_model=TicketView)
async def dismiss_orphan_report(
    ticket_id: uuid.UUID, report_id: uuid.UUID, payload: ReportDismissInput, request: Request
):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await lock_report_worker(db, request.app.state.settings, report_id)
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        assignment_id = await db.scalar(
            select(CompletionReport.assignment_id).where(
                CompletionReport.id == report_id, CompletionReport.ticket_id == ticket.id
            )
        )
        if not assignment_id:
            raise HTTPException(404, "report_not_found")
        assignment = await db.scalar(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        report = await db.scalar(
            select(CompletionReport)
            .where(CompletionReport.id == report_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        scope, payload_hash, prior = await idempotency(
            db, request, f"reportdismiss:{report.id}:{principal.actor_scope}", payload
        )
        if prior:
            return await ticket_view(db, ticket, principal)
        if (
            assignment_current(assignment, await db.get(User, assignment.worker_id))
            or report.reviewed
            or report.approved_snapshot
            or report.superseded_at is not None
            or report.analysis_status == "dismissed"
        ):
            raise HTTPException(409, "report_not_dismissible")
        if report.revision != payload.expected_report_revision:
            raise HTTPException(409, "report_revision_conflict")
        report.revision += 1
        report.analysis_status = "dismissed"
        transition(
            db,
            ticket,
            principal.user,
            "report_dismissed",
            json.dumps(
                {
                    "report_id": str(report.id),
                    "report_revision": report.revision,
                    "reason": payload.reason,
                },
                ensure_ascii=False,
            ),
        )
        await db.flush()
        result = await ticket_view(db, ticket, principal)
        save_idempotency(db, scope, payload_hash, result)
        return result


def safety_fields(result):
    return {
        "source_text_hash": result.original_hash,
        "rules_version": result.rule_version,
        "rule_hits": result.hits,
        "rule_floor": result.rule_floor,
        "ai_urgency": result.ai_urgency,
        "automatic_urgency": result.automatic_urgency,
        "assessment_status": "rules_only",
        "requires_human_review": True,
        "emergency_banner": result.rule_floor <= 1,
    }


async def image_response(db, upload, request, *, source=False, variant_id=None):
    import hashlib

    from fastapi.responses import Response as BytesResponse
    from starlette.concurrency import run_in_threadpool

    storage = LocalPrivateStorage(request.app.state.settings.storage_path)
    from app.protection import require_legacy_storage

    await require_legacy_storage(db, storage)
    if source:
        key = upload.object_key
        expected_hash = None
    else:
        if variant_id and variant_id != upload.current_variant_id:
            raise HTTPException(409, "stale_image_variant")
        variant = (
            await db.get(ImageVariant, upload.current_variant_id)
            if upload.current_variant_id
            else None
        )
        if not variant or variant.status != "ready" or not variant.object_key:
            raise HTTPException(409, "image_not_ready")
        if variant.delete_after <= now():
            raise HTTPException(410, "image_deleted")
        key, expected_hash = variant.object_key, variant.sha256
    try:
        data = await run_in_threadpool(storage.read, key)
    except FileNotFoundError:
        raise HTTPException(410, "image_deleted") from None
    except OSError:
        raise HTTPException(503, "photo_storage_unavailable") from None
    if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
        raise HTTPException(409, "image_hash_mismatch")
    # Service User FOR SHARE remains held while private bytes are materialized.
    # Authorization is fixed at transaction exit; already-authorized in-flight bytes
    # cannot be retracted by a later role change after that transaction commits.
    return BytesResponse(
        data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/uploads/{upload_id}/source")
async def image_source(upload_id: uuid.UUID, request: Request):
    if await _use_managed_photos(request):
        from app.managed_access import managed_image

        return await managed_image(request, upload_id, source=True)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        return await image_response(db, upload, request, source=True)


@router.get("/uploads/{upload_id}/metadata", response_model=PhotoView)
async def image_metadata(upload_id: uuid.UUID, request: Request):
    """Refresh the owner's protection state without processing or approving an image."""
    if await _use_managed_photos(request):
        from app.managed_access import managed_metadata

        return await managed_metadata(request, upload_id)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        from app.protection import require_legacy_storage

        await require_legacy_storage(
            db, LocalPrivateStorage(request.app.state.settings.storage_path)
        )
        result = await photo_view(db, upload)
    return result


@router.get("/uploads/{upload_id}/variants/{variant_id}")
async def image_variant(upload_id: uuid.UUID, variant_id: uuid.UUID, request: Request):
    if await _use_managed_photos(request):
        from app.managed_access import managed_image

        return await managed_image(request, upload_id, variant_id=variant_id)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal)
        return await image_response(db, upload, request, variant_id=variant_id)


@router.post("/uploads/{upload_id}/process", response_model=PhotoView)
async def reprocess_image(upload_id: uuid.UUID, payload: ProcessInput, request: Request):
    if await _use_managed_photos(request):
        from app.managed_photo import managed_reprocess

        return await managed_reprocess(
            request,
            upload_id,
            [mask.model_dump() for mask in payload.masks],
            detect_faces=payload.detect_faces,
        )
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        await process_upload(
            db,
            upload,
            LocalPrivateStorage(request.app.state.settings.storage_path),
            [mask.model_dump() for mask in payload.masks],
            detect_faces=payload.detect_faces,
        )
        return await photo_view(db, upload)


async def _reanalyze_after_photo_approval(db, request, upload, principal, seen_before):
    """Wake an initial photo wait, or queue at most one newly approved input."""
    settings = request.app.state.settings
    if (
        settings.llm_fake
        or upload.ticket_id is None
        or upload.information_request_id is not None
        or upload.report_id is not None
        or upload.assignment_id is not None
    ):
        return
    ticket = await db.get(Ticket, upload.ticket_id)
    if (
        not ticket
        or ticket.status in {"completed", "transferred", "merged", "rejected", "cancelled"}
        or ticket.reporter_id != principal.user.id
    ):
        return
    if (
        getattr(settings, "booth_free_input_live", False)
        and (ticket.safety_data or {}).get("booth_live_intake") is not True
    ):
        return
    jobs = (
        await db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.ticket_id == ticket.id, ProcessingJob.kind == "ticket_analysis")
            .order_by(ProcessingJob.created_at, ProcessingJob.id)
        )
    ).all()
    if not jobs:
        return
    initial = jobs[0]
    if initial.state == "queued" and initial.attempt_count == 0:
        from app.jobs import release_photo_wait

        await release_photo_wait(db, ticket, initial)
        return
    if seen_before or len(jobs) >= 2:
        return
    ticket.analysis_revision += 1
    ticket.ai_status = "queued"
    ticket.updated_at = now()
    db.add(ProcessingJob(ticket_id=ticket.id, input_revision=ticket.analysis_revision))


@router.post("/uploads/{upload_id}/approval", response_model=PhotoView)
async def approve_image(upload_id: uuid.UUID, payload: ApprovalInput, request: Request):
    if await _use_managed_photos(request):
        from app.managed_access import managed_approve

        return await managed_approve(request, upload_id, payload)
    import hashlib

    from starlette.concurrency import run_in_threadpool

    from app.protection import expected_purpose, require_legacy_storage, revoke_approvals

    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        await require_legacy_storage(
            db, LocalPrivateStorage(request.app.state.settings.storage_path)
        )
        variant = (
            await db.get(ImageVariant, upload.current_variant_id)
            if upload.current_variant_id
            else None
        )
        if (
            not variant
            or variant.status != "ready"
            or variant.id != payload.variant_id
            or variant.sha256 != payload.sha256
            or variant.pipeline_version != payload.pipeline_version
            or payload.provider != request.app.state.settings.llm_primary
            or payload.purpose != expected_purpose(upload)
            or variant.delete_after <= now()
        ):
            raise HTTPException(409, "image_approval_mismatch")
        storage = LocalPrivateStorage(request.app.state.settings.storage_path)
        try:
            data = await run_in_threadpool(storage.read, variant.object_key)
        except OSError:
            raise HTTPException(409, "image_not_ready") from None
        if hashlib.sha256(data).hexdigest() != variant.sha256:
            raise HTTPException(409, "image_hash_mismatch")
        seen_before = bool(
            await db.scalar(
                select(ImageApproval.id)
                .where(
                    ImageApproval.upload_id == upload.id,
                    ImageApproval.variant_id == variant.id,
                    ImageApproval.sha256 == variant.sha256,
                    ImageApproval.actor_id == principal.user.id,
                    ImageApproval.provider == payload.provider,
                    ImageApproval.purpose == payload.purpose,
                )
                .limit(1)
            )
        )
        await revoke_approvals(db, upload)
        db.add(
            ImageApproval(
                upload_id=upload.id,
                variant_id=variant.id,
                sha256=variant.sha256,
                pipeline_version=variant.pipeline_version,
                actor_id=principal.user.id,
                provider=payload.provider,
                purpose=payload.purpose,
            )
        )
        await db.flush()
        await _reanalyze_after_photo_approval(db, request, upload, principal, seen_before)
        await db.flush()
        return await photo_view(db, upload)


@router.delete("/uploads/{upload_id}/approval", response_model=PhotoView)
async def revoke_image(upload_id: uuid.UUID, request: Request):
    if await _use_managed_photos(request):
        from app.managed_access import managed_revoke

        return await managed_revoke(request, upload_id)
    from app.protection import require_legacy_storage, revoke_approvals

    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        upload = await get_upload(db, upload_id, principal, owner_only=True)
        await require_legacy_storage(
            db, LocalPrivateStorage(request.app.state.settings.storage_path)
        )
        await revoke_approvals(db, upload)
        await db.flush()
        return await photo_view(db, upload)


@router.post("/tickets/{ticket_id}/urgency", response_model=TicketView)
async def override_urgency(ticket_id: uuid.UUID, payload: UrgencyInput, request: Request):
    async with _photo_aware_transaction(request) as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        if ticket.status == "merged":
            raise HTTPException(409, "merged_source_locked")
        if ticket.safety_version != payload.expected_assessment_version or not ticket.safety_data:
            raise HTTPException(409, "assessment_version_conflict")
        previous = ticket.effective_urgency
        ticket.effective_urgency = payload.effective_urgency
        ticket.override_reason = payload.reason
        ticket.safety_version += 1
        transition(
            db,
            ticket,
            principal.user,
            "urgency_overridden",
            json.dumps(
                {
                    "reason": payload.reason,
                    "previous": previous,
                    "effective": ticket.effective_urgency,
                    "automatic": ticket.safety_data["automatic_urgency"],
                    "assessment_version": ticket.safety_version,
                },
                ensure_ascii=False,
            ),
        )
        await db.flush()
        return await ticket_view(db, ticket, principal)


# Explicit assignment paths bind cookie workers through authenticate before sharing
# the existing image handlers. No client-supplied query/header grants photo scope.
@router.post("/worker/assignments/{assignment_id}/uploads", response_model=PhotoView)
async def worker_upload(
    assignment_id: uuid.UUID, request: Request, file: Annotated[UploadFile, File()]
):
    return await upload(request, file)


@router.get("/worker/assignments/{assignment_id}/uploads/{upload_id}")
async def worker_download(assignment_id: uuid.UUID, upload_id: uuid.UUID, request: Request):
    return await download(upload_id, request)


@router.get(
    "/worker/assignments/{assignment_id}/uploads/{upload_id}/metadata", response_model=PhotoView
)
async def worker_image_metadata(assignment_id: uuid.UUID, upload_id: uuid.UUID, request: Request):
    return await image_metadata(upload_id, request)


@router.get("/worker/assignments/{assignment_id}/uploads/{upload_id}/source")
async def worker_image_source(assignment_id: uuid.UUID, upload_id: uuid.UUID, request: Request):
    return await image_source(upload_id, request)


@router.get("/worker/assignments/{assignment_id}/uploads/{upload_id}/variants/{variant_id}")
async def worker_image_variant(
    assignment_id: uuid.UUID, upload_id: uuid.UUID, variant_id: uuid.UUID, request: Request
):
    return await image_variant(upload_id, variant_id, request)


@router.post(
    "/worker/assignments/{assignment_id}/uploads/{upload_id}/process", response_model=PhotoView
)
async def worker_reprocess_image(
    assignment_id: uuid.UUID, upload_id: uuid.UUID, payload: ProcessInput, request: Request
):
    return await reprocess_image(upload_id, payload, request)


@router.post(
    "/worker/assignments/{assignment_id}/uploads/{upload_id}/approval", response_model=PhotoView
)
async def worker_approve_image(
    assignment_id: uuid.UUID, upload_id: uuid.UUID, payload: ApprovalInput, request: Request
):
    return await approve_image(upload_id, payload, request)


@router.delete(
    "/worker/assignments/{assignment_id}/uploads/{upload_id}/approval", response_model=PhotoView
)
async def worker_revoke_image(assignment_id: uuid.UUID, upload_id: uuid.UUID, request: Request):
    return await revoke_image(upload_id, request)
