"""ID-only durable mail dispatcher. Sending leases are never retried blindly."""

import argparse
import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from arq import create_pool
from arq.worker import Worker
from sqlalchemy import func, or_, select

from app.config import Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.jobs import json_deserialize, json_serialize, redis_settings
from app.mail_outbox import (
    TERMINAL,
    approval_hash,
    decrypt,
    external_notification_suppressed,
    namespace,
)
from app.models import (
    CompletionReport,
    EmailChallenge,
    MailAttempt,
    MailJob,
    Ticket,
    TicketEvent,
    TicketMerge,
    User,
)
from app.services.gmail import gmail_send
from app.services.mail import Outcome, local_save, smtp_send

MAX_ATTEMPTS = 3
LEASE_SECONDS = 180


@dataclass(frozen=True)
class Lease:
    job_id: uuid.UUID
    token: uuid.UUID
    attempt_no: int


def queue_name(settings):
    return f"baroai:{namespace(settings)}:mail"


async def clock(db):
    return await db.scalar(select(func.clock_timestamp()))


async def locked(db, job_id):
    # OTP request/verify transactions acquire the challenge before any mail row.
    ref = await db.get(MailJob, job_id)
    if ref and ref.challenge_key:
        await db.scalar(
            select(EmailChallenge)
            .where(EmailChallenge.address_key == ref.challenge_key)
            .with_for_update()
        )
    return await db.scalar(select(MailJob).where(MailJob.id == job_id).with_for_update())


async def close_attempt(db, job, stamp, state, error):
    if job.attempt_count:
        attempt = await db.scalar(
            select(MailAttempt)
            .where(MailAttempt.job_id == job.id, MailAttempt.attempt_no == job.attempt_count)
            .with_for_update()
        )
        if attempt and attempt.finished_at is None:
            attempt.state, attempt.error_code, attempt.finished_at = state, error, stamp


async def finish(db, job, stamp, state, error=None):
    job.state, job.error_code = state, error
    await close_attempt(db, job, stamp, state, error)
    job.lease_token, job.lease_expires_at = None, None
    if state in TERMINAL:
        job.encrypted_payload, job.finished_at = None, stamp
    elif state == "retry_wait":
        job.next_attempt_at = stamp + timedelta(seconds=10 * job.attempt_count)
        job.last_enqueued_at = None


async def invalid_reason(db, job, settings, stamp):
    if job.namespace != namespace(settings):
        return "superseded", "namespace_mismatch"
    if job.expires_at <= stamp:
        return "expired", "mail_expired"
    if settings.mail_delivery == "disabled" or job.delivery not in {"local", "smtp", "gmail"}:
        return "superseded", "delivery_disabled"
    if job.delivery != settings.mail_delivery:
        return "superseded", "delivery_configuration_changed"
    if job.purpose == "otp":
        challenge = await db.get(EmailChallenge, job.challenge_key)
        if (
            not challenge
            or challenge.nonce != job.challenge_nonce
            or challenge.consumed
            or challenge.attempts >= 5
        ):
            return "superseded", "otp_superseded"
        if challenge.expires_at <= stamp:
            return "expired", "otp_expired"
    elif job.purpose == "information_request":
        from app.auth_service import (
            LOCAL_STAFF_IDS,
            service_account_eligible,
            staff_identity_eligible,
        )
        from app.models import InformationRequest
        from app.staff_auth import staff_principal_eligible

        item = await db.get(InformationRequest, job.information_request_id)
        ticket = await db.get(Ticket, job.ticket_id)
        user = await db.get(User, job.recipient_id)
        requester = await db.get(User, item.requested_by) if item else None
        requester_valid = bool(
            requester
            and requester.role == "dispatcher"
            and (
                service_account_eligible(requester)
                or (
                    staff_identity_eligible(requester, settings)
                    and await staff_principal_eligible(db, requester)
                )
            )
        )
        if (
            not requester_valid
            or not item
            or item.status != "open"
            or item.ticket_id != job.ticket_id
            or item.requested_by in LOCAL_STAFF_IDS
            or not ticket
            or ticket.reporter_id != job.recipient_id
            or not ticket.information_mail_allowed
            or not service_account_eligible(user)
        ):
            return "superseded", "information_request_unavailable"
    elif job.purpose == "approved_reply":
        report = await db.get(CompletionReport, job.report_id)
        if report and await external_notification_suppressed(db, report):
            return "superseded", "local_staff_notification_suppressed"
        ticket = await db.get(Ticket, job.ticket_id)
        user = await db.get(User, job.recipient_id)
        if (
            not report
            or not report.reviewed
            or not report.approved_snapshot
            or not ticket
            or not user
            or ticket.reporter_id != user.id
            or not job.approval_hash
            or job.approval_hash != approval_hash(report)
        ):
            return "superseded", "publication_unavailable"
        if job.merge_id:
            link = await db.get(TicketMerge, job.merge_id)
            if (
                not link
                or not link.active
                or not link.subscribed
                or link.source_id != ticket.id
                or link.target_id != report.ticket_id
            ):
                return "superseded", "subscription_ended"
        elif ticket.id != report.ticket_id:
            return "superseded", "publication_unavailable"
    else:
        return "superseded", "unknown_purpose"
    return None


async def claim(database, settings, job_id):
    async with database.transaction() as db:
        await dispatch_lock(db)
        stamp = await clock(db)
        job = await locked(db, job_id)
        if not job or job.namespace != namespace(settings) or job.state in TERMINAL:
            return None
        if job.state in {"claimed", "sending"}:
            if job.lease_expires_at and job.lease_expires_at > stamp:
                return None
            if job.state == "sending":
                await finish(db, job, stamp, "unknown", "sending_lease_lost")
                return None
            await close_attempt(db, job, stamp, "retry_wait", "claim_lease_lost")
        elif job.next_attempt_at > stamp:
            return None
        invalid = await invalid_reason(db, job, settings, stamp)
        if invalid:
            await finish(db, job, stamp, *invalid)
            return None
        if job.attempt_count >= MAX_ATTEMPTS:
            await finish(db, job, stamp, "failed", "attempts_exhausted")
            return None
        job.attempt_count += 1
        job.state, job.lease_token = "claimed", uuid.uuid4()
        job.lease_expires_at = stamp + timedelta(seconds=LEASE_SECONDS)
        db.add(
            MailAttempt(job_id=job.id, attempt_no=job.attempt_count, lease_token=job.lease_token)
        )
        return Lease(job.id, job.lease_token, job.attempt_count)


async def begin_send(database, settings, lease):
    async with database.transaction() as db:
        await dispatch_lock(db)
        stamp = await clock(db)
        job = await locked(db, lease.job_id)
        if (
            not job
            or job.state != "claimed"
            or job.lease_token != lease.token
            or job.lease_expires_at <= stamp
        ):
            return None
        invalid = await invalid_reason(db, job, settings, stamp)
        if invalid:
            await finish(db, job, stamp, *invalid)
            return None
        try:
            payload = decrypt(settings, job.encrypted_payload)
            if settings.deployment_mode == "service":
                from app.auth_service import (
                    is_service_email,
                    reserved_email,
                    service_account_eligible,
                )

                recipient = await db.get(User, job.recipient_id) if job.recipient_id else None
                if (
                    reserved_email(payload.get("to"))
                    or not is_service_email(payload.get("to"))
                    or (job.recipient_id and not service_account_eligible(recipient))
                ):
                    await finish(db, job, stamp, "superseded", "recipient_domain_disallowed")
                    return None
            if job.recipient_id:
                user = await db.get(User, job.recipient_id)
                if user.verified_email != payload["to"]:
                    await finish(db, job, stamp, "superseded", "recipient_changed")
                    return None
        except Exception:
            await finish(db, job, stamp, "failed", "payload_unavailable")
            return None
        job.state = "sending"
        attempt = await db.scalar(
            select(MailAttempt).where(
                MailAttempt.job_id == job.id, MailAttempt.attempt_no == lease.attempt_no
            )
        )
        attempt.state = "sending"
        return payload, job.delivery


async def complete(database, lease, outcome):
    async with database.transaction() as db:
        await dispatch_lock(db)
        stamp = await clock(db)
        job = await locked(db, lease.job_id)
        if (
            not job
            or job.state != "sending"
            or job.lease_token != lease.token
            or job.lease_expires_at <= stamp
        ):
            return False
        state, error = outcome.state, outcome.error_code
        if state == "retry_wait" and job.attempt_count >= MAX_ATTEMPTS:
            state, error = "failed", "attempts_exhausted"
        if state == "retry_wait" and job.expires_at <= stamp:
            state, error = "expired", "mail_expired"
        await finish(db, job, stamp, state, error)
        if (
            state == "accepted"
            and job.purpose == "information_request"
            and job.delivery in {"smtp", "gmail"}
        ):
            db.add(
                TicketEvent(
                    ticket_id=job.ticket_id,
                    actor_id=None,
                    event_type="information_mail_accepted",
                    reason="추가 정보 안내 메일 제공자 접수",
                )
            )
        return True


async def process_mail(database, settings, job_id, *, transport=None):
    lease = await claim(database, settings, uuid.UUID(str(job_id)))
    if not lease:
        return
    prepared = await begin_send(database, settings, lease)
    if not prepared:
        return
    payload, delivery = prepared
    sender = transport or {"local": local_save, "smtp": smtp_send, "gmail": gmail_send}[delivery]
    # No session or advisory lock is held while this thread performs I/O.
    try:
        if delivery == "gmail" and transport is None:
            cancelled = threading.Event()
            try:
                outcome = await asyncio.to_thread(
                    sender,
                    settings,
                    lease.job_id,
                    payload,
                    deadline=time.monotonic() + 120,
                    cancelled=cancelled,
                )
            except asyncio.CancelledError:
                cancelled.set()
                raise
        else:
            outcome = await asyncio.to_thread(sender, settings, lease.job_id, payload)
    except Exception:
        outcome = Outcome("unknown", "transport_result_unknown")
    await complete(database, lease, outcome)


async def send_mail(ctx, job_id):
    await process_mail(ctx["database"], ctx["settings"], job_id)


async def dispatch_once(database, redis, *, settings, job_ids=None):
    async with database.transaction() as db:
        await dispatch_lock(db)
        stamp = await clock(db)
        query = (
            select(MailJob.id)
            .where(
                MailJob.namespace == namespace(settings),
                or_(
                    (
                        MailJob.state.in_(["queued", "retry_wait"])
                        & (MailJob.next_attempt_at <= stamp)
                    ),
                    (
                        MailJob.state.in_(["claimed", "sending"])
                        & (MailJob.lease_expires_at <= stamp)
                    ),
                ),
                or_(
                    MailJob.last_enqueued_at.is_(None),
                    MailJob.last_enqueued_at < stamp - timedelta(seconds=10),
                ),
            )
            .order_by(MailJob.created_at)
            .limit(100)
        )
        if job_ids is not None:
            query = query.where(MailJob.id.in_(job_ids))
        ids = list(await db.scalars(query))
    count = 0
    for job_id in ids:
        queued = await redis.enqueue_job(
            "send_mail", str(job_id), _queue_name=queue_name(settings), _job_id=str(uuid.uuid4())
        )
        if queued:
            count += 1
            async with database.transaction() as db:
                await dispatch_lock(db)
                job = await locked(db, job_id)
                if job and job.state not in TERMINAL:
                    job.last_enqueued_at = await clock(db)
    return count


def make_worker(settings, *, burst=False):
    async def startup(ctx):
        ctx["settings"], ctx["database"] = settings, Database(settings)

    async def shutdown(ctx):
        await ctx["database"].close()

    return Worker(
        functions=[send_mail],
        redis_settings=redis_settings(settings),
        queue_name=queue_name(settings),
        on_startup=startup,
        on_shutdown=shutdown,
        job_serializer=json_serialize,
        job_deserializer=json_deserialize,
        retry_jobs=False,
        max_tries=1,
        keep_result=0,
        job_timeout=170,
        max_jobs=4,
        burst=burst,
        log_results=False,
    )


async def dispatch_loop(settings, *, once=False):
    database = Database(settings)
    try:
        while True:
            redis = None
            try:
                redis = await create_pool(
                    redis_settings(settings),
                    job_serializer=json_serialize,
                    job_deserializer=json_deserialize,
                )
                await dispatch_once(database, redis, settings=settings)
            except Exception:
                logging.getLogger("baroai.mail").error("mail_dispatch_unavailable")
                if once:
                    raise RuntimeError("mail_dispatch_unavailable") from None
            finally:
                if redis:
                    await redis.aclose()
            if once:
                return
            await asyncio.sleep(2)
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["dispatch", "worker"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--burst", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.command == "worker":
            make_worker(settings, burst=args.burst).run()
        else:
            asyncio.run(dispatch_loop(settings, once=args.once))
    except Exception:
        raise SystemExit("mail_runtime_unavailable") from None


if __name__ == "__main__":
    main()
