import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assignment, AuthSession, PublicStaffInvite, User

COOKIE = "baroai_session"
SERVICE_COOKIE = "baroai_service_session"
PROTOTYPE_COOKIE = "baroai_prototype_session"
PUBLIC_DEMO_COOKIE = "baroai_public_demo_session"
LOOPBACK_PEERS = {"127.0.0.1", "::1"}
LOCAL_SERVICE_HOSTS = {
    f"{host}:{port}"
    for host in ("127.0.0.1", "localhost", "[::1]")
    for port in (5174, 8001)
}


def service_public_request(request: Request) -> bool:
    origin = request.app.state.settings.service_public_origin
    return bool(origin and request.headers.getlist("host") == [urlsplit(origin).netloc])


def service_host_allowed(request: Request) -> bool:
    hosts = request.headers.getlist("host")
    return (len(hosts) == 1 and hosts[0] in LOCAL_SERVICE_HOSTS) or service_public_request(request)


def secure_session_cookie(request: Request) -> bool:
    settings = request.app.state.settings
    return settings.secure_cookie or settings.app_env == "prod" or service_public_request(request)


def cookie_name(settings):
    return {
        "service": SERVICE_COOKIE,
        "prototype": PROTOTYPE_COOKIE,
        "demo": COOKIE,
        "public_demo": PUBLIC_DEMO_COOKIE,
    }[settings.deployment_mode]


def token_digest(settings, value):
    if settings.deployment_mode == "demo":
        return digest(value)
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        (settings.deployment_mode + ":" + value).encode(),
        hashlib.sha256,
    ).hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


def require_origin(request: Request) -> None:
    settings = request.app.state.settings
    expected = (
        settings.service_public_origin if service_public_request(request) else settings.app_base_url
    )
    if request.headers.getlist("origin") != [expected]:
        raise HTTPException(403, "origin_rejected")


def require_local_staff_request(request: Request, *, require_flag=True) -> None:
    settings = request.app.state.settings
    try:
        host = urlsplit("http://" + request.headers.get("host", ""))
        valid = (
            (settings.local_staff_auth or not require_flag)
            and settings.deployment_mode == "service"
            and settings.app_env in {"dev", "test"}
            and request.client is not None
            and request.client.host in LOOPBACK_PEERS
            and len(request.headers.getlist("host")) == 1
            and host.hostname in {"127.0.0.1", "localhost", "::1"}
            and host.port in {5174, 8001}
            and not host.username
            and not host.password
            and not host.path
            and not host.query
            and not host.fragment
            and request.headers.getlist("origin") in ([], [settings.app_base_url])
            and not any(
                name in {"forwarded", "cf-connecting-ip"} or name.startswith("x-forwarded-")
                for name in request.headers
            )
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(403, "local_staff_unavailable")


@dataclass
class Principal:
    user: User
    session: AuthSession | None = None
    assignment: Assignment | None = None

    @property
    def actor_scope(self) -> str:
        return str(self.assignment.id if self.assignment else self.user.id)


def assignment_current(assignment, worker):
    # Expiry prevents new token requests, but does not orphan already-saved reports.
    return bool(
        assignment
        and worker
        and not assignment.revoked
        and worker.role == "worker"
        and assignment.auth_epoch == worker.auth_epoch
    )


def current_assignment_clause():
    return and_(
        Assignment.revoked.is_(False),
        User.role == "worker",
        Assignment.auth_epoch == User.auth_epoch,
    )


async def lock_worker_user(db, settings, user_id):
    if settings.deployment_mode != "demo":
        return await db.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    return await db.get(User, user_id)


async def lock_report_worker(db, settings, report_id):
    if settings.deployment_mode != "demo":
        from app.models import CompletionReport

        worker_id = await db.scalar(
            select(Assignment.worker_id)
            .join(CompletionReport, CompletionReport.assignment_id == Assignment.id)
            .where(CompletionReport.id == report_id)
        )
        if worker_id is not None:
            await lock_worker_user(db, settings, worker_id)


async def authenticate(request: Request, db: AsyncSession, *, worker_allowed=False) -> Principal:
    from app.dispatch_lock import dispatch_lock

    await dispatch_lock(db)
    settings = request.app.state.settings
    if settings.deployment_mode == "demo" and (settings.app_env != "dev" or not settings.demo_auth):
        raise HTTPException(401, "demo_authentication_unavailable")
    from app.auth_service import ensure_namespace

    await ensure_namespace(db, settings)
    cookie = cookie_name(settings)
    other_cookies = {COOKIE, SERVICE_COOKIE, PROTOTYPE_COOKIE, PUBLIC_DEMO_COOKIE} - {cookie}
    bearer = request.headers.get("authorization")
    if (
        any(request.cookies.get(name) for name in other_cookies)
        and not request.cookies.get(cookie)
        and not bearer
    ):
        raise HTTPException(401, "authentication_scope_mismatch")
    if bearer:
        if settings.deployment_mode in {"public_demo", "service"}:
            raise HTTPException(401, "invalid_authentication")
        if not worker_allowed or not bearer.startswith("Bearer ") or request.cookies.get(cookie):
            raise HTTPException(401, "invalid_authentication")
        token = bearer[7:]
        if not 40 <= len(token) <= 200:
            raise HTTPException(401, "invalid_worker_token")
        assignment = await db.scalar(
            select(Assignment).where(Assignment.token_hash == token_digest(settings, token))
        )
        if not assignment or assignment.revoked or assignment.expires_at <= now():
            raise HTTPException(401, "invalid_worker_token")
        if settings.deployment_mode != "demo":
            user = await db.scalar(
                select(User)
                .where(User.id == assignment.worker_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        else:
            user = await db.get(User, assignment.worker_id)
        if not assignment_current(assignment, user):
            raise HTTPException(401, "invalid_worker_token")
        principal = Principal(user=user, assignment=assignment)
    else:
        token = request.cookies.get(cookie, "")
        if not 40 <= len(token) <= 200:
            raise HTTPException(401, "authentication_required")
        expected_user_id = None
        scoped_user = None
        if settings.deployment_mode != "demo":
            expected_user_id = await db.scalar(
                select(AuthSession.user_id).where(
                    AuthSession.token_hash == token_digest(settings, token)
                )
            )
            if expected_user_id is None:
                raise HTTPException(401, "authentication_required")
            # All service operations share User -> AuthSession -> ticket ordering.
            scoped_user = await db.scalar(
                select(User)
                .where(User.id == expected_user_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        session = await db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_digest(settings, token))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not session
            or session.revoked
            or session.expires_at <= now()
            or (expected_user_id is not None and session.user_id != expected_user_id)
        ):
            raise HTTPException(401, "authentication_required")
        principal = Principal(
            user=scoped_user or await db.get(User, session.user_id), session=session
        )
    if settings.deployment_mode == "public_demo" and (
        not principal.user
        or principal.user.persona is not None
        or principal.user.role not in {"reporter", "dispatcher", "worker"}
    ):
        raise HTTPException(401, "authentication_required")
    if settings.deployment_mode == "public_demo" and principal.user.role in {
        "dispatcher",
        "worker",
    }:
        invite = await db.scalar(
            select(PublicStaffInvite).where(PublicStaffInvite.session_id == principal.session.id)
        )
        if (
            not invite
            or invite.revoked
            or invite.consumed_at is None
            or invite.user_id != principal.user.id
            or invite.role != principal.user.role
        ):
            raise HTTPException(401, "authentication_required")
    if settings.deployment_mode == "service":
        from app.auth_service import (
            LOCAL_STAFF_IDS,
            local_staff_identity,
            service_principal_eligible,
            staff_identity_eligible,
        )

        if (
            principal.user
            and principal.user.id in LOCAL_STAFF_IDS
            and not local_staff_identity(principal.user, settings)
        ):
            raise HTTPException(401, "authentication_required")
        if local_staff_identity(principal.user, settings):
            require_local_staff_request(request, require_flag=False)
            from app.staff_auth import local_staff_principal_eligible

            if not await local_staff_principal_eligible(db, principal.user, settings):
                raise HTTPException(401, "authentication_required")
        if not service_principal_eligible(principal.user, settings):
            raise HTTPException(401, "service_email_domain_required")
        if staff_identity_eligible(principal.user, settings):
            from app.staff_auth import staff_principal_eligible

            if not await staff_principal_eligible(db, principal.user):
                raise HTTPException(401, "authentication_required")
    if settings.deployment_mode == "prototype":
        from app.auth_service import prototype_dispatcher_eligible, worker_eligible

        if principal.assignment:
            if not worker_eligible(principal.user, settings):
                raise HTTPException(401, "invalid_worker_token")
        elif not prototype_dispatcher_eligible(principal.user):
            raise HTTPException(401, "authentication_required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_origin(request)
        if principal.session and not secrets.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), principal.session.csrf_token.encode()
        ):
            raise HTTPException(403, "csrf_rejected")
    if worker_allowed:
        from app.auth_service import demo_worker_session_eligible

        scoped_id = request.path_params.get("assignment_id")
        if principal.assignment:
            if scoped_id is not None and str(principal.assignment.id) != str(scoped_id):
                raise HTTPException(403, "assignment_forbidden")
        elif principal.user.role == "worker" and (
            settings.deployment_mode in {"service", "public_demo"}
            or demo_worker_session_eligible(principal.user, settings)
        ):
            if scoped_id is None:
                raise HTTPException(403, "worker_assignment_required")
            assignment = await db.get(Assignment, uuid.UUID(str(scoped_id)))
            if (
                not assignment
                or assignment.worker_id != principal.user.id
                or not assignment_current(assignment, principal.user)
            ):
                raise HTTPException(404, "assignment_not_found")
            # Bind without locking the assignment yet. Routes lock ticket first,
            # then locked_assignment; cookies retain their CSRF/session identity.
            principal.assignment = assignment
        if scoped_id is not None and principal.assignment is None:
            raise HTTPException(403, "worker_session_required")
    return principal


def role(principal: Principal, expected: str) -> None:
    if principal.assignment or principal.user.role != expected:
        raise HTTPException(403, "role_forbidden")


async def locked_assignment(db: AsyncSession, principal: Principal, assignment_id: uuid.UUID):
    # Call only after locking the ticket: all writes share ticket -> assignment lock order.
    assignment = await db.scalar(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not principal.assignment
        or not assignment
        or principal.assignment.id != assignment_id
        or not assignment_current(assignment, principal.user)
        or (principal.session is None and assignment.expires_at <= now())
    ):
        raise HTTPException(401, "invalid_worker_token")
    return assignment
