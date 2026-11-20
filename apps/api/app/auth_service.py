"""Namespace guards and atomic encrypted OTP delivery intents. Never logs addresses or codes."""

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.models import AuthRateLimit, DeploymentIdentity, EmailChallenge, User


def keyed(settings, value):
    return hmac.new(
        settings.otp_pepper.get_secret_value().encode(), value.encode(), hashlib.sha256
    ).hexdigest()


SERVICE_EMAIL_DOMAIN = "snu.ac.kr"
PROTOTYPE_DISPATCHER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
PROTOTYPE_EMAIL = "baroai-admin@snu.ac.kr"
PROTOTYPE_PERSONA = "prototype_dispatcher"

# Reserved identities never represent verified mailboxes, even when local access is off.
LOCAL_STAFF = {
    "baroai-manager@snu.ac.kr": (
        uuid.UUID("00000000-0000-4000-8000-000000000101"),
        "local_service_dispatcher",
        "dispatcher",
        "가상 담당자",
    ),
    "baroai-worker@snu.ac.kr": (
        uuid.UUID("00000000-0000-4000-8000-000000000102"),
        "local_service_worker",
        "worker",
        "가상 작업자",
    ),
}
LOCAL_STAFF_IDS = frozenset(item[0] for item in LOCAL_STAFF.values())


def local_staff_identity(user, settings):
    return bool(
        settings.deployment_mode == "service"
        and settings.app_env in {"dev", "test"}
        and user
        and user.verified_email is None
        and any((user.id, user.persona, user.role) == item[:3] for item in LOCAL_STAFF.values())
    )


def local_staff_eligible(user, settings):
    return settings.local_staff_auth and local_staff_identity(user, settings)


def service_principal_eligible(user, settings):
    return (
        service_account_eligible(user)
        or local_staff_identity(user, settings)
        or staff_identity_eligible(user, settings)
    )


def staff_identity_eligible(user, settings):
    # Created only by the approval transaction; credential state is checked on authentication.
    return bool(
        settings.deployment_mode == "service"
        and user
        and user.id
        and user.id not in LOCAL_STAFF_IDS
        and user.role in {"dispatcher", "worker"}
        and user.verified_email is None
        and user.persona == "staff_" + user.id.hex
    )


def reserved_email(email):
    try:
        return canonical_email(email) in LOCAL_STAFF
    except HTTPException:
        return False


def prototype_dispatcher_eligible(user):
    return bool(
        user
        and user.id == PROTOTYPE_DISPATCHER_ID
        and user.verified_email == PROTOTYPE_EMAIL
        and user.persona == PROTOTYPE_PERSONA
        and user.role == "dispatcher"
    )


def demo_worker_session_eligible(user, settings):
    if (
        not user
        or user.role != "worker"
        or settings.deployment_mode != "demo"
        or settings.app_env != "dev"
        or not settings.demo_auth
    ):
        return False
    if user.persona in {"worker_a", "worker_b"}:
        return True
    return (
        settings.planning_dataset_provider == "db"
        and user.persona in {"worker_c", "worker_d", "worker_e", "worker_f", "worker_g"}
        and user.verified_email is None
    )


def worker_eligible(user, settings):
    if not user or user.role != "worker":
        return False
    if settings.deployment_mode == "service":
        return service_principal_eligible(user, settings)
    if settings.deployment_mode == "prototype":
        return user.persona in {"worker_a", "worker_b"} and user.verified_email is None
    if settings.deployment_mode == "public_demo":
        return user.persona is None
    return True


def namespace_for(settings):
    if settings.deployment_mode == "service":
        return settings.service_namespace
    if settings.deployment_mode == "prototype":
        return settings.prototype_namespace
    if settings.deployment_mode == "public_demo":
        return settings.public_demo_namespace
    return "demo"


def canonical_email(value):
    # Validate ASCII before casefold: U+017F must not fold into an allowed s.
    if not isinstance(value, str):
        raise HTTPException(422, "email_invalid")
    stripped = value.strip()
    if not stripped.isascii():
        raise HTTPException(422, "email_invalid")
    normalized = stripped.casefold()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9.-]+", normalized):
        raise HTTPException(422, "email_invalid")
    return normalized


def is_service_email(value):
    try:
        return canonical_email(value).split("@", 1)[1] == SERVICE_EMAIL_DOMAIN
    except HTTPException:
        return False


def service_account_eligible(user):
    return bool(user and user.persona is None and is_service_email(user.verified_email))


def normalize_email(value, settings):
    normalized = canonical_email(value)
    domain = (
        SERVICE_EMAIL_DOMAIN
        if settings.deployment_mode == "service"
        else settings.allowed_email_domain
    )
    if normalized.split("@", 1)[1] != domain.casefold():
        raise HTTPException(422, "email_domain_rejected")
    return normalized


async def ensure_namespace(db, settings):
    # Serialize first initialization and prevent legacy demo data becoming service data.
    if settings.deployment_mode == "public_demo":
        schema = await db.scalar(text("SELECT current_schema()"))
        if schema != "public":
            raise HTTPException(503, "public_schema_mismatch")
    marker = await db.get(DeploymentIdentity, 1)
    if marker is None:
        await db.execute(text("SELECT pg_advisory_xact_lock(24002401)"))
        marker = await db.get(DeploymentIdentity, 1)
    namespace = namespace_for(settings)
    if marker is None:
        if settings.deployment_mode == "public_demo":
            # Inspect every ordinary table in the active schema, not just users.
            names = (
                (
                    await db.execute(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') "
                            "AND c.relname != 'alembic_version'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            preparer = db.bind.dialect.identifier_preparer
            for name in names:
                quoted = f"{preparer.quote(schema)}.{preparer.quote(name)}"
                if await db.scalar(text(f"SELECT EXISTS (SELECT 1 FROM {quoted} LIMIT 1)")):
                    raise HTTPException(503, "public_database_not_empty")
        if settings.deployment_mode != "demo" and await db.scalar(select(User.id).limit(1)):
            raise HTTPException(503, "service_database_not_empty")
        marker = DeploymentIdentity(id=1, mode=settings.deployment_mode, namespace=namespace)
        db.add(marker)
        await db.flush()
    if marker.mode != settings.deployment_mode or marker.namespace != namespace:
        raise HTTPException(503, "deployment_namespace_mismatch")


async def locked_rate(db, settings, address, client, action):
    stamp = await db.scalar(select(func.clock_timestamp()))
    specs = [
        (keyed(settings, f"{action}:address:{address}"), 5 if action == "request" else 30),
        (keyed(settings, f"{action}:client:{client}"), 20 if action == "request" else 100),
    ]
    permitted = True
    for key, limit in sorted(specs):
        lock = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        rate = await db.get(AuthRateLimit, key)
        if rate is None:
            rate = AuthRateLimit(key=key, window_start=stamp, count=0)
            db.add(rate)
        if stamp - rate.window_start >= timedelta(minutes=15):
            rate.window_start, rate.count = stamp, 0
        rate.count += 1
        permitted = permitted and rate.count <= limit
    await db.flush()
    return stamp, permitted


async def request_code(db, settings, email, client):
    email = normalize_email(email, settings)
    if reserved_email(email):
        raise HTTPException(422, "reserved_address")
    await ensure_namespace(db, settings)
    stamp, allowed = await locked_rate(db, settings, email, client, "request")
    if not allowed:
        return "auth_rate_limited"
    address = keyed(settings, "email:" + email)
    challenge = await db.scalar(
        select(EmailChallenge).where(EmailChallenge.address_key == address).with_for_update()
    )
    code, nonce = f"{secrets.randbelow(1000000):06d}", secrets.token_hex(16)
    if challenge is not None and hmac.compare_digest(
        keyed(settings, f"code:{email}:{challenge.nonce}:{code}"), challenge.code_hash
    ):
        # A random collision must not make the previous numeric code valid again.
        code = f"{(int(code) + 1) % 1000000:06d}"
    if challenge is None:
        challenge = EmailChallenge(address_key=address)
        db.add(challenge)
    challenge.nonce = nonce
    challenge.code_hash = keyed(settings, f"code:{email}:{nonce}:{code}")
    challenge.expires_at = stamp + timedelta(minutes=5)
    challenge.attempts, challenge.consumed = 0, False
    await db.flush()
    from app.mail_outbox import otp_intent

    otp_intent(db, settings, challenge, email, code)
    return None


async def verify_code(db, settings, email, code, client):
    email = normalize_email(email, settings)
    if reserved_email(email):
        raise HTTPException(422, "reserved_address")
    await ensure_namespace(db, settings)
    stamp, allowed = await locked_rate(db, settings, email, client, "verify")
    if not allowed:
        return None, "auth_rate_limited"
    challenge = await db.scalar(
        select(EmailChallenge)
        .where(EmailChallenge.address_key == keyed(settings, "email:" + email))
        .with_for_update()
    )
    if (
        not challenge
        or challenge.consumed
        or challenge.expires_at <= stamp
        or challenge.attempts >= 5
    ):
        return None, "otp_invalid"
    challenge.attempts += 1
    expected = keyed(settings, f"code:{email}:{challenge.nonce}:{code}")
    if not hmac.compare_digest(expected, challenge.code_hash):
        return None, "otp_invalid"
    challenge.consumed = True
    user = await db.scalar(
        select(User).where(User.verified_email == email).with_for_update(key_share=True)
    )
    if user is None:
        user = User(verified_email=email, persona=None, role="reporter", display_name="신고자")
        db.add(user)
        await db.flush()
    return user, None


def service_location():
    from app.schemas import LocationView

    return LocationView(
        state="staff_review",
        can_answer=False,
        dictionary_version="service-unregistered-v1",
        source_type="unregistered",
        issues=["service_dictionary_unavailable"],
    ).model_dump()


def update_service_location(db, ticket, principal, payload):
    from app.location_workflow import check_version, refresh_safety, save_location
    from app.tickets import transition

    check_version(ticket, payload.expected_version)
    if payload.action == "confirm":
        raise HTTPException(422, "service_location_unavailable")
    if payload.action == "answer":
        if not payload.answer:
            raise HTTPException(422, "location_answer_required")
        if principal.user.role != "dispatcher" and ticket.location_answer_count >= 2:
            raise HTTPException(409, "location_answer_limit")
        ticket.location_answer_count += 1
        ticket.location_history = ticket.location_history + [
            {"kind": "answer", "text": payload.answer}
        ]
        ticket.location_text = payload.answer
        refresh_safety(ticket)
    if principal.user.role == "dispatcher" and not payload.reason:
        raise HTTPException(422, "reason_required")
    save_location(ticket, service_location())
    transition(
        db, ticket, principal.user, "location_updated", payload.reason or "서비스 위치 설명 갱신"
    )
