"""Explicit initialization of a previously isolated prototype namespace."""

import argparse
import asyncio
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.auth_service import (
    PROTOTYPE_DISPATCHER_ID,
    PROTOTYPE_EMAIL,
    PROTOTYPE_PERSONA,
    ensure_namespace,
    prototype_dispatcher_eligible,
)
from app.config import Settings
from app.db import Database
from app.models import Assignment, CompletionReport, Ticket, User

SAMPLE_REPORTER_ID = uuid.UUID("00000000-0000-4000-8000-000000000101")
SAMPLE_TICKET_ID = uuid.UUID("00000000-0000-4000-8000-000000000102")
SAMPLE_ASSIGNMENT_ID = uuid.UUID("00000000-0000-4000-8000-000000000103")
SAMPLE_REPORT_ID = uuid.UUID("00000000-0000-4000-8000-000000000104")


def sample_identity(key):
    if key is None:
        return SAMPLE_TICKET_ID, SAMPLE_ASSIGNMENT_ID, SAMPLE_REPORT_ID, "B-PROTOTYPE01"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", key):
        raise ValueError("prototype_sample_key_invalid")
    namespace = uuid.UUID("e8a49bb8-d36a-48fd-88d8-a2f8cf802a13")
    ids = tuple(
        uuid.uuid5(namespace, f"{key}:{kind}") for kind in ("ticket", "assignment", "report")
    )
    public_no = "B-P" + hashlib.sha256(key.encode()).hexdigest()[:16].upper()
    return (*ids, public_no)


async def initialize(settings, *, with_sample=False, sample_key=None):
    if settings.deployment_mode != "prototype":
        raise ValueError("prototype_mode_required")
    if sample_key is not None and not with_sample:
        raise ValueError("prototype_sample_key_requires_sample")
    identity = sample_identity(sample_key)
    database = Database(settings)
    try:
        async with database.transaction() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(7294123001)"))
            await ensure_namespace(db, settings)
            user = await db.get(User, PROTOTYPE_DISPATCHER_ID)
            if user is None:
                conflict = await db.scalar(
                    select(User.id).where(
                        (User.verified_email == PROTOTYPE_EMAIL)
                        | (User.persona == PROTOTYPE_PERSONA)
                    )
                )
                if conflict is not None:
                    raise ValueError("prototype_identity_conflict")
                db.add(
                    User(
                        id=PROTOTYPE_DISPATCHER_ID,
                        verified_email=PROTOTYPE_EMAIL,
                        persona=PROTOTYPE_PERSONA,
                        role="dispatcher",
                        display_name="합성 접수 담당자",
                    )
                )
            elif not prototype_dispatcher_eligible(user):
                raise ValueError("prototype_identity_conflict")
            for persona, name in (("worker_a", "합성 작업자 가"), ("worker_b", "합성 작업자 나")):
                worker = await db.scalar(select(User).where(User.persona == persona))
                if worker is None:
                    db.add(User(persona=persona, role="worker", display_name=name))
                elif worker.role != "worker" or worker.verified_email is not None:
                    raise ValueError("prototype_identity_conflict")
            if with_sample:
                await db.flush()
                await ensure_sample(db, identity)
    finally:
        await database.close()


async def ensure_sample(db, identity):
    ticket_id, assignment_id, report_id, public_no = identity
    reporter = await db.get(User, SAMPLE_REPORTER_ID)
    ticket = await db.get(Ticket, ticket_id)
    assignment = await db.get(Assignment, assignment_id)
    report = await db.get(CompletionReport, report_id)
    worker = await db.scalar(select(User).where(User.persona == "worker_a"))
    if reporter is not None and not (
        reporter.persona == "prototype_sample_reporter"
        and reporter.role == "reporter"
        and reporter.verified_email is None
    ):
        raise ValueError("prototype_sample_conflict")
    existing = (ticket, assignment, report)
    if any(existing):
        if (
            not reporter
            or not all(existing)
            or not (
                reporter.persona == "prototype_sample_reporter"
                and reporter.role == "reporter"
                and reporter.verified_email is None
                and ticket.reporter_id == reporter.id
                and ticket.public_no == public_no
                and assignment.ticket_id == ticket.id
                and assignment.worker_id == worker.id
                and report.ticket_id == ticket.id
                and report.assignment_id == assignment.id
            )
        ):
            raise ValueError("prototype_sample_conflict")
        return
    if reporter is None and await db.scalar(
        select(User.id).where(User.persona == "prototype_sample_reporter")
    ):
        raise ValueError("prototype_sample_conflict")
    if await db.scalar(select(Ticket.id).where(Ticket.public_no == public_no)):
        raise ValueError("prototype_sample_conflict")
    if reporter is None:
        db.add(
            User(
                id=SAMPLE_REPORTER_ID,
                persona="prototype_sample_reporter",
                role="reporter",
                display_name="합성 신고자",
            )
        )
        await db.flush()
    db.add(
        Ticket(
            id=ticket_id,
            reporter_id=SAMPLE_REPORTER_ID,
            public_no=public_no,
            description="[합성 사례] 복도 조명이 켜지지 않아요.",
            location_text="합성 건물 2층 복도",
            status="in_progress",
        )
    )
    await db.flush()
    db.add(
        Assignment(
            id=assignment_id,
            ticket_id=ticket_id,
            worker_id=worker.id,
            token_hash=hashlib.sha256(f"prototype-sample:{assignment_id}".encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=3650),
            started_at=datetime.now(UTC),
            execution_revision=1,
        )
    )
    await db.flush()
    structure = {
        "actions": ["합성 조명 점검 및 교체"],
        "materials_used": [],
        "residual_issue": "",
        "revisit_needed": False,
        "photo_comparison": "not_performed",
        "source": "manual",
    }
    db.add(
        CompletionReport(
            id=report_id,
            ticket_id=ticket_id,
            assignment_id=assignment_id,
            result="completed",
            text="[합성 보고] 조명을 점검하고 교체했어요.",
            revision=1,
            analysis_status="manual",
            confirmed_data=structure,
            worker_confirmed_revision=1,
            confirmed_result="completed",
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-sample", action="store_true", help="Add one isolated synthetic case"
    )
    parser.add_argument(
        "--sample-key", help="Stable lowercase slug for an additional synthetic case"
    )
    args = parser.parse_args()
    asyncio.run(initialize(Settings(), with_sample=args.with_sample, sample_key=args.sample_key))
    print("Prototype synthetic initialization complete.")
