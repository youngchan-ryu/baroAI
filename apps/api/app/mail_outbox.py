"""Atomic encrypted intents and role-safe delivery summaries."""

import hashlib
import json
from datetime import timedelta

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import Assignment, MailJob, Ticket, TicketMerge, User

TERMINAL = {"local_saved", "accepted", "unknown", "failed", "expired", "superseded"}
TEMPLATE_VERSION = "mail-v1"


def namespace(settings):
    return f"{settings.deployment_mode}:{settings.app_env}"


def approval_hash(report):
    """Identify the approved publication, without storing its contents in the ledger."""
    value = {
        "report_id": str(report.id),
        "ticket_id": str(report.ticket_id),
        "approved_at": report.approved_at.isoformat() if report.approved_at else None,
        "approved_by": str(report.approved_by) if report.approved_by else None,
        "snapshot": report.approved_snapshot,
        "reply_text": report.reply_text,
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def encrypt(settings, payload):
    return (
        Fernet(settings.mail_encryption_key.get_secret_value().encode())
        .encrypt(json.dumps(payload, ensure_ascii=False).encode())
        .decode()
    )


def decrypt(settings, ciphertext):
    return json.loads(
        Fernet(settings.mail_encryption_key.get_secret_value().encode()).decrypt(
            ciphertext.encode()
        )
    )


def otp_intent(db, settings, challenge, email, code):
    payload = {
        "to": email,
        "code": code,
        "expires_at": challenge.expires_at.isoformat(),
        "subject": "바로AI 이메일 인증",
        "body": (
            f"인증 코드: {code}\n"
            "5분 안에 가장 최근 요청한 코드만 입력해 주세요.\n"
            "요청하지 않았다면 무시해 주세요."
        ),
    }
    job = MailJob(
        purpose="otp",
        namespace=namespace(settings),
        delivery=settings.mail_delivery,
        template_version=TEMPLATE_VERSION,
        dedupe_key=f"otp:{challenge.address_key}:{challenge.nonce}",
        encrypted_payload=encrypt(settings, payload),
        challenge_key=challenge.address_key,
        challenge_nonce=challenge.nonce,
        expires_at=challenge.expires_at,
    )
    db.add(job)
    return job


async def external_notification_suppressed(db, report):
    from app.auth_service import LOCAL_STAFF_IDS

    if (
        report.approved_snapshot
        and report.approved_snapshot.get("external_notification_suppressed") is True
    ):
        return True
    if report.approved_by in LOCAL_STAFF_IDS:
        return True
    assignment = await db.get(Assignment, report.assignment_id) if report.assignment_id else None
    return bool(assignment and assignment.worker_id in LOCAL_STAFF_IDS)


async def reply_intents(db, settings, ticket, report):
    # Demo identities have no verified mailbox. Old approvals are never scanned.
    if settings.deployment_mode != "service" or settings.mail_delivery == "disabled":
        return
    if await external_notification_suppressed(db, report):
        return
    recipients = [(ticket, None)]
    links = (
        await db.scalars(
            select(TicketMerge).where(
                TicketMerge.target_id == ticket.id,
                TicketMerge.active.is_(True),
                TicketMerge.subscribed.is_(True),
            )
        )
    ).all()
    for link in links:
        source = await db.get(Ticket, link.source_id)
        if source:
            recipients.append((source, link))
    for own_ticket, link in recipients:
        user = await db.get(User, own_ticket.reporter_id)
        from app.auth_service import service_account_eligible

        if not service_account_eligible(user):
            continue
        payload = {
            "to": user.verified_email,
            "subject": "바로AI 접수 업데이트 안내",
            "body": "접수한 신고에 업데이트가 있어요. 로그인 후 본인 접수 내용을 확인해 주세요.\n"
            + settings.mail_link_origin.rstrip("/")
            + f"/t/{own_ticket.id}",
        }
        db.add(
            MailJob(
                purpose="approved_reply",
                namespace=namespace(settings),
                delivery=settings.mail_delivery,
                template_version=TEMPLATE_VERSION,
                dedupe_key=f"reply:{report.id}:{report.revision}:{own_ticket.id}:{user.id}",
                encrypted_payload=encrypt(settings, payload),
                ticket_id=own_ticket.id,
                report_id=report.id,
                approval_hash=approval_hash(report),
                recipient_id=user.id,
                merge_id=link.id if link else None,
                expires_at=report.approved_at + timedelta(hours=24),
            )
        )


async def delivery_status(db, *, report_id=None, ticket_id=None, recipient_id=None, merge_id=None):
    query = select(MailJob).where(MailJob.purpose == "approved_reply")
    if report_id:
        query = query.where(MailJob.report_id == report_id)
    if ticket_id:
        query = query.where(MailJob.ticket_id == ticket_id)
    if merge_id:
        query = query.where(MailJob.merge_id == merge_id)
    if recipient_id:
        query = query.where(MailJob.recipient_id == recipient_id)
    job = await db.scalar(query.order_by(MailJob.created_at.desc()).limit(1))
    return job.state if job else "not_sent"
