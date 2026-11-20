"""Separate approval-gated staff credentials for service mode."""

import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Literal

import anyio
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from app.auth_routes import client_address, revoke_session, service_settings
from app.auth_service import (
    LOCAL_STAFF,
    LOCAL_STAFF_IDS,
    ensure_namespace,
    local_staff_identity,
    locked_rate,
)
from app.models import Assignment, AuthSession, Base, IdentityTimestamps, User
from app.routes import session_view
from app.schemas import SessionView
from app.security import (
    authenticate,
    cookie_name,
    now,
    require_local_staff_request,
    role,
    secure_session_cookie,
    token_digest,
)


class StaffCredential(IdentityTimestamps, Base):
    __tablename__ = "staff_credentials"
    __table_args__ = (
        CheckConstraint("role IN ('dispatcher','worker')", name="staff_credential_role"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','revoked')", name="staff_credential_status"
        ),
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)


class StaffAccountEvent(IdentityTimestamps, Base):
    __tablename__ = "staff_account_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('applied','approved','rejected','revoked')", name="staff_event_action"
        ),
    )
    credential_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_credentials.id"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class StaffLogin(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", value):
            raise ValueError("invalid username")
        return value

    @field_validator("password")
    @classmethod
    def limit_password(cls, value):
        password = value.get_secret_value()
        try:
            valid = 8 <= len(password) <= 128 and len(password.encode("utf-8")) <= 512
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise ValueError("invalid password length")
        return value


class StaffRegister(StaffLogin):
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["dispatcher", "worker"]

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("display name required")
        return value


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("reason required")
        return value


class ApplicationView(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    role: Literal["dispatcher", "worker"]
    created_at: datetime


class ApplicationsView(BaseModel):
    items: list[ApplicationView]


class StatusView(BaseModel):
    status: Literal["pending", "approved", "rejected", "revoked"]


# Fixed public dummy hash equalizes unknown-name verification cost; not an account credential.
_DUMMY = (
    "$argon2id$v=19$m=19456,t=2,p=1$YmFyb2FpLWR1bW15LXNhbHQ$"
    "fKMSTpQKE863uZeB4WvzfonTKWiVpp664Y4jF/GizHg"
)
_hash_limiter = anyio.CapacityLimiter(2)


def _hash(password):
    return Argon2id(
        salt=secrets.token_bytes(16), length=32, iterations=2, lanes=1, memory_cost=19456
    ).derive_phc_encoded(password.encode())


def _verify(password, encoded):
    try:
        Argon2id.verify_phc_encoded(password.encode(), encoded)
        return True
    except (InvalidKey, ValueError):
        return False


async def staff_principal_eligible(db, user):
    """Call only for staff personas; legacy sessions never need the new table."""
    if (
        not user
        or user.id in LOCAL_STAFF_IDS
        or not user.persona
        or not user.persona.startswith("staff_")
    ):
        return False
    credential = await db.scalar(select(StaffCredential).where(StaffCredential.user_id == user.id))
    return bool(
        credential
        and credential.status == "approved"
        and credential.role == user.role
        and user.role in {"dispatcher", "worker"}
        and user.verified_email is None
        and user.persona == "staff_" + user.id.hex
    )


async def local_staff_principal_eligible(db, user, settings):
    if not local_staff_identity(user, settings):
        return False
    if not await db.scalar(select(func.to_regclass("staff_credentials"))):
        return settings.local_staff_auth
    username = next(
        email.split("@")[0] for email, item in LOCAL_STAFF.items() if item[0] == user.id
    )
    rows = list(
        await db.scalars(
            select(StaffCredential).where(
                (StaffCredential.user_id == user.id) | (StaffCredential.username == username)
            )
        )
    )
    if not rows:
        return settings.local_staff_auth
    return bool(
        len(rows) == 1
        and rows[0].user_id == user.id
        and rows[0].username == username
        and rows[0].status == "approved"
        and rows[0].role == user.role
    )


router = APIRouter(prefix="/v1")


async def require_staff_schema(db):
    if not await db.scalar(select(func.to_regclass("staff_credentials"))) or not await db.scalar(
        select(func.to_regclass("staff_account_events"))
    ):
        raise HTTPException(503, "staff_auth_unavailable")


@router.post("/auth/staff/register", response_model=StatusView)
async def register(payload: StaffRegister, request: Request):
    settings = service_settings(request)
    async with request.app.state.database.transaction() as db:
        await ensure_namespace(db, settings)
        await require_staff_schema(db)
        _, allowed = await locked_rate(
            db, settings, payload.username, client_address(request), "staff_register"
        )
    if not allowed:
        raise HTTPException(429, "auth_rate_limited")
    hashed = await anyio.to_thread.run_sync(
        _hash, payload.password.get_secret_value(), limiter=_hash_limiter
    )
    if payload.username in {email.split("@")[0] for email in LOCAL_STAFF}:
        return StatusView(status="pending")
    async with request.app.state.database.transaction() as db:
        credential_id = await db.scalar(
            insert(StaffCredential)
            .values(
                id=uuid.uuid4(),
                username=payload.username,
                password_hash=hashed,
                display_name=payload.display_name,
                role=payload.role,
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=[StaffCredential.username])
            .returning(StaffCredential.id)
        )
        if credential_id is not None:
            db.add(StaffAccountEvent(credential_id=credential_id, action="applied"))
    return StatusView(status="pending")


@router.post("/auth/staff/login", response_model=SessionView)
async def login(payload: StaffLogin, request: Request, response: Response):
    settings = service_settings(request)
    database = request.app.state.database
    async with database.transaction() as db:
        await ensure_namespace(db, settings)
        await require_staff_schema(db)
        _, allowed = await locked_rate(
            db, settings, payload.username, client_address(request), "staff_login"
        )
        credential = await db.scalar(
            select(StaffCredential).where(StaffCredential.username == payload.username)
        )
        encoded = credential.password_hash if credential else _DUMMY
        credential_id = credential.id if credential else None
    if not allowed:
        raise HTTPException(429, "auth_rate_limited")
    valid = await anyio.to_thread.run_sync(
        _verify, payload.password.get_secret_value(), encoded, limiter=_hash_limiter
    )
    if not valid or credential_id is None:
        raise HTTPException(401, "authentication_failed")
    token = secrets.token_urlsafe(32)
    async with database.transaction() as db:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        # User lock precedes credential and session locks, as in existing service auth.
        user_id = await db.scalar(
            select(StaffCredential.user_id).where(StaffCredential.id == credential_id)
        )
        user = (
            await db.scalar(select(User).where(User.id == user_id).with_for_update(read=True))
            if user_id
            else None
        )
        credential = await db.scalar(
            select(StaffCredential).where(StaffCredential.id == credential_id).with_for_update()
        )
        if user and local_staff_identity(user, settings):
            require_local_staff_request(request, require_flag=False)
        if credential is None or credential.password_hash != encoded:
            error = "authentication_failed"
        elif credential.status == "pending":
            error = "staff_approval_pending"
        elif (
            not user
            or credential.user_id != user.id
            or not (
                await local_staff_principal_eligible(db, user, settings)
                if local_staff_identity(user, settings)
                else await staff_principal_eligible(db, user)
            )
        ):
            error = "authentication_failed"
        else:
            error = None
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
    if error:
        raise HTTPException(403 if error == "staff_approval_pending" else 401, error)
    try:
        previous = request.cookies.get(cookie_name(settings))
        if previous:
            await revoke_session(database, token_digest(settings, previous))
        async with database.transaction() as db:
            user = await db.scalar(
                select(User).where(User.id == user_id).with_for_update(read=True)
            )
            session = await db.scalar(
                select(AuthSession).where(AuthSession.id == session_id).with_for_update()
            )
            if (
                not (
                    await local_staff_principal_eligible(db, user, settings)
                    if local_staff_identity(user, settings)
                    else await staff_principal_eligible(db, user)
                )
                or not session
                or session.revoked
                or session.expires_at <= now()
            ):
                raise ValueError("stale session")
            result = session_view(user, session, settings)
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
        secure=secure_session_cookie(request),
        max_age=settings.session_hours * 3600,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/admin/staff-applications", response_model=ApplicationsView)
async def applications(request: Request):
    if request.app.state.settings.deployment_mode != "service":
        raise HTTPException(404, "service_auth_unavailable")
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await require_staff_schema(db)
        rows = (
            await db.scalars(
                select(StaffCredential)
                .where(StaffCredential.status == "pending")
                .order_by(StaffCredential.created_at, StaffCredential.id)
            )
        ).all()
        return ApplicationsView(
            items=[
                ApplicationView(
                    id=row.id,
                    username=row.username,
                    display_name=row.display_name,
                    role=row.role,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )


@router.post("/admin/staff-applications/{application_id}/decision", response_model=StatusView)
async def decide(application_id: uuid.UUID, payload: Decision, request: Request):
    service_settings(request)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await require_staff_schema(db)
        credential = await db.scalar(
            select(StaffCredential).where(StaffCredential.id == application_id).with_for_update()
        )
        if not credential:
            raise HTTPException(404, "staff_application_not_found")
        if credential.status != "pending":
            raise HTTPException(409, "staff_application_decided")
        if payload.decision == "approve":
            user_id = uuid.uuid4()
            user = User(
                id=user_id,
                persona="staff_" + user_id.hex,
                verified_email=None,
                role=credential.role,
                display_name=credential.display_name,
            )
            db.add(user)
            await db.flush()
            credential.user_id = user_id
        credential.status = "approved" if payload.decision == "approve" else "rejected"
        credential.decided_by = principal.user.id
        credential.decided_at = now()
        credential.decision_reason = payload.reason
        db.add(
            StaffAccountEvent(
                credential_id=credential.id,
                actor_id=principal.user.id,
                action=credential.status,
                reason=payload.reason,
            )
        )
        return StatusView(status=credential.status)


class RevokeInput(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    confirm_open_assignments: bool = False

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("reason required")
        return value


async def open_assignment_count(db, user_id):
    return await db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(
            Assignment.worker_id == user_id,
            Assignment.revoked.is_(False),
            Assignment.visit_ended_at.is_(None),
            Assignment.stopped_at.is_(None),
        )
    )


@router.get("/admin/staff-accounts")
async def accounts(request: Request):
    service_settings(request)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await require_staff_schema(db)
        rows = (
            await db.scalars(
                select(StaffCredential)
                .where(StaffCredential.status == "approved")
                .order_by(StaffCredential.created_at, StaffCredential.id)
            )
        ).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "display_name": row.display_name,
                    "role": row.role,
                    "user_id": row.user_id,
                    "open_assignments": await open_assignment_count(db, row.user_id),
                }
                for row in rows
            ]
        }


@router.get("/admin/staff-account-events")
async def account_events(request: Request):
    service_settings(request)
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await require_staff_schema(db)
        rows = (
            await db.execute(
                select(StaffAccountEvent, StaffCredential.display_name, User.display_name)
                .join(StaffCredential, StaffCredential.id == StaffAccountEvent.credential_id)
                .outerjoin(User, User.id == StaffAccountEvent.actor_id)
                .order_by(StaffAccountEvent.created_at.desc(), StaffAccountEvent.id.desc())
                .limit(100)
            )
        ).all()
        return {
            "items": [
                {
                    "action": event.action,
                    "created_at": event.created_at,
                    "target_name": target,
                    "actor_name": actor,
                    "reason": event.reason,
                }
                for event, target, actor in rows
            ]
        }


@router.post("/admin/staff-accounts/{credential_id}/revoke", response_model=StatusView)
async def revoke_account(credential_id: uuid.UUID, payload: RevokeInput, request: Request):
    settings = service_settings(request)
    async with request.app.state.database.transaction() as db:
        # authenticate holds the dispatch gate before the actor's User/Session locks.
        principal = await authenticate(request, db)
        role(principal, "dispatcher")
        await require_staff_schema(db)
        credential = await db.scalar(
            select(StaffCredential).where(StaffCredential.id == credential_id).with_for_update()
        )
        if credential is None or credential.status != "approved" or credential.user_id is None:
            raise HTTPException(404, "staff_account_not_found")
        if credential.user_id == principal.user.id:
            raise HTTPException(409, "staff_self_revoke_forbidden")
        user = await db.scalar(select(User).where(User.id == credential.user_id).with_for_update())
        if user is None:
            raise HTTPException(409, "staff_account_invalid")
        if credential.role == "dispatcher":
            from app.auth_service import local_staff_identity, service_account_eligible

            candidates = (
                await db.scalars(select(User).where(User.role == "dispatcher", User.id != user.id))
            ).all()
            other_exists = False
            for candidate in candidates:
                if service_account_eligible(candidate):
                    other_exists = True
                elif local_staff_identity(candidate, settings):
                    other_exists = await local_staff_principal_eligible(db, candidate, settings)
                else:
                    other_exists = await staff_principal_eligible(db, candidate)
                if other_exists:
                    break
            if not other_exists:
                raise HTTPException(409, "staff_last_dispatcher_forbidden")
        count = await open_assignment_count(db, user.id)
        if count and not payload.confirm_open_assignments:
            raise HTTPException(409, "staff_open_assignments_confirmation_required")
        credential.status = "revoked"
        credential.decided_by = principal.user.id
        credential.decided_at = now()
        credential.decision_reason = payload.reason
        user.auth_epoch += 1
        await db.execute(
            update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True)
        )
        db.add(
            StaffAccountEvent(
                credential_id=credential.id,
                actor_id=principal.user.id,
                action="revoked",
                reason=payload.reason,
            )
        )
        return StatusView(status="revoked")
