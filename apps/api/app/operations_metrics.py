"""SQL-only dispatcher metrics. No report text, event reasons, or prompts are read."""

from sqlalchemy import Integer, Numeric, and_, case, cast, distinct, func, select

from app.models import (
    Assignment,
    CompletionReport,
    InformationRequest,
    ProcessingAttempt,
    ProcessingJob,
    Ticket,
    TicketEvent,
    User,
)
from app.operations_filters import bounds, category_domain, conditions
from app.security import current_assignment_clause


async def read_operations(
    db,
    start,
    end,
    category_group=None,
    building=None,
    urgency=None,
    category_code=None,
    visibility=None,
):
    filters = conditions(start, end, category_group, building, urgency, category_code)
    if visibility is not None:
        filters.append(visibility)
    ids = select(Ticket.id).where(*filters)
    n = await db.scalar(select(func.count()).select_from(Ticket).where(*filters))
    status_rows = (
        await db.execute(
            select(Ticket.status, func.count()).where(*filters).group_by(Ticket.status)
        )
    ).all()
    domain = category_domain()
    categories = (
        await db.execute(
            select(Ticket.category_code, func.count())
            .where(*filters)
            .group_by(Ticket.category_code)
            .order_by(func.count().desc(), Ticket.category_code)
            .limit(10)
        )
    ).all()
    groups = (
        await db.execute(
            select(domain, func.count())
            .where(*filters)
            .group_by(domain)
            .order_by(func.count().desc(), domain)
        )
    ).all()
    building_expr = Ticket.location_data["selected"]["building_code"].astext
    buildings = (
        await db.execute(
            select(building_expr, func.count())
            .where(*filters)
            .group_by(building_expr)
            .order_by(func.count().desc(), building_expr)
            .limit(10)
        )
    ).all()

    # A first observed transition gives a conservative duration; missing or reversed
    # events never count towards the sample. Completion requires a reviewed report.
    e = TicketEvent
    event_times = (
        select(
            e.ticket_id,
            func.min(e.created_at).filter(e.event_type == "confirmed").label("confirmed"),
            func.min(e.created_at).filter(e.event_type == "assigned").label("assigned"),
            func.min(e.created_at)
            .filter(and_(e.event_type == "report_reviewed", e.to_status == "completed"))
            .label("completed"),
        )
        .where(e.ticket_id.in_(ids))
        .group_by(e.ticket_id)
        .subquery()
    )
    times = (
        select(
            Ticket.id,
            Ticket.public_no,
            Ticket.created_at,
            event_times.c.confirmed,
            event_times.c.assigned,
            event_times.c.completed,
        )
        .join(event_times, event_times.c.ticket_id == Ticket.id)
        .subquery()
    )
    durations = {}
    for key, a, b in (
        ("receipt_to_confirmation", times.c.created_at, times.c.confirmed),
        ("confirmation_to_assignment", times.c.confirmed, times.c.assigned),
        ("assignment_to_completion", times.c.assigned, times.c.completed),
    ):
        seconds = func.extract("epoch", b - a)
        row = (
            await db.execute(
                select(func.count(), func.percentile_cont(0.5).within_group(seconds))
                .select_from(times)
                .where(a.is_not(None), b.is_not(None), b >= a)
            )
        ).one()
        durations[key] = {
            "sample_n": row[0],
            "median_seconds": float(row[1]) if row[0] >= 5 else None,
            "small_sample": row[0] < 5,
        }
    elapsed = func.extract("epoch", times.c.completed - times.c.created_at)
    longest = (
        await db.execute(
            select(times.c.id, times.c.public_no, elapsed.label("seconds"))
            .where(times.c.completed >= times.c.created_at)
            .order_by(elapsed.desc(), times.c.id)
            .limit(5)
        )
    ).all()

    day_expr = func.date(func.timezone("Asia/Seoul", Ticket.created_at))
    received = (
        await db.execute(select(day_expr, func.count()).where(*filters).group_by(day_expr))
    ).all()
    # Completion events are period-based, independent of the ticket's receipt date.
    first, after = bounds(start, end)
    non_date = conditions(start, end, category_group, building, urgency, category_code)[2:]
    if visibility is not None:
        non_date.append(visibility)
    completed_day = func.date(func.timezone("Asia/Seoul", e.created_at))
    completed = (
        await db.execute(
            select(completed_day, func.count(distinct(e.ticket_id)))
            .join(Ticket, Ticket.id == e.ticket_id)
            .where(
                e.event_type == "report_reviewed",
                e.to_status == "completed",
                e.created_at >= first,
                e.created_at < after,
                *non_date,
            )
            .group_by(completed_day)
        )
    ).all()
    terminal = ("completed", "transferred", "merged", "rejected", "cancelled")
    latest_assignment = (
        select(Assignment.id)
        .distinct(Assignment.ticket_id)
        .order_by(Assignment.ticket_id, Assignment.created_at.desc(), Assignment.id.desc())
    )
    active_workers = (
        select(
            User.id.label("worker_id"),
            User.display_name.label("name"),
            func.count(distinct(Ticket.id)).label("open_n"),
            func.cast(0, Integer).label("completed_n"),
        )
        .join(Assignment, Assignment.worker_id == User.id)
        .join(Ticket, Ticket.id == Assignment.ticket_id)
        .where(
            Ticket.id.in_(ids),
            Assignment.id.in_(latest_assignment),
            current_assignment_clause(),
            Assignment.stopped_at.is_(None),
            Assignment.visit_ended_at.is_(None),
            Ticket.status.not_in(terminal),
        )
        .group_by(User.id, User.display_name)
    )
    # Only approved completion reports prove the worker at completion. If the
    # newest approval time is tied across workers, leave the ticket unattributed.
    ranked_reports = (
        select(
            CompletionReport.ticket_id,
            Assignment.worker_id,
            func.dense_rank()
            .over(
                partition_by=CompletionReport.ticket_id,
                order_by=CompletionReport.approved_at.desc(),
            )
            .label("approval_rank"),
        )
        .join(Assignment, Assignment.id == CompletionReport.assignment_id)
        .join(Ticket, Ticket.id == CompletionReport.ticket_id)
        .where(
            Ticket.id.in_(ids),
            Ticket.status == "completed",
            CompletionReport.reviewed.is_(True),
            CompletionReport.approved_at.is_not(None),
            CompletionReport.confirmed_result == "completed",
            Assignment.ticket_id == CompletionReport.ticket_id,
        )
        .subquery()
    )
    latest_reports = (
        select(ranked_reports.c.ticket_id, ranked_reports.c.worker_id)
        .where(ranked_reports.c.approval_rank == 1)
        .distinct()
        .subquery()
    )
    unambiguous = (
        select(latest_reports.c.ticket_id)
        .group_by(latest_reports.c.ticket_id)
        .having(func.count(distinct(latest_reports.c.worker_id)) == 1)
    )
    completed_workers = (
        select(
            User.id.label("worker_id"),
            User.display_name.label("name"),
            func.cast(0, Integer).label("open_n"),
            func.count(distinct(latest_reports.c.ticket_id)).label("completed_n"),
        )
        .join(latest_reports, latest_reports.c.worker_id == User.id)
        .where(latest_reports.c.ticket_id.in_(unambiguous))
        .group_by(User.id, User.display_name)
    )
    worker_counts = active_workers.union_all(completed_workers).subquery()
    workers = (
        await db.execute(
            select(
                worker_counts.c.worker_id,
                worker_counts.c.name,
                func.sum(worker_counts.c.open_n),
                func.sum(worker_counts.c.completed_n),
            )
            .group_by(worker_counts.c.worker_id, worker_counts.c.name)
            .order_by(worker_counts.c.name, worker_counts.c.worker_id)
        )
    ).all()
    completed_total = await db.scalar(
        select(func.count()).select_from(Ticket).where(*filters, Ticket.status == "completed")
    )
    attributed = await db.scalar(select(func.count()).select_from(unambiguous.subquery()))
    unattributed_completed_n = completed_total - attributed
    # Latest request determines the mutually exclusive answer state, matching ticket_view.
    latest_information = (
        select(InformationRequest.ticket_id, InformationRequest.status)
        .distinct(InformationRequest.ticket_id)
        .order_by(
            InformationRequest.ticket_id,
            InformationRequest.created_at.desc(),
            InformationRequest.id.desc(),
        )
        .subquery()
    )
    info_rows = (
        await db.execute(
            select(latest_information.c.status, func.count())
            .where(latest_information.c.ticket_id.in_(ids))
            .group_by(latest_information.c.status)
        )
    ).all()
    waiting_threshold = case(
        (Ticket.effective_urgency == 0, 30),
        (Ticket.effective_urgency == 1, 240),
        (Ticket.effective_urgency == 2, 1440),
        (Ticket.effective_urgency == 3, 4320),
    )
    age_minutes = func.extract("epoch", func.now() - Ticket.created_at) / 60
    waiting = (
        await db.execute(
            select(
                func.count().filter(Ticket.status.not_in(terminal)),
                func.count().filter(
                    Ticket.status.not_in(terminal), age_minutes >= waiting_threshold
                ),
            ).where(*filters)
        )
    ).one()

    # Attempts use their own clock; a sent, numeric cost is the only known settlement.
    meta = ProcessingAttempt.metadata_json
    cost_text = meta["cost_usd"].astext
    known_cost = and_(
        ProcessingAttempt.mode == "live",
        func.jsonb_typeof(meta["sent"]) == "boolean",
        meta["sent"].astext == "true",
        cost_text.op("~")(r"^[0-9]{1,10}(\.[0-9]{1,12})?$"),
    )
    attempts = (
        await db.execute(
            select(
                func.count().filter(ProcessingAttempt.mode == "live"),
                func.count().filter(ProcessingAttempt.mode == "replay"),
                func.count(distinct(ProcessingJob.ticket_id)).filter(
                    ProcessingAttempt.mode == "live",
                    ProcessingAttempt.applied.is_(True),
                    func.jsonb_typeof(meta["sent"]) == "boolean",
                    meta["sent"].astext == "true",
                ),
                func.count(distinct(ProcessingJob.ticket_id)).filter(
                    ProcessingAttempt.mode == "live",
                    ProcessingAttempt.applied.is_(True),
                    func.jsonb_typeof(meta["cache_hit"]) == "boolean",
                    meta["cache_hit"].astext == "true",
                    func.jsonb_typeof(meta["sent"]) == "boolean",
                    meta["sent"].astext == "false",
                ),
                func.count(distinct(ProcessingJob.ticket_id)).filter(
                    ProcessingAttempt.mode == "replay", ProcessingAttempt.applied.is_(True)
                ),
                func.count().filter(known_cost),
                func.sum(case((known_cost, cast(cost_text, Numeric)), else_=None)),
                func.count().filter(func.jsonb_typeof(meta["rule_floor_raised"]) == "boolean"),
                func.count().filter(
                    func.jsonb_typeof(meta["rule_floor_raised"]) == "boolean",
                    meta["rule_floor_raised"].astext == "true",
                ),
            )
            .select_from(ProcessingAttempt)
            .join(ProcessingJob, ProcessingJob.id == ProcessingAttempt.job_id)
            .join(Ticket, Ticket.id == ProcessingJob.ticket_id)
            .where(
                ProcessingJob.kind == "ticket_analysis",
                ProcessingAttempt.started_at >= first,
                ProcessingAttempt.started_at < after,
                *non_date,
            )
        )
    ).one()
    confirmed = (
        select(TicketEvent.id)
        .where(TicketEvent.ticket_id == Ticket.id, TicketEvent.event_type == "confirmed")
        .exists()
    )
    edited = (
        select(TicketEvent.id)
        .where(TicketEvent.ticket_id == Ticket.id, TicketEvent.event_type == "edited")
        .exists()
    )
    overridden = (
        select(TicketEvent.id)
        .where(TicketEvent.ticket_id == Ticket.id, TicketEvent.event_type == "urgency_overridden")
        .exists()
    )
    ai_category = Ticket.safety_data["ai_draft"]["category_code"].astext
    ai_urgency = Ticket.safety_data["ai_draft"]["ai_urgency"].astext
    comparisons = (
        await db.execute(
            select(
                func.count().filter(confirmed, ai_category.is_not(None)),
                func.count().filter(
                    confirmed, ai_category.is_not(None), edited, Ticket.category_code != ai_category
                ),
                func.count().filter(confirmed, ai_urgency.in_(("0", "1", "2", "3"))),
                func.count().filter(confirmed, ai_urgency.in_(("0", "1", "2", "3")), overridden),
            ).where(*filters)
        )
    ).one()
    information = dict(info_rows)
    ai = {
        "attempt_period_basis": "attempt_started_at",
        "live_attempts_n": attempts[0],
        "replay_attempts_n": attempts[1],
        "live_applied_ticket_n": attempts[2],
        "cached_applied_ticket_n": attempts[3],
        "replay_applied_ticket_n": attempts[4],
        "known_settled_cost_n": attempts[5],
        "known_settled_cost_usd": str(attempts[6]) if attempts[6] is not None else None,
        "safety_comparable_attempt_n": attempts[7],
        "safety_raised_attempt_n": attempts[8],
        "comparison_basis": "current_live_draft_and_confirmed_ticket; history_causality_unknown",
        "category_comparable_n": comparisons[0],
        "category_edited_and_differs_n": comparisons[1],
        "urgency_comparable_n": comparisons[2],
        "urgency_overridden_n": comparisons[3],
    }
    return {
        "cohort_n": n,
        "unattributed_completed_n": unattributed_completed_n,
        "completion_trend_basis": (
            "daily_distinct_approved_completion_events; "
            "re-completions_on_different_days_count_again"
        ),
        "information": {
            "waiting": information.get("open", 0),
            "answered": information.get("answered", 0),
        },
        "waiting": {
            "open_n": waiting[0],
            "long_n": waiting[1],
            "threshold_minutes_by_urgency": [30, 240, 1440, 4320],
        },
        "ai": ai,
        "status": dict(status_rows),
        "categories": [{"key": k, "count": v} for k, v in categories],
        "category_groups": [{"key": k, "count": v} for k, v in groups],
        "buildings": [{"key": k, "count": v} for k, v in buildings],
        "durations": durations,
        "longest": [
            {"ticket_id": str(i), "public_no": p, "seconds": float(s)} for i, p, s in longest
        ],
        "received_by_day": {str(d): v for d, v in received},
        "completed_by_day": {str(d): v for d, v in completed},
        "workers": [
            {"worker_id": str(i), "name": name, "open_n": o, "completed_n": c}
            for i, name, o, c in workers
        ],
    }
