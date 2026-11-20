"""Explicit, bounded cleanup for the dedicated public demonstration database."""

import argparse
import asyncio
import base64
import json
import sys
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select

from app.config import Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.models import (
    Assignment,
    AssignmentCredentialReceipt,
    AuthSession,
    Base,
    CompletionReport,
    IdempotencyRecord,
    MailJob,
    ProcessingJob,
    Ticket,
    TicketEvent,
    User,
)
from app.public_staff import require_existing_marker
from app.security import now


async def cleanup_one(db, ticket, *, dry_run):
    """Remove a supported ticket graph, refusing any unsupported relation."""
    assignments = list(
        (await db.scalars(select(Assignment.id).where(Assignment.ticket_id == ticket.id))).all()
    )
    reports = list(
        (
            await db.scalars(
                select(CompletionReport.id).where(CompletionReport.ticket_id == ticket.id)
            )
        ).all()
    )
    if await db.scalar(
        select(CompletionReport.id)
        .where(
            CompletionReport.ticket_id == ticket.id,
            CompletionReport.assignment_id.not_in(assignments),
        )
        .limit(1)
    ):
        raise ValueError("cross_ticket_reference")
    if assignments and await db.scalar(
        select(CompletionReport.id)
        .where(
            CompletionReport.assignment_id.in_(assignments),
            CompletionReport.ticket_id != ticket.id,
        )
        .limit(1)
    ):
        raise ValueError("cross_ticket_reference")
    for model in (ProcessingJob, MailJob):
        if reports and await db.scalar(
            select(model.id)
            .where(model.report_id.in_(reports), model.ticket_id.is_distinct_from(ticket.id))
            .limit(1)
        ):
            raise ValueError("cross_ticket_reference")
        if await db.scalar(
            select(model.id)
            .where(
                model.ticket_id == ticket.id,
                model.report_id.is_not(None),
                model.report_id.not_in(reports),
            )
            .limit(1)
        ):
            raise ValueError("cross_ticket_reference")
    mail_ids = list(
        (
            await db.scalars(
                select(MailJob.id).where(
                    or_(MailJob.ticket_id == ticket.id, MailJob.report_id.in_(reports))
                )
            )
        ).all()
    )
    if mail_ids and await db.scalar(
        select(MailJob.id)
        .where(MailJob.recovery_parent_id.in_(mail_ids), MailJob.id.not_in(mail_ids))
        .limit(1)
    ):
        raise ValueError("cross_ticket_reference")
    targets = {"tickets": [ticket.id], "assignments": assignments, "completion_reports": reports}
    supported = {
        "tickets": {
            "ticket_events",
            "assignments",
            "completion_reports",
            "processing_jobs",
            "mail_jobs",
        },
        "assignments": {"assignments", "completion_reports", "assignment_credential_receipts"},
        "completion_reports": {"completion_reports", "processing_jobs", "mail_jobs"},
    }
    # Unknown FK relations, including legacy photo and merge/plan state, are never deleted.
    for table in Base.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                target = fk.column.table.name
                ids = targets.get(target, [])
                if not ids or table.name in supported[target]:
                    continue
                if (
                    await db.scalar(select(table.c[column.name]).where(column.in_(ids)).limit(1))
                    is not None
                ):
                    raise ValueError("unsupported_related_data")
    # Self-references must remain entirely within this ticket graph.
    for model, column, ids in (
        (Assignment, Assignment.predecessor_id, assignments),
        (CompletionReport, CompletionReport.supersedes_report_id, reports),
    ):
        if ids and await db.scalar(
            select(model.id).where(column.in_(ids), model.id.not_in(ids)).limit(1)
        ):
            raise ValueError("cross_ticket_reference")
    records = list((await db.scalars(select(IdempotencyRecord))).all())
    matching = []
    ids = {
        str(ticket.id),
        ticket.id.hex,
        *(str(i) for i in assignments),
        *(str(i) for i in reports),
    }
    for row in records:
        parts = row.scope.split(":")
        scoped = any(part in ids for part in parts[1:-1])
        malformed_may_belong = scoped or (
            len(parts) >= 3 and parts[0] == "create" and parts[1] == str(ticket.reporter_id)
        )
        try:
            value = json.loads(row.response_json)
        except (ValueError, TypeError):
            if malformed_may_belong:
                raise ValueError("invalid_receipt") from None
            continue
        if not isinstance(value, dict):
            if malformed_may_belong:
                raise ValueError("invalid_receipt")
            continue
        nested = value.get("ticket")
        response_ticket = value.get("ticket_id") or (
            nested.get("id") if isinstance(nested, dict) else value.get("id")
        )
        if scoped or response_ticket == str(ticket.id):
            matching.append(row.id)
    if dry_run:
        return
    if matching:
        await db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.id.in_(matching)))
    await db.execute(
        delete(MailJob).where(or_(MailJob.ticket_id == ticket.id, MailJob.report_id.in_(reports)))
    )
    await db.execute(
        delete(ProcessingJob).where(
            or_(ProcessingJob.ticket_id == ticket.id, ProcessingJob.report_id.in_(reports))
        )
    )
    await db.execute(
        delete(AssignmentCredentialReceipt).where(
            AssignmentCredentialReceipt.assignment_id.in_(assignments)
        )
    )
    await db.execute(delete(TicketEvent).where(TicketEvent.ticket_id == ticket.id))
    await db.execute(delete(CompletionReport).where(CompletionReport.id.in_(reports)))
    await db.execute(delete(Assignment).where(Assignment.id.in_(assignments)))
    await db.delete(ticket)
    await db.flush()
    # Preserve any user/session with a remaining ticket or other FK reference.
    user = await db.get(User, ticket.reporter_id)
    if (
        user
        and user.role == "reporter"
        and not await db.scalar(select(Ticket.id).where(Ticket.reporter_id == user.id).limit(1))
    ):
        sessions = list(
            (await db.scalars(select(AuthSession).where(AuthSession.user_id == user.id))).all()
        )
        if all(s.revoked or s.expires_at <= now() for s in sessions):
            references = False
            for table in Base.metadata.tables.values():
                if table.name in {"tickets", "auth_sessions"}:
                    continue
                for column in table.columns:
                    if any(fk.column.table.name == "users" for fk in column.foreign_keys):
                        if (
                            await db.scalar(select(column).where(column == user.id).limit(1))
                            is not None
                        ):
                            references = True
            if not references:
                for session in sessions:
                    await db.delete(session)
                await db.flush()
                await db.delete(user)


def encode_cursor(stamp, ticket_id):
    return base64.urlsafe_b64encode(f"{stamp.isoformat()}|{ticket_id}".encode()).decode()


def decode_cursor(cursor):
    try:
        if len(cursor) > 160:
            raise ValueError
        stamp, ticket_id = (
            base64.b64decode(cursor, altchars=b"-_", validate=True).decode().split("|")
        )
        stamp = datetime.fromisoformat(stamp)
        if stamp.tzinfo is None:
            raise ValueError
        return stamp, uuid.UUID(ticket_id)
    except (ValueError, UnicodeError):
        raise ValueError("invalid_cursor") from None


async def run(database, settings, *, dry_run=True, batch_size=20, after=None, scan_limit=1000):
    if not 1 <= batch_size <= 100 or not 1 <= scan_limit <= 1000:
        raise ValueError("invalid_batch_size")
    cursor = decode_cursor(after) if after else None
    async with database.transaction() as db:
        await dispatch_lock(db)
        await require_existing_marker(db, settings)
        query = select(Ticket.created_at, Ticket.id).where(
            Ticket.created_at <= now() - timedelta(days=7)
        )
        if cursor:
            stamp, ticket_id = cursor
            query = query.where(
                or_(
                    Ticket.created_at > stamp,
                    (Ticket.created_at == stamp) & (Ticket.id > ticket_id),
                )
            )
        rows = list(
            (await db.execute(query.order_by(Ticket.created_at, Ticket.id).limit(scan_limit))).all()
        )
    succeeded = failed = 0
    reasons = {}
    last = None
    examined = 0
    for stamp, ticket_id in rows:
        if succeeded >= batch_size:
            break
        last = (stamp, ticket_id)
        examined += 1
        try:
            async with database.transaction() as db:
                await dispatch_lock(db)
                await require_existing_marker(db, settings)
                ticket = await db.get(Ticket, ticket_id, with_for_update=True)
                if ticket is None or ticket.created_at > now() - timedelta(days=7):
                    continue
                await cleanup_one(db, ticket, dry_run=dry_run)
            succeeded += 1
        except Exception as exc:
            failed += 1
            reason = (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc)
                in {"unsupported_related_data", "cross_ticket_reference", "invalid_receipt"}
                else "cleanup_error"
            )
            reasons[reason] = reasons.get(reason, 0) + 1
    next_cursor = (
        encode_cursor(*last) if last and (examined < len(rows) or len(rows) == scan_limit) else None
    )
    return {
        "dry_run": dry_run,
        "examined": examined,
        "succeeded": succeeded,
        "failed": failed,
        "reasons": reasons,
        "next_cursor": next_cursor,
        "complete_pass": next_cursor is None,
    }


def main():
    parser = argparse.ArgumentParser(description="Public demo retention operator")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--after", help="next_cursor from the previous invocation")
    args = parser.parse_args()

    async def execute():
        settings = Settings()
        database = Database(settings)
        try:
            return await run(
                database,
                settings,
                dry_run=not args.apply,
                batch_size=args.batch_size,
                after=args.after,
            )
        finally:
            await database.close()

    try:
        result = asyncio.run(execute())
    except Exception:
        print("public retention refused", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
