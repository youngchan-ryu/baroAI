"""Approved one-level merges. Ownership and private object scopes never expand."""

import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models import (
    Assignment,
    CompletionReport,
    DuplicateDismissal,
    Ticket,
    TicketEvent,
    TicketMerge,
    User,
)
from app.reporting import REPLIES
from app.schemas import DuplicateCandidate, DuplicateCandidates, MergedProgress, MergeHistoryView
from app.security import assignment_current, current_assignment_clause, now

OPEN = {"received", "confirmed", "assigned", "in_progress"}


def aggregate_safety(ticket):
    own_auto = ticket.safety_data.get("automatic_urgency", 3)
    own_effective = ticket.effective_urgency
    floor = ticket.linked_safety_floor
    return {
        "own_automatic_urgency": own_auto,
        "own_effective_urgency": own_effective,
        "automatic_urgency": min(own_auto, floor),
        "effective_urgency": min(own_effective, floor),
        "linked_rule_floor": floor,
        "linked_requires_review": ticket.linked_safety_requires_review,
        "emergency_banner": bool(ticket.safety_data.get("emergency_banner") or floor <= 1),
    }


async def merge_version(db, ticket):
    assignments = (
        await db.execute(
            select(
                Assignment.id, Assignment.revoked, Assignment.auth_epoch, User.role, User.auth_epoch
            )
            .join(User, User.id == Assignment.worker_id)
            .where(Assignment.ticket_id == ticket.id)
            .order_by(Assignment.id)
        )
    ).all()
    reports = (
        await db.execute(
            select(CompletionReport.id, CompletionReport.revision, CompletionReport.reviewed)
            .where(CompletionReport.ticket_id == ticket.id)
            .order_by(CompletionReport.id)
        )
    ).all()
    links = (
        await db.execute(
            select(TicketMerge.id, TicketMerge.revision, TicketMerge.active)
            .where(or_(TicketMerge.source_id == ticket.id, TicketMerge.target_id == ticket.id))
            .order_by(TicketMerge.id)
        )
    ).all()
    snapshot = [
        ticket.analysis_revision,
        ticket.location_version,
        ticket.safety_version,
        ticket.status,
        ticket.category_code,
        ticket.linked_safety_floor,
        ticket.linked_safety_requires_review,
        [list(row) for row in assignments],
        [list(row) for row in reports],
        [list(row) for row in links],
    ]
    return hashlib.sha256(
        json.dumps(snapshot, default=str, separators=(",", ":")).encode()
    ).hexdigest()


async def view_fields(db, ticket, principal):
    version = await merge_version(db, ticket)
    dispatcher = principal and not principal.assignment and principal.user.role == "dispatcher"
    links = list(
        (
            await db.scalars(
                select(TicketMerge)
                .where(or_(TicketMerge.source_id == ticket.id, TicketMerge.target_id == ticket.id))
                .order_by(TicketMerge.created_at, TicketMerge.id)
            )
        ).all()
    )
    history = (
        [
            MergeHistoryView(
                id=m.id,
                linked_floor=m.safety_snapshot.get("floor", 3),
                source_automatic_urgency=m.safety_snapshot.get("automatic_urgency"),
                source_effective_urgency=m.safety_snapshot.get("effective_urgency"),
                source_rules_version=m.safety_snapshot.get("rules_version"),
                source_id=m.source_id,
                target_id=m.target_id,
                source_previous_status=m.previous_status,
                active=m.active,
                revision=m.revision,
                subscribed=m.subscribed,
                created_at=m.created_at,
                cancelled_at=m.cancelled_at,
                reassignment_required=not m.active
                and m.previous_status in {"assigned", "in_progress"},
            )
            for m in links
        ]
        if dispatcher
        else []
    )
    progress = None
    if principal and not principal.assignment and ticket.reporter_id == principal.user.id:
        link = next((m for m in links if m.active and m.source_id == ticket.id), None)
        if link:
            progress = MergedProgress(revision=link.revision, subscribed=link.subscribed)
            if link.subscribed:
                from app.mail_outbox import delivery_status

                target = await db.get(Ticket, link.target_id)
                if target:
                    report = await db.scalar(
                        select(CompletionReport)
                        .where(
                            CompletionReport.ticket_id == target.id,
                            CompletionReport.reviewed.is_(True),
                        )
                        .order_by(
                            CompletionReport.approved_at.desc().nullslast(),
                            CompletionReport.created_at.desc(),
                        )
                        .limit(1)
                    )
                    message = None
                    if report and report.approved_snapshot:
                        progress.delivery_status = await delivery_status(
                            db,
                            ticket_id=ticket.id,
                            recipient_id=principal.user.id,
                            report_id=report.id,
                            merge_id=link.id,
                        )
                        result = report.approved_snapshot.get("confirmed_result")
                        message = (
                            "연결된 작업의 완료 보고를 담당자가 확인했어요. "
                            "개별 신고의 수리 보증은 아니에요."
                            if result == "completed"
                            else REPLIES.get(result)
                        )
                    progress.target_status = (
                        target.status
                        if target.status
                        in OPEN | {"completed", "transferred", "cancelled", "reopened", "rejected"}
                        else "received"
                    )
                    progress.message = message
    return {"merge_version": version, "merge_history": history, "merged_progress": progress}


def matching(source, target, stamp):
    if source.id == target.id or source.status not in OPEN or target.status not in OPEN:
        return None
    if min(source.created_at, target.created_at) < stamp - timedelta(hours=72):
        return None
    if source.category_code == "UNKNOWN" or source.category_code != target.category_code:
        return None
    if (
        source.location_data.get("state") != "confirmed"
        or target.location_data.get("state") != "confirmed"
    ):
        return None
    left, right = source.location_data.get("selected"), target.location_data.get("selected")
    if not left or not right or left["building_code"] != right["building_code"]:
        return None
    from app.services.location import registered

    if not registered(left["code"]) or not registered(right["code"]):
        return None
    same_space = left.get("space_code") and left["code"] == right["code"]
    same_floor = left.get("floor") and left.get("floor") == right.get("floor")
    if same_space:
        return (
            "same_space_24h"
            if abs((source.created_at - target.created_at).total_seconds()) <= 86400
            else "same_space"
        )
    return "same_floor" if same_floor else None


async def candidates(db, source):
    source_version = await merge_version(db, source)
    links = list((await db.scalars(select(TicketMerge).where(TicketMerge.active.is_(True)))).all())
    if any(m.target_id == source.id or m.source_id == source.id for m in links):
        return DuplicateCandidates(source_version=source_version, candidates=[])
    targets = list(
        (
            await db.scalars(
                select(Ticket)
                .where(
                    Ticket.status.in_(OPEN),
                    Ticket.id != source.id,
                    Ticket.created_at >= now() - timedelta(hours=72),
                )
                .order_by(Ticket.created_at, Ticket.id)
            )
        ).all()
    )
    result = []
    for target in targets:
        evidence = matching(source, target, now())
        if not evidence or any(m.source_id == target.id for m in links):
            continue
        version = await merge_version(db, target)
        dismissed = await db.scalar(
            select(DuplicateDismissal.id)
            .where(
                or_(
                    (DuplicateDismissal.source_id == source.id)
                    & (DuplicateDismissal.target_id == target.id)
                    & (DuplicateDismissal.source_version == source_version)
                    & (DuplicateDismissal.target_version == version),
                    (DuplicateDismissal.source_id == target.id)
                    & (DuplicateDismissal.target_id == source.id)
                    & (DuplicateDismissal.source_version == version)
                    & (DuplicateDismissal.target_version == source_version),
                )
            )
            .limit(1)
        )
        if dismissed:
            continue
        risk = aggregate_safety(target)
        active = await db.scalar(
            select(Assignment.id)
            .join(User, User.id == Assignment.worker_id)
            .where(Assignment.ticket_id == target.id, current_assignment_clause())
        )
        pending = await db.scalar(
            select(CompletionReport.id)
            .where(
                CompletionReport.ticket_id == target.id,
                CompletionReport.reviewed.is_(False),
                CompletionReport.superseded_at.is_(None),
                CompletionReport.analysis_status != "dismissed",
            )
            .limit(1)
        )
        result.append(
            DuplicateCandidate(
                target_id=target.id,
                public_no=target.public_no,
                status=target.status,
                category_code=target.category_code,
                location_label=target.location_data["selected"]["label"],
                automatic_urgency=risk["automatic_urgency"],
                effective_urgency=risk["effective_urgency"],
                merge_version=version,
                evidence=evidence,
                has_active_assignment=bool(active),
                has_pending_report=bool(pending),
            )
        )
    return DuplicateCandidates(source_version=source_version, candidates=result)


async def lock_pair(db, source_id, target_id):
    if source_id == target_id:
        raise HTTPException(422, "merge_same_ticket")
    records = list(
        (
            await db.scalars(
                select(Ticket)
                .where(Ticket.id.in_([source_id, target_id]))
                .order_by(Ticket.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(records) != 2:
        raise HTTPException(404, "ticket_not_found")
    assignments = list(
        (
            await db.scalars(
                select(Assignment)
                .where(Assignment.ticket_id.in_([source_id, target_id]))
                .order_by(Assignment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    reports = list(
        (
            await db.scalars(
                select(CompletionReport)
                .where(CompletionReport.ticket_id.in_([source_id, target_id]))
                .order_by(CompletionReport.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    links = list(
        (
            await db.scalars(
                select(TicketMerge)
                .where(
                    or_(
                        TicketMerge.source_id.in_([source_id, target_id]),
                        TicketMerge.target_id.in_([source_id, target_id]),
                    )
                )
                .order_by(TicketMerge.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    by_id = {t.id: t for t in records}
    return by_id[source_id], by_id[target_id], assignments, reports, links


async def check_versions(db, source, target, payload):
    if (
        await merge_version(db, source) != payload.expected_source_version
        or await merge_version(db, target) != payload.expected_target_version
    ):
        raise HTTPException(409, "merge_version_conflict")


def event(db, ticket, principal, kind, reason):
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=principal.user.id,
            event_type=kind,
            reason=reason,
            from_status=ticket.status,
            to_status=ticket.status,
        )
    )


async def merge(db, source, target, assignments, reports, links, principal, payload):
    from app.tickets import transition

    await check_versions(db, source, target, payload)
    if any(
        a.ticket_id == source.id
        and a.active_plan_id
        and not a.visit_ended_at
        and (not a.revoked or a.stopped_at)
        for a in assignments
    ):
        raise HTTPException(409, "plan_partial_merge_forbidden")
    if source.status not in OPEN or target.status not in OPEN:
        raise HTTPException(409, "merge_not_open")
    if any(
        m.active and (m.source_id in {source.id, target.id} or m.target_id == source.id)
        for m in links
    ):
        raise HTTPException(409, "merge_chain_forbidden")
    if not matching(source, target, now()):
        raise HTTPException(409, "merge_candidate_changed")
    if any(
        r.ticket_id == source.id and not r.reviewed and r.analysis_status != "dismissed"
        for r in reports
    ):
        raise HTTPException(409, "merge_source_report_pending")
    active = []
    for assignment in assignments:
        if assignment.ticket_id == source.id and assignment_current(
            assignment, await db.get(User, assignment.worker_id)
        ):
            active.append(assignment)
    if (active or source.status in {"assigned", "in_progress"}) and not payload.stop_source_work:
        raise HTTPException(409, "merge_source_stop_required")
    source_risk = aggregate_safety(source)
    floor = min(source_risk["automatic_urgency"], source_risk["effective_urgency"])
    link = TicketMerge(
        source_id=source.id,
        target_id=target.id,
        previous_status=source.status,
        approved_by=principal.user.id,
        reason=payload.reason,
        source_version=payload.expected_source_version,
        target_version=payload.expected_target_version,
        safety_snapshot={
            "automatic_urgency": source_risk["automatic_urgency"],
            "effective_urgency": source_risk["effective_urgency"],
            "own_automatic_urgency": source_risk["own_automatic_urgency"],
            "own_effective_urgency": source_risk["own_effective_urgency"],
            "inherited_linked_floor": source.linked_safety_floor,
            "floor": floor,
            "source_text_hash": source.safety_data.get("source_text_hash"),
            "rules_version": source.safety_data.get("rules_version"),
        },
    )
    db.add(link)
    for assignment in active:
        assignment.revoked = True
    target.linked_safety_floor = min(target.linked_safety_floor, floor)
    transition(db, source, principal.user, "merged", payload.reason, "merged")
    transition(db, target, principal.user, "merge_received", payload.reason)
    return link


async def unmerge(db, source, target, links, principal, payload):
    from app.tickets import transition

    await check_versions(db, source, target, payload)
    link = next(
        (m for m in links if m.active and m.source_id == source.id and m.target_id == target.id),
        None,
    )
    if not link or source.status != "merged":
        raise HTTPException(409, "merge_not_active")
    link.active, link.subscribed = False, False
    link.revision += 1
    link.cancelled_by, link.cancelled_at, link.cancellation_reason = (
        principal.user.id,
        now(),
        payload.reason,
    )
    target.linked_safety_requires_review = True
    restored = (
        "confirmed" if link.previous_status in {"assigned", "in_progress"} else link.previous_status
    )
    transition(db, source, principal.user, "merge_cancelled", payload.reason, restored)
    transition(db, target, principal.user, "merge_source_restored", payload.reason)
