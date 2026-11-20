"""Service reporter self-registration through local-only email OTP."""

import hmac
import secrets
from datetime import timedelta
from ipaddress import IPv6Address, ip_address

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.auth_service import normalize_email, request_code, verify_code
from app.models import AuthSession, PublicStaffInvite, User
from app.routes import session_view
from app.schemas import (
    OtpAccepted,
    OtpRequest,
    OtpVerify,
    PrototypeLogin,
    PublicGuestStart,
    PublicInviteExchange,
    RuntimeView,
    SessionView,
)
from app.security import (
    LOOPBACK_PEERS,
    authenticate,
    cookie_name,
    now,
    require_origin,
    secure_session_cookie,
    service_public_request,
    token_digest,
)
from app.services.llm import policy_from_settings, runtime_mode

router = APIRouter(prefix="/v1")


@router.get("/runtime", response_model=RuntimeView)
async def runtime(request: Request):
    settings = request.app.state.settings
    from app.security import require_local_staff_request

    try:
        require_local_staff_request(request)
        local_staff_auth = True
    except HTTPException:
        local_staff_auth = False
    staff_password_auth = False
    if settings.deployment_mode == "service":
        try:
            async with request.app.state.database.transaction() as db:
                staff_password_auth = bool(
                    await db.scalar(select(func.to_regclass("staff_credentials")))
                    and await db.scalar(select(func.to_regclass("staff_account_events")))
                )
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
    mode = runtime_mode(settings)
    photo_ai_capability = "manual"
    policy = policy_from_settings(settings)
    vision_model = policy.live_model(vision=True)
    vision_rates = policy.llm_price_table.get(vision_model)
    image_bound = (
        vision_rates.get("image_input_token_bound") if isinstance(vision_rates, dict) else None
    )
    if (
        settings.deployment_mode in {"service", "demo"}
        and mode == "live"
        and vision_model
        and type(image_bound) is int
        and 0 < image_bound <= 1_000_000_000
        and settings.llm_max_images > 0
    ):
        # The existing gateway intentionally never reads managed storage images.
        # Failure to check the storage mode cannot establish a capability.
        try:
            from starlette.concurrency import run_in_threadpool

            from app.protection import managed_db_present
            from app.storage import marker_present

            async with request.app.state.database.transaction() as db:
                managed = await managed_db_present(db)
            if not managed and not await run_in_threadpool(marker_present, settings.storage_path):
                photo_ai_capability = (
                    "restricted" if settings.booth_synthetic_live_only else "available"
                )
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
    return RuntimeView(
        photo_ai_capability=photo_ai_capability,
        public_limits={
            "ticket_hourly": settings.public_ticket_hourly_limit,
            "active_sessions": settings.public_active_sessions_limit,
            "ip_hourly": settings.public_ip_hourly_limit,
            "ticket_storage": settings.public_ticket_storage_limit,
        }
        if settings.deployment_mode == "public_demo"
        else None,
        local_staff_auth=local_staff_auth,
        staff_password_auth=staff_password_auth,
        mode=settings.deployment_mode,
        auth_method={
            "service": "email_otp",
            "prototype": "prototype_admin",
            "demo": "demo",
            "public_demo": "public_session",
        }[settings.deployment_mode],
        llm_mode=mode,
        mail_delivery=(
            settings.mail_delivery if settings.deployment_mode == "service" else "disabled"
        ),
    )


@router.post("/auth/public-guest", response_model=SessionView)
async def public_guest_start(payload: PublicGuestStart, request: Request, response: Response):
    from app.auth_service import ensure_namespace
    from app.dispatch_lock import dispatch_lock

    settings = request.app.state.settings
    if settings.deployment_mode != "public_demo":
        raise HTTPException(404, "public_guest_unavailable")
    require_origin(request)
    if request.headers.get("authorization"):
        raise HTTPException(401, "invalid_authentication")
    token = None
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        await ensure_namespace(db, settings)
        principal = None
        if request.cookies.get(cookie_name(settings)):
            try:
                principal = await authenticate(request, db)
            except HTTPException as exc:
                if exc.status_code != 401:
                    raise
        if principal:
            if principal.user.role != "reporter":
                raise HTTPException(409, "public_staff_session_active")
            result = session_view(principal.user, principal.session, settings)
        else:
            from app.public_limits import check_guest

            await check_guest(db, request)
            token = secrets.token_urlsafe(32)
            user = User(role="reporter", display_name="시연 신고자")
            db.add(user)
            await db.flush()
            session = AuthSession(
                user_id=user.id,
                token_hash=token_digest(settings, token),
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now() + timedelta(hours=settings.session_hours),
            )
            db.add(session)
            await db.flush()
            result = session_view(user, session, settings)
    if token:
        response.set_cookie(
            cookie_name(settings),
            token,
            httponly=True,
            samesite="strict",
            secure=True,
            max_age=settings.session_hours * 3600,
            path="/",
        )
    return result


@router.post("/auth/public-invite/exchange", response_model=SessionView)
async def public_invite_exchange(
    payload: PublicInviteExchange, request: Request, response: Response
):
    from app.dispatch_lock import dispatch_lock
    from app.public_staff import invite_digest, require_existing_marker

    settings = request.app.state.settings
    if settings.deployment_mode != "public_demo":
        raise HTTPException(404, "public_invite_unavailable")
    require_origin(request)
    token = secrets.token_urlsafe(32)
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        await require_existing_marker(db, settings)
        invite = await db.scalar(
            select(PublicStaffInvite)
            .where(PublicStaffInvite.token_hash == invite_digest(settings, payload.token))
            .with_for_update()
        )
        if not invite or invite.revoked or invite.consumed_at or invite.expires_at <= now():
            raise HTTPException(401, "public_invite_invalid")
        user = await db.get(User, invite.user_id, with_for_update=True)
        if not user or user.persona is not None or user.role != invite.role:
            raise HTTPException(401, "public_invite_invalid")
        previous = request.cookies.get(cookie_name(settings))
        if previous:
            await db.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_digest(settings, previous))
                .values(revoked=True)
            )
        session = AuthSession(
            user_id=user.id,
            token_hash=token_digest(settings, token),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now() + timedelta(hours=settings.session_hours),
        )
        db.add(session)
        await db.flush()
        invite.consumed_at, invite.session_id = now(), session.id
        result = session_view(user, session, settings)
    response.set_cookie(
        cookie_name(settings),
        token,
        httponly=True,
        samesite="strict",
        secure=True,
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return result


def service_settings(request):
    settings = request.app.state.settings
    if settings.deployment_mode != "service":
        raise HTTPException(404, "service_auth_unavailable")
    require_origin(request)
    return settings


def client_address(request):
    peer = request.client.host if request.client else "unknown"
    # The loopback web proxy preserves both Host and Cloudflare's single IP header.
    if peer in LOOPBACK_PEERS and service_public_request(request):
        addresses = request.headers.getlist("cf-connecting-ip")
        if len(addresses) == 1 and "%" not in addresses[0]:
            try:
                address = ip_address(addresses[0])
                if isinstance(address, IPv6Address) and address.ipv4_mapped:
                    address = address.ipv4_mapped
                return str(address)
            except ValueError:
                pass
    return peer


@router.post("/auth/otp/request", response_model=OtpAccepted, status_code=202)
async def otp_request(payload: OtpRequest, request: Request):
    settings = service_settings(request)
    email = normalize_email(payload.email, settings)
    async with request.app.state.database.transaction() as db:
        error = await request_code(db, settings, email, client_address(request))
    # Raise only after commit: request and failed-verify counters must not roll back.
    if error:
        raise HTTPException(429 if error == "auth_rate_limited" else 503, error)
    return OtpAccepted(delivery=settings.mail_delivery)


async def revoke_session(database, token_hash):
    # No User lock is retained while touching a previous account's session.
    async with database.transaction() as db:
        await db.execute(
            update(AuthSession).where(AuthSession.token_hash == token_hash).values(revoked=True)
        )


async def final_session(database, settings, user_id, session_id):
    from sqlalchemy import select

    from app.auth_service import (
        ensure_namespace,
        prototype_dispatcher_eligible,
        service_principal_eligible,
        staff_identity_eligible,
    )
    from app.models import User

    async with database.transaction() as db:
        await ensure_namespace(db, settings)
        user = await db.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        session = await db.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not (
                prototype_dispatcher_eligible(user)
                if settings.deployment_mode == "prototype"
                else service_principal_eligible(user, settings)
            )
            or not session
            or session.user_id != user_id
            or session.revoked
            or session.expires_at <= now()
        ):
            raise HTTPException(503, "authentication_retry_required")
        if staff_identity_eligible(user, settings):
            from app.staff_auth import staff_principal_eligible

            if not await staff_principal_eligible(db, user):
                raise HTTPException(503, "authentication_retry_required")
        # Snapshot the current role only here, while User SHARE and session locks remain held.
        return session_view(user, session, settings)


@router.post("/auth/otp/verify", response_model=SessionView)
async def otp_verify(payload: OtpVerify, request: Request, response: Response):
    settings = service_settings(request)
    email = normalize_email(payload.email, settings)
    database = request.app.state.database
    token, session_id, user_id = None, None, None
    # Stage 1: OTP and new credential commit; never lock the old account's session here.
    async with database.transaction() as db:
        user, error = await verify_code(db, settings, email, payload.code, client_address(request))
        if user:
            token = secrets.token_urlsafe(32)
            session = AuthSession(
                user_id=user.id,
                token_hash=token_digest(settings, token),
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now() + timedelta(hours=settings.session_hours),
                revoked=False,
            )
            db.add(session)
            await db.flush()
            user_id, session_id = user.id, session.id
    if error:
        raise HTTPException(429 if error == "auth_rate_limited" else 400, error)
    try:
        # Stage 2 commits independently, so cross-account login cannot invert User/session locks.
        previous = request.cookies.get(cookie_name(settings))
        if previous and previous != token:
            await revoke_session(database, token_digest(settings, previous))
        # Stage 3 rechecks revocation and current role after any intervening staff change.
        result = await final_session(database, settings, user_id, session_id)
    except Exception:
        try:
            await revoke_session(database, token_digest(settings, token))
        except Exception:
            # An undelivered credential may await expiry cleanup. Never claim revocation succeeded.
            pass
        # The OTP is consumed. A fresh code is required; no cookie or token is disclosed.
        raise HTTPException(503, "authentication_retry_required") from None
    response.set_cookie(
        cookie_name(settings),
        token,
        httponly=True,
        samesite="strict",
        secure=secure_session_cookie(request),
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return result


@router.post("/auth/prototype-login", response_model=SessionView)
async def prototype_login(payload: PrototypeLogin, request: Request, response: Response):
    from app.auth_service import (
        PROTOTYPE_DISPATCHER_ID,
        ensure_namespace,
        locked_rate,
        prototype_dispatcher_eligible,
    )

    settings = request.app.state.settings
    if settings.deployment_mode != "prototype":
        raise HTTPException(404, "prototype_auth_unavailable")
    require_origin(request)
    database = request.app.state.database
    token = secrets.token_urlsafe(32)
    async with database.transaction() as db:
        await ensure_namespace(db, settings)
        _, permitted = await locked_rate(
            db, settings, "prototype-admin", client_address(request), "verify"
        )
        user = await db.scalar(
            select(User).where(User.id == PROTOTYPE_DISPATCHER_ID).with_for_update(read=True)
        )
        valid_password = hmac.compare_digest(
            payload.password.encode(), settings.prototype_admin_password.get_secret_value().encode()
        )
        valid = (
            permitted
            and payload.email == "baroai-admin@snu.ac.kr"
            and valid_password
            and prototype_dispatcher_eligible(user)
        )
        if valid:
            session = AuthSession(
                user_id=user.id,
                token_hash=token_digest(settings, token),
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now() + timedelta(hours=settings.session_hours),
                revoked=False,
            )
            db.add(session)
            await db.flush()
            user_id, session_id = user.id, session.id
    if not permitted:
        raise HTTPException(429, "auth_rate_limited")
    if not valid:
        raise HTTPException(401, "authentication_failed")
    try:
        previous = request.cookies.get(cookie_name(settings))
        if previous:
            await revoke_session(database, token_digest(settings, previous))
        result = await final_session(database, settings, user_id, session_id)
    except Exception:
        try:
            await revoke_session(database, token_digest(settings, token))
        except Exception:
            pass
        raise HTTPException(503, "authentication_retry_required") from None
    response.set_cookie(
        cookie_name(settings),
        token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookie,
        max_age=settings.session_hours * 3600,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/auth/local-staff", response_model=SessionView)
async def local_staff_login(payload: OtpRequest, request: Request, response: Response):
    from app.auth_service import LOCAL_STAFF, ensure_namespace, local_staff_eligible
    from app.security import require_local_staff_request

    settings = service_settings(request)
    require_local_staff_request(request)
    email = normalize_email(payload.email, settings)
    identity = LOCAL_STAFF.get(email)
    if not identity:
        raise HTTPException(404, "local_staff_unavailable")
    user_id = identity[0]
    database = request.app.state.database
    token = secrets.token_urlsafe(32)
    async with database.transaction() as db:
        await ensure_namespace(db, settings)
        # Serialize first creation; never adopt or modify an existing account on collision.
        from sqlalchemy import text

        await db.execute(text("SELECT pg_advisory_xact_lock(24002402)"))
        user = None
        for reserved_address, item in LOCAL_STAFF.items():
            identity_id, identity_persona, identity_role, identity_name = item
            current = await db.scalar(
                select(User).where(User.id == identity_id).with_for_update(read=True)
            )
            conflict = await db.scalar(
                select(User.id)
                .where(
                    (User.persona == identity_persona) | (User.verified_email == reserved_address)
                )
                .where(User.id != identity_id)
            )
            if conflict or (current is not None and not local_staff_eligible(current, settings)):
                raise HTTPException(409, "local_staff_identity_conflict")
            if current is None:
                current = User(
                    id=identity_id,
                    persona=identity_persona,
                    role=identity_role,
                    display_name=identity_name,
                    verified_email=None,
                )
                db.add(current)
                await db.flush()
            if identity_id == user_id:
                user = current
        assert user is not None
        session = AuthSession(
            user_id=user.id,
            token_hash=token_digest(settings, token),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now() + timedelta(hours=settings.session_hours),
            revoked=False,
        )
        db.add(session)
        await db.flush()
        session_id = session.id
    try:
        previous = request.cookies.get(cookie_name(settings))
        if previous:
            await revoke_session(database, token_digest(settings, previous))
        result = await final_session(database, settings, user_id, session_id)
    except Exception:
        await revoke_session(database, token_digest(settings, token))
        raise HTTPException(503, "authentication_retry_required") from None
    response.set_cookie(
        cookie_name(settings),
        token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookie,
        max_age=settings.session_hours * 3600,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result
