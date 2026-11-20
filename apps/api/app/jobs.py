"""DB-owned analysis attempts (replay or opt-in live), leases and ID-only ARQ dispatch."""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import ROUND_UP, Decimal
from zoneinfo import ZoneInfo

from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from dotenv import dotenv_values
from sqlalchemy import func, or_, select
from sqlalchemy.engine import make_url

from app.booth_fixtures import allowed_live_input
from app.catalog import CATEGORIES, CATEGORY_MAP
from app.config import ROOT, Settings, isolated_settings_requested
from app.db import Database
from app.followup_questions import analysis_text, approved_evidence, clean_questions
from app.location_workflow import safety_evidence_parts
from app.models import (
    Assignment,
    CompletionReport,
    ProcessingAttempt,
    ProcessingJob,
    Ticket,
    TicketEvent,
    User,
)
from app.security import assignment_current
from app.services import llm
from app.services.routing import suggest
from app.services.safety import assess_live_urgency, has_current_hazard, inspect_raw_text

logger = logging.getLogger("baroai.jobs")
MAX_ATTEMPTS = 3
LIVE_MAX_ATTEMPTS = 2
LIVE_RETRYABLE = {
    "timeout", "provider_unavailable", "transport_failed", "interrupted"
}
LEASE_SECONDS = 30
REDISPATCH_SECONDS = 10
TERMINAL = {"succeeded", "failed", "superseded"}
# Retrying cannot change these outcomes; fail now and leave it to people.
NON_RETRYABLE = {
    "provider_auth_failed",
    "live_processing_unavailable",
    "daily_cap_exceeded",
    "daily_budget_exceeded",
    "ticket_cap_exceeded",
    "budget_authorization_required",
    "budget_invalid",
    "attempt_already_reserved",
    "lease_lost",
    "policy_violation",
}


@dataclass(frozen=True)
class Lease:
    job_id: uuid.UUID
    token: uuid.UUID
    attempt_no: int
    text: str
    kind: str = "ticket_analysis"
    report_result: str | None = None
    ticket_id: uuid.UUID | None = None
    # None means the fixed legacy catalog; a tuple is an explicit allowed list.
    categories: tuple[tuple[str, str], ...] | None = None
    rule_floor: int = 3
    history_replay_context: tuple[str, int] | None = None
    has_followup: bool = False
    booth_live_intake: bool = False


def history_replay_enabled(settings):
    if not settings or not settings.llm_fake or settings.deployment_mode != 'demo':
        return False
    if (settings.app_env != 'dev' or not settings.demo_auth
            or settings.planning_dataset_provider != 'db'
            or os.environ.get('BARO_ISOLATED_SETTINGS') != '1'
            or os.environ.get('BARO_CAMPUS_OWNED_SUPERVISOR') != '1'
            or os.environ.get('BARO_BOOTH_HISTORY_REPLAY') != '1'):
        return False
    url = make_url(settings.database_url.get_secret_value())
    generation = os.path.basename(os.environ.get('HOME', ''))
    return (bool(re.fullmatch(r'[0-9a-f]{32}', generation))
            and url.drivername == 'postgresql+asyncpg' and url.host == '127.0.0.1'
            and url.database == 'booth_' + generation and url.username == url.database
            and bool(url.port) and not url.query)


async def history_context(db, ticket, settings):
    if not history_replay_enabled(settings):
        return None
    from app.models import Upload

    count = await db.scalar(select(func.count(Upload.id)).where(
        Upload.ticket_id == ticket.id, Upload.report_id.is_(None),
        Upload.information_request_id.is_(None)
    ))
    return ticket.location_text, count


async def db_clock(db):
    return await db.scalar(select(func.clock_timestamp()))


async def locked_job(db, job_id):
    target = (
        await db.execute(
            select(ProcessingJob.ticket_id, ProcessingJob.report_id, Assignment.worker_id)
            .outerjoin(CompletionReport, CompletionReport.id == ProcessingJob.report_id)
            .outerjoin(Assignment, Assignment.id == CompletionReport.assignment_id)
            .where(ProcessingJob.id == job_id)
        )
    ).one_or_none()
    if target is None:
        return None, None
    ticket_id, report_id, worker_id = target
    worker = None
    if worker_id is not None:
        # Match request/CLI lock ordering: User before ticket/assignment/report/job.
        worker = await db.scalar(
            select(User)
            .where(User.id == worker_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ticket = await db.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket and db.info["settings"].deployment_mode == "public_demo":
        from app.tickets import public_visible_ticket

        visible = await db.scalar(
            select(Ticket.id).where(
                Ticket.id == ticket.id, public_visible_ticket(await db_clock(db))
            )
        )
        if not visible:
            return None, None
    report, assignment = None, None
    if report_id:
        assignment_id = await db.scalar(
            select(CompletionReport.assignment_id).where(CompletionReport.id == report_id)
        )
        assignment = await db.scalar(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        report = await db.scalar(
            select(CompletionReport)
            .where(CompletionReport.id == report_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    job = await db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job:
        job._report, job._assignment, job._worker = report, assignment, worker
    return ticket, job


async def mark_live_ticket_superseded(db, ticket, job):
    latest_id = await db.scalar(
        select(ProcessingJob.id)
        .where(ProcessingJob.ticket_id == ticket.id, ProcessingJob.kind == "ticket_analysis")
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
        .limit(1)
    )
    if latest_id != job.id:
        return
    draft = (ticket.safety_data or {}).get("ai_draft") or {}
    fingerprint = draft.get("input_hash")
    valid = False
    if isinstance(fingerprint, str):
        snapshot = await approved_image_snapshot(db, ticket.id)
        current = input_fingerprint(
            db, analysis_text(ticket), snapshot, draft.get("selected_image_digests", [])
        )
        valid = hmac.compare_digest(fingerprint, current)
    ticket.ai_status = "drafted" if valid else "superseded"


async def preserve_successor_status(db, ticket, job):
    if job.kind != "ticket_analysis":
        return
    successor = await db.scalar(select(ProcessingJob).where(
        ProcessingJob.ticket_id == ticket.id, ProcessingJob.kind == job.kind,
        ProcessingJob.input_revision > job.input_revision,
        ProcessingJob.state.in_(("queued", "running", "retry_wait")),
    ).order_by(ProcessingJob.input_revision.desc()).limit(1))
    if successor:
        ticket.ai_status = successor.state


async def current_attempt(db, job):
    return await db.scalar(
        select(ProcessingAttempt).where(
            ProcessingAttempt.job_id == job.id, ProcessingAttempt.attempt_no == job.attempt_count
        )
    )


def stale(ticket, job, live=False):
    if job.kind == "report_analysis":
        report, assignment = job._report, job._assignment
        return (
            not report
            or not assignment
            or not assignment_current(assignment, job._worker)
            or report.reviewed
            or report.analysis_status == "dismissed"
            or report.worker_confirmed_revision is not None
            or report.revision != job.input_revision
            or ticket.status != "in_progress"
        )
    if live:
        return ticket.status in {"completed", "transferred", "merged", "rejected", "cancelled"}
    return ticket.analysis_revision != job.input_revision or ticket.status != "received"


def attempt_metadata(kind, live, observed, protocol="v1"):
    report = kind == "report_analysis"
    base = asdict(
        llm.LiveMetadata(
            prompt_version=(llm.live_versions(kind, protocol)[0] if live else
                            (llm.REPORT_PROMPT_VERSION if report else llm.PROMPT_VERSION)),
            schema_version=(llm.live_versions(kind, protocol)[1] if live else
                            (llm.REPORT_SCHEMA_VERSION if report else llm.SCHEMA_VERSION)),
            kind=kind,
            sent=None,
        )
    )
    base["duration_ms"] = None
    base["attempts"] = None
    base["image_count"] = None
    base["mode"] = "live" if live else "replay"
    base["protocol"] = protocol if live else None
    if observed:
        base.update(observed)
    base["kind"] = kind
    if live and base.get("sent") is True and base.get("cache_hit") is None:
        base["cache_hit"] = False
    return base


def end_attempt(attempt, stamp, code, *, applied=False, metadata=None):
    if attempt and attempt.ended_at is None:
        attempt.ended_at, attempt.error_code, attempt.applied = stamp, code, applied
        attempt.duration_ms = max(0, (stamp - attempt.started_at).total_seconds() * 1000)
        if metadata is not None:
            attempt.metadata_json = settle_metadata(attempt.metadata_json or {}, metadata)


def settle_metadata(previous, observed):
    merged = {**previous, **observed}
    for key in (
        "reserved_usd", "reservation_day", "price_rates", "price_as_of",
        "requested_model", "input_hash", "location_input_hash", "payload_hash",
        "selected_image_digests",
        "transfer_image_digests", "cache_key", "cache_result", "cache_model",
    ):
        if key in previous and (key != "requested_model" or previous[key] is not None):
            merged[key] = previous[key]
    if previous.get("service_tier") not in (None, "default", "unknown"):
        merged["service_tier"] = previous["service_tier"]
    if previous.get("reserved_usd") is not None and previous.get("cost_usd") is None:
        merged["cost_usd"] = (
            llm.measured_cost(observed, previous.get("price_rates"))
            if observed.get("service_tier") == "default"
            else None
        )
    elif previous.get("cost_usd") is not None:
        merged["cost_usd"] = previous["cost_usd"]
    return merged


def reservation_day(stamp):
    return stamp.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()


def cache_fingerprint(db, lease, body, transferred, protocol):
    """Purpose-separated HMAC over the actual semantic request, without raw input storage."""
    normalized = (lease.text if lease.kind == "ticket_analysis" else " ".join(lease.text.split()))
    allowed = llm._allowed_categories(lease.categories) if lease.kind == "ticket_analysis" else []
    versions = llm.live_versions(lease.kind, protocol)
    if lease.kind == "report_analysis":
        user = (llm.prompts.report_v2_user(normalized, lease.report_result)
                if protocol == "v2" else llm.prompts.report_user_message(
                    normalized, lease.report_result))
    else:
        user = (llm.prompts.ticket_v2_user(normalized) if protocol == "v2"
                else llm.prompts.ticket_user_message(normalized, allowed, len(transferred)))
    messages = [dict(message) for message in body["messages"]]
    messages[1]["content"] = {"normalized_text": user, "transferred_sha256": transferred}
    request = {**body, "messages": messages}
    semantic = {
        "kind": lease.kind, "protocol": protocol, "versions": versions,
        "request": request, "categories": allowed,
    }
    key = db.info["settings"].jwt_secret.get_secret_value().encode()
    derived = hmac.digest(key, b"baroai-ai06-result-cache-v1", "sha256")
    data = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(derived, data.encode(), hashlib.sha256).hexdigest()


def cached_draft(lease, value, model, requested_model, protocol):
    """Revalidate a minimal source result without copying provider metadata."""
    from app.schemas import ReportStructure

    if not isinstance(value, dict):
        return None
    prompt_version, schema_version = llm.live_versions(lease.kind, protocol)
    meta = llm.LiveMetadata(
        prompt_version=prompt_version, schema_version=schema_version, kind=lease.kind,
        protocol=protocol, mode="live", model=model, requested_model=requested_model, sent=False,
        cache_hit=True, input_tokens=0, cached_tokens=0, output_tokens=0,
        reasoning_tokens=0, cost_usd="0", duration_ms=0,
    )
    try:
        if lease.kind == "ticket_analysis":
            llm.reject_promises([value.get("summary"), value.get("urgency_reason")])
            value = dict(value)
            questions = clean_questions(value.pop("followup_questions", []))
            output = llm.LiveTicketOutput.model_validate(value, strict=True)
            llm.reject_promises([output.summary, output.urgency_reason])
            allowed = {code for code, _ in llm._allowed_categories(lease.categories)}
            if output.category_code not in allowed:
                return None
            return llm.Draft(
                replay_match="live", mode="live", category_code=output.category_code,
                ai_urgency=output.ai_urgency, summary=output.summary,
                urgency_reason=output.urgency_reason, model=model,
                prompt_version=prompt_version, metadata=meta,
                followup_questions=tuple(questions),
                hazard_signals=tuple(output.hazard_signals),
                rule_signal_assessment=tuple(x.model_dump() for x in output.rule_signal_assessment),
            )
        actions, materials = value.get("actions"), value.get("materials_used")
        llm.reject_promises([
            *(actions if isinstance(actions, list) else []),
            *(materials if isinstance(materials, list) else []), value.get("residual_issue"),
        ])
        structure = ReportStructure.model_validate(value, strict=True)
        if structure.source != "live" or structure.photo_comparison != "not_performed":
            return None
        if lease.report_result == "revisit" and not structure.revisit_needed:
            return None
        llm.LiveReportOutput.model_validate({
            "actions": structure.actions, "materials_used": structure.materials_used,
            "residual_issue": structure.residual_issue, "revisit_needed": structure.revisit_needed,
        }, strict=True)
        llm.reject_promises(
            [*structure.actions, *structure.materials_used, structure.residual_issue]
        )
        return llm.ReportDraft(structure=structure, mode="live", metadata=meta)
    except (ValueError, TypeError, llm._LiveFailure):
        return None


async def lookup_cache(database, lease, policy, body, originals, transferred):
    # Source consent cannot safely be re-locked after the destination ticket.
    # Until a coordinated multi-ticket lock exists, photo inputs always miss.
    if originals or transferred:
        return None
    from app.dispatch_lock import dispatch_lock

    async with database.transaction() as db:
        await dispatch_lock(db)
        ticket, job = await locked_job(db, lease.job_id)
        stamp = await db_clock(db)
        if (not job or job.state != "running" or job.lease_token != lease.token
                or job.attempt_count != lease.attempt_no or not job.lease_expires_at
                or job.lease_expires_at <= stamp or stale(ticket, job, live=True)
                or (job.kind == "ticket_analysis" and analysis_text(ticket) != lease.text)):
            return "lease_lost"
        attempt = await current_attempt(db, job)
        if not attempt:
            return "lease_lost"
        snapshot = (await approved_image_snapshot(db, ticket.id)
                    if job.kind == "ticket_analysis" else [])
        key = cache_fingerprint(db, lease, body, transferred, policy.llm_protocol)
        meta = dict(attempt.metadata_json or {})
        meta["cache_key"] = key
        if job.kind == "ticket_analysis":
            meta["input_hash"] = input_fingerprint(db, analysis_text(ticket), snapshot, [])
            meta["location_input_hash"] = input_fingerprint(db, ticket.location_text, [], [])
            meta["selected_image_digests"] = []
        attempt.metadata_json = meta
        sources = (await db.scalars(select(ProcessingAttempt).join(
            ProcessingJob, ProcessingJob.id == ProcessingAttempt.job_id
        ).where(
            ProcessingAttempt.mode == "live", ProcessingAttempt.applied.is_(True),
            ProcessingAttempt.error_code.is_(None),
            ProcessingAttempt.ended_at >= stamp - timedelta(hours=24),
            ProcessingAttempt.ended_at <= stamp,
            ProcessingAttempt.metadata_json["cache_key"].astext == key,
            ProcessingAttempt.metadata_json["sent"].astext == "true",
            ProcessingJob.state == "succeeded", ProcessingJob.kind == job.kind,
            ProcessingJob.id != job.id,
        ).order_by(ProcessingAttempt.ended_at.desc()).limit(10))).all()
        for source in sources:
            previous = source.metadata_json or {}
            if previous.get("cache_hit") is True or previous.get("image_count") not in (0, None):
                continue
            source_model = previous.get("model")
            if (not isinstance(source_model, str)
                    or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", source_model)
                    or source_model.lower().startswith(("sk-", "key-", "bearer-"))):
                source_model = None
            draft = cached_draft(lease, previous.get("cache_result"), source_model,
                                 body["model"], policy.llm_protocol)
            if draft is not None:
                return draft
        return None


async def reserve_budget(
    database, lease, policy, request_bytes, model, image_count, output_cap, image_digests=None,
    transfer_digests=None,
):
    from app.dispatch_lock import dispatch_lock

    async with database.transaction() as db:
        await dispatch_lock(db)
        ticket, job = await locked_job(db, lease.job_id)
        stamp = await db_clock(db)
        if (
            not job
            or job.state != "running"
            or job.lease_token != lease.token
            or job.attempt_count != lease.attempt_no
            or not job.lease_expires_at
            or job.lease_expires_at <= stamp
            or stale(ticket, job, live=True)
            or (job.kind == "ticket_analysis" and analysis_text(ticket) != lease.text)
        ):
            return "lease_lost"
        attempt = await current_attempt(db, job)
        if not attempt or (attempt.metadata_json or {}).get("reserved_usd") is not None:
            return "attempt_already_reserved"
        input_hash = None
        if job.kind == "ticket_analysis":
            snapshot = await approved_image_snapshot(db, ticket.id)
            selected = image_digests or []
            if len(selected) != image_count or Counter(selected) - Counter(snapshot):
                return "lease_lost"
            transferred = transfer_digests if transfer_digests is not None else selected
            if len(transferred) != image_count or any(
                not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                for digest in transferred
            ):
                return "lease_lost"
            input_hash = input_fingerprint(db, analysis_text(ticket), snapshot, selected)
            payload_hash = input_fingerprint(db, analysis_text(ticket), [], transferred)
            previous = (await db.scalars(
                select(ProcessingAttempt.metadata_json)
                .join(ProcessingJob, ProcessingJob.id == ProcessingAttempt.job_id)
                .where(
                    ProcessingJob.ticket_id == ticket.id, ProcessingJob.kind == job.kind,
                    ProcessingJob.id != job.id,
                    ProcessingAttempt.metadata_json["reserved_usd"].astext.is_not(None),
                )
            )).all()
            for prior in previous:
                prior = prior or {}
                prior_transferred = prior.get("transfer_image_digests")
                if (prior_transferred is None and selected
                        and prior.get("image_count") != 0):
                    # A legacy image attempt cannot prove the new bytes differ.
                    return "input_unchanged"
                prior_hash = prior.get("payload_hash")
                if isinstance(prior_hash, str):
                    if hmac.compare_digest(prior_hash, payload_hash):
                        return "input_unchanged"
                    continue
                # Older attempts may not have a payload fingerprint. Only a
                # recorded text-only attempt proves a later photo is new input.
                prior_selected = prior.get("selected_image_digests")
                if isinstance(prior_selected, list):
                    if prior_selected == selected:
                        return "input_unchanged"
                elif prior.get("image_count") != 0 or not selected:
                    return "input_unchanged"
        rates = llm.rates_for(policy, model)
        budget = llm.decimal_value(policy.daily_llm_budget_usd)
        image_bound = policy.llm_price_table.get(model, {}).get("image_input_token_bound")
        if (
            not llm.pricing_ready(policy)
            or rates is None
            or budget is None
            or (
                image_count
                and (type(image_bound) is not int or not 0 < image_bound <= 1_000_000_000)
            )
        ):
            return "budget_invalid"
        estimate = (
            (request_bytes + 4096 + image_count * (image_bound or 0)) * rates["in"]
            + output_cap * rates["out"]
        ) / 1000000
        estimate = estimate.quantize(Decimal("0.000001"), rounding=ROUND_UP)
        day = reservation_day(stamp)
        rows = (
            await db.execute(
                select(ProcessingAttempt.metadata_json, ProcessingJob.ticket_id, ProcessingJob.kind)
                .join(ProcessingJob, ProcessingJob.id == ProcessingAttempt.job_id)
                .where(
                    or_(
                        ProcessingAttempt.metadata_json["reservation_day"].astext == day,
                        (ProcessingJob.ticket_id == job.ticket_id)
                        & (ProcessingJob.kind == job.kind),
                        ProcessingAttempt.metadata_json["service_tier"].astext.notin_(
                            ("default", "unknown")
                        ),
                    )
                )
            )
        ).all()
        total, ticket_count = Decimal(0), 0
        for meta, ticket_id, kind in rows:
            meta = meta or {}
            if meta.get("reserved_usd") is None:
                continue
            if ticket_id == job.ticket_id and kind == job.kind:
                ticket_count += 1
            if meta.get("service_tier") not in (None, "default", "unknown"):
                return "budget_invalid"
            if meta.get("reservation_day") == day:
                value = llm.decimal_value(
                    meta.get("cost_usd")
                    if meta.get("cost_usd") is not None
                    else meta.get("reserved_usd")
                )
                if value is None:
                    return "budget_invalid"
                total += value
        if ticket_count >= 3:
            return "ticket_cap_exceeded"
        if total + estimate > budget:
            return "daily_budget_exceeded"
        attempt.metadata_json = {
            **(attempt.metadata_json or {}),
            "reserved_usd": str(estimate),
            "reservation_day": day,
            "price_rates": {k: str(v) for k, v in rates.items()},
            "price_as_of": policy.llm_price_as_of,
            "requested_model": model,
            **({"input_hash": input_hash,
                "location_input_hash": input_fingerprint(db, ticket.location_text, [], []),
                "payload_hash": payload_hash,
                "selected_image_digests": selected, "transfer_image_digests": transferred}
               if input_hash is not None else {}),
        }
        job.lease_expires_at = stamp + timedelta(
            seconds=lease_seconds(db.info.get("settings"), True)
        )
        return llm.ReservationGrant(str(lease.job_id), str(lease.token), lease.attempt_no)


def set_status(ticket, job, status):
    if job.kind == "report_analysis":
        job._report.analysis_status = status
    else:
        ticket.ai_status = status


def complete_job(job, stamp, state, error=None):
    job.state, job.error_code, job.completed_at = state, error, stamp
    job.lease_token, job.lease_expires_at, job.next_attempt_at = None, None, None


def failed_event(job, live):
    if job.report_id:
        return "report_analysis_live_failed" if live else "report_analysis_failed"
    return "analysis_live_failed" if live else "analysis_failed"


def audit(db, ticket, stamp, event, reason):
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=None,
            created_at=stamp,
            event_type=event,
            reason=reason,
            from_status=ticket.status,
            to_status=ticket.status,
        )
    )
    ticket.updated_at = stamp


def lease_seconds(settings, live):
    """A live attempt must be able to finish its provider timeout inside the lease."""
    if not live:
        return LEASE_SECONDS
    return max(LEASE_SECONDS, int(getattr(settings, "llm_timeout_s", 20)) + 15)


def live_mode(db, live=None):
    """Explicit caller mode wins; otherwise the transaction's application settings."""
    if live is not None:
        return live
    settings = db.info.get("settings")
    return settings is not None and not settings.llm_fake


async def acquire_lease(database, job_id, *, live=None, admit_job=None):
    """Optionally admit a locked job using a read-only async check in this transaction."""
    async with database.transaction() as db:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        ticket, job = await locked_job(db, job_id)
        if not job or job.state in TERMINAL:
            return None
        if admit_job is not None and not await admit_job(db, job):
            return None
        stamp = await db_clock(db)
        # An unexpired owner alone may decide whether its in-flight input is stale.
        # A worker in another mode must not supersede that owner's attempt.
        if job.state == "running" and job.lease_expires_at and job.lease_expires_at > stamp:
            return None
        live = live_mode(db, live)
        if stale(ticket, job, live):
            end_attempt(await current_attempt(db, job), stamp, "superseded")
            complete_job(job, stamp, "superseded", "superseded")
            # Never overwrite a later revision's analysis status.
            if job.kind == "report_analysis":
                if (
                    job._report
                    and job._report.revision == job.input_revision
                    and job._report.worker_confirmed_revision is None
                    and not job._report.reviewed
                    and job._report.analysis_status != "dismissed"
                ):
                    job._report.analysis_status = "superseded"
            elif live:
                await mark_live_ticket_superseded(db, ticket, job)
            elif ticket.analysis_revision == job.input_revision:
                ticket.ai_status = "superseded"
            return None
        if job.kind == "ticket_analysis" and live:
            prior_open = await db.scalar(select(ProcessingJob.id).where(
                ProcessingJob.ticket_id == ticket.id, ProcessingJob.kind == job.kind,
                ProcessingJob.id != job.id, ProcessingJob.input_revision < job.input_revision,
                ProcessingJob.state.in_(("running", "retry_wait")),
            ).limit(1))
            if prior_open:
                return None
        previous_attempt = await current_attempt(db, job) if job.attempt_count else None
        # Never extend a live attempt's limit by switching the worker to replay.
        prior_live = await db.scalar(
            select(ProcessingAttempt.id).where(
                ProcessingAttempt.job_id == job.id, ProcessingAttempt.mode == "live"
            ).limit(1)
        ) if job.attempt_count else None
        limited_live = live or bool(prior_live)
        attempt_limit = LIVE_MAX_ATTEMPTS if limited_live else MAX_ATTEMPTS
        if job.state == "running":
            end_attempt(previous_attempt, stamp, "interrupted")
        elif job.next_attempt_at and job.next_attempt_at > stamp:
            return None
        if job.attempt_count >= attempt_limit:
            complete_job(job, stamp, "failed", "attempts_exhausted")
            set_status(ticket, job, "failed")
            await preserve_successor_status(db, ticket, job)
            audit(
                db,
                ticket,
                stamp,
                failed_event(job, limited_live),
                "AI 분석 중단 후 시도 소진 · 사람 확인 필요"
                if limited_live
                else "재생 분석 중단 후 시도 소진 · 사람 확인 필요",
            )
            return None
        job.attempt_count += 1
        token = uuid.uuid4()
        settings = db.info.get("settings")
        live = live_mode(db, live)
        job.state, job.lease_token = "running", token
        job.lease_expires_at = stamp + timedelta(seconds=lease_seconds(settings, live))
        job.next_attempt_at, job.error_code = None, None
        set_status(ticket, job, "running")
        db.add(
            ProcessingAttempt(
                job_id=job.id,
                attempt_no=job.attempt_count,
                mode="live" if live else "replay",
                started_at=stamp,
                metadata_json={
                    **attempt_metadata(job.kind, live, observed=None,
                                       protocol=getattr(settings, "llm_protocol", "v1")),
                    "deployment_mode": getattr(settings, "deployment_mode", None),
                },
            )
        )
        return Lease(
            job.id,
            token,
            job.attempt_count,
            job._report.text if job.report_id else analysis_text(ticket),
            kind=job.kind,
            report_result=job._report.result if job.report_id else None,
            ticket_id=ticket.id,
            categories=None if job.report_id or not live else await live_categories(db, settings),
            rule_floor=(ticket.safety_data or {}).get("rule_floor", 3),
            history_replay_context=(await history_context(db, ticket, settings)
                                    if not job.report_id else None),
            has_followup=bool(approved_evidence(ticket)) if not job.report_id else False,
            booth_live_intake=(ticket.safety_data or {}).get("booth_live_intake") is True,
        )


async def live_categories(db, settings):
    """School catalog mode offers the active catalog's categories (code, name).

    Without an active catalog only UNKNOWN is allowed. File mode uses the fixed list.
    """
    if not settings or settings.planning_dataset_provider != "db":
        return None
    from app.school_catalog_routes import active_catalog

    allowed = [("UNKNOWN", "담당자 확인 필요")]
    _pointer, active = await active_catalog(db)
    listed = (active.content or {}).get("categories") if active else None
    if isinstance(listed, list):
        allowed += [
            (item["code"], item["name"] if isinstance(item.get("name"), str) else item["code"])
            for item in listed
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        ]
    return tuple(allowed)


async def ticket_photo_pending(db, ticket_id):
    """Whether an undeleted local ticket photo still lacks current valid approval."""
    from app.models import ImageApproval, ImageVariant, Upload
    from app.protection import expected_purpose, managed_db_present
    from app.security import now

    if await managed_db_present(db):
        return False
    stamp = now()
    provider = getattr(db.info.get("settings"), "llm_primary", "openai")
    uploads = (await db.scalars(select(Upload).where(
        Upload.ticket_id == ticket_id, Upload.report_id.is_(None),
        Upload.information_request_id.is_(None),
        Upload.assignment_id.is_(None), Upload.deleted.is_(False), Upload.delete_after > stamp,
    ))).all()
    for upload in uploads:
        if expected_purpose(upload) != "ticket_analysis":
            continue
        variant = (await db.get(ImageVariant, upload.current_variant_id)
                   if upload.current_variant_id else None)
        if (not variant or variant.upload_id != upload.id or variant.status != "ready"
                or not variant.object_key or not variant.sha256 or variant.delete_after <= stamp):
            return True
        approval = await db.scalar(select(ImageApproval.id).where(
            ImageApproval.upload_id == upload.id, ImageApproval.variant_id == variant.id,
            ImageApproval.revoked_at.is_(None), ImageApproval.actor_id == upload.owner_id,
            ImageApproval.provider == provider, ImageApproval.purpose == "ticket_analysis",
            ImageApproval.sha256 == variant.sha256,
            ImageApproval.pipeline_version == variant.pipeline_version,
        ))
        if not approval:
            return True
    return False


async def release_photo_wait(db, ticket, job):
    if (job.state == "queued" and job.attempt_count == 0 and job.next_attempt_at
            and not await ticket_photo_pending(db, ticket.id)):
        job.next_attempt_at = None
        job.last_enqueued_at = None


async def approved_image_snapshot(db, ticket_id):
    """Current eligible variant digests; called while the ticket row is locked for writes."""
    from app.models import ImageApproval, ImageVariant, Upload
    from app.protection import expected_purpose, managed_db_present
    from app.security import now

    if await managed_db_present(db):
        return []
    settings = db.info.get("settings")
    provider = getattr(settings, "llm_primary", "openai")
    stamp = now()
    uploads = (await db.scalars(
        select(Upload).where(
            Upload.ticket_id == ticket_id, Upload.report_id.is_(None),
        Upload.information_request_id.is_(None),
            Upload.assignment_id.is_(None), Upload.deleted.is_(False),
            Upload.current_variant_id.is_not(None),
        ).order_by(Upload.created_at, Upload.id).limit(llm.MAX_IMAGES)
    )).all()
    digests = []
    for upload in uploads:
        if upload.delete_after <= stamp or expected_purpose(upload) != "ticket_analysis":
            continue
        variant = await db.get(ImageVariant, upload.current_variant_id, populate_existing=True)
        if (not variant or variant.upload_id != upload.id or variant.status != "ready"
                or not variant.object_key or not variant.sha256 or variant.delete_after <= stamp):
            continue
        approval = await db.scalar(select(ImageApproval.id).where(
            ImageApproval.upload_id == upload.id, ImageApproval.variant_id == variant.id,
            ImageApproval.revoked_at.is_(None), ImageApproval.actor_id == upload.owner_id,
            ImageApproval.provider == provider, ImageApproval.purpose == "ticket_analysis",
            ImageApproval.sha256 == variant.sha256,
            ImageApproval.pipeline_version == variant.pipeline_version,
        ))
        if approval:
            digests.append(variant.sha256)
    return digests


def input_fingerprint(db, text, snapshot, selected):
    settings = db.info["settings"]
    key = settings.jwt_secret.get_secret_value().encode()
    derived = hmac.digest(key, b"baroai-ticket-input-v1", "sha256")
    payload = json.dumps([text, snapshot, selected], ensure_ascii=False, separators=(",", ":"))
    return hmac.new(derived, payload.encode(), hashlib.sha256).hexdigest()


async def approved_ticket_images(database, ticket_id, settings):
    """Approved, ready, hash-verified privacy variants only. Never an original upload.

    Any doubt (managed storage, missing approval, hash mismatch) skips the image;
    the ticket is then drafted from text alone.
    """
    from starlette.concurrency import run_in_threadpool

    from app.models import ImageApproval, ImageVariant, Upload
    from app.protection import expected_purpose, managed_db_present
    from app.security import now
    from app.storage import LocalPrivateStorage

    provider = getattr(settings, "llm_primary", "openai")
    images = []
    async with database.sessions() as db:
        if await managed_db_present(db):
            return images
        uploads = (
            await db.scalars(
                select(Upload)
                .where(
                    Upload.ticket_id == ticket_id,
                    Upload.report_id.is_(None),
                    Upload.information_request_id.is_(None),
                    Upload.assignment_id.is_(None),
                    Upload.deleted.is_(False),
                    Upload.current_variant_id.is_not(None),
                )
                .order_by(Upload.created_at, Upload.id)
                .limit(llm.MAX_IMAGES)
            )
        ).all()
        storage = LocalPrivateStorage(settings.storage_path)
        stamp = now()
        for upload in uploads:
            if upload.delete_after <= stamp or expected_purpose(upload) != "ticket_analysis":
                continue
            variant = await db.get(ImageVariant, upload.current_variant_id)
            if (
                not variant
                or variant.upload_id != upload.id
                or variant.status != "ready"
                or not variant.object_key
                or not variant.sha256
                or variant.delete_after <= stamp
            ):
                continue
            approved = await db.scalar(
                select(ImageApproval.id).where(
                    ImageApproval.upload_id == upload.id,
                    ImageApproval.variant_id == variant.id,
                    ImageApproval.revoked_at.is_(None),
                    ImageApproval.actor_id == upload.owner_id,
                    ImageApproval.provider == provider,
                    ImageApproval.purpose == "ticket_analysis",
                    ImageApproval.sha256 == variant.sha256,
                    ImageApproval.pipeline_version == variant.pipeline_version,
                )
            )
            if not approved:
                continue
            try:
                data = await run_in_threadpool(storage.read, variant.object_key)
                images.append(llm.PreprocessedImage(data, variant.sha256, variant.pipeline_version))
                break
            except Exception:
                continue
    # The gateway re-verifies each hash before anything is encoded.
    return images


def validate_ticket_replay(result, text, history_replay_context=None):
    validated = llm.StructuredDraft.model_validate(
        {"category_code": result.category_code, "ai_urgency": result.ai_urgency}
    )
    from app.booth_fixtures import ticket_replay_fixture

    fixture, category = ticket_replay_fixture(
        text, history=history_replay_context is not None,
        location=history_replay_context[0] if history_replay_context else '',
        photo_count=history_replay_context[1] if history_replay_context else 0,
    )
    matched = fixture != 'unknown'
    expected = "matched_replay" if matched else "unmatched"
    if (
        result.mode != "replay"
        or result.replay_match != expected
        or validated.category_code != category
        or validated.ai_urgency is not None
    ):
        raise ValueError("invalid_ticket_replay")
    return validated


def validate_ticket_live(result, lease):
    """Job-boundary re-validation of a live draft; the gateway is not trusted blindly."""
    if (result.mode != "live" or result.replay_match != "live"
            or (result.metadata and result.metadata.category_rejected)):
        raise ValueError("invalid_ticket_live")
    output = llm.LiveTicketOutput.model_validate(
        {
            "category_code": result.category_code,
            "ai_urgency": result.ai_urgency,
            "summary": result.summary,
            "urgency_reason": result.urgency_reason,
            "hazard_signals": list(result.hazard_signals),
            "rule_signal_assessment": list(result.rule_signal_assessment),
        }
    )
    if llm.PROMISE_PATTERN.search(output.summary) or llm.PROMISE_PATTERN.search(
        output.urgency_reason
    ):
        raise ValueError("invalid_ticket_live")
    allowed = {code for code, _ in llm._allowed_categories(lease.categories)}
    if output.category_code not in allowed:
        raise ValueError("invalid_ticket_category")
    return output


def validate_report_result(result, live):
    from app.schemas import ReportStructure

    structure = ReportStructure.model_validate(result.structure.model_dump(), strict=True)
    expected = "live" if live else "replay"
    if result.mode != expected or structure.source != expected:
        raise ValueError("invalid_report_result")
    return structure


async def finish_lease(database, lease, result=None, *, error=None, metadata=None, live=None):
    async with database.transaction() as db:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        ticket, job = await locked_job(db, lease.job_id)
        stamp = await db_clock(db)
        if (
            not job
            or job.state != "running"
            or job.lease_token != lease.token
            or not job.lease_expires_at
            or job.lease_expires_at <= stamp
        ):
            attempt = await db.scalar(
                select(ProcessingAttempt)
                .where(
                    ProcessingAttempt.job_id == lease.job_id,
                    ProcessingAttempt.attempt_no == lease.attempt_no,
                )
                .with_for_update()
            )
            if attempt and not attempt.applied and metadata is not None:
                previous = attempt.metadata_json or {}
                if previous.get("cost_usd") is None:
                    attempt.metadata_json = settle_metadata(
                        previous, {**metadata, "discarded": "lease_lost"}
                    )
            return False
        attempt = await current_attempt(db, job)
        live = attempt.mode == "live" if attempt else live_mode(db, live)
        input_changed = False
        if job.kind == "ticket_analysis":
            saved = (attempt.metadata_json or {}) if attempt else {}
            fingerprint = saved.get("input_hash")
            if fingerprint:
                snapshot = await approved_image_snapshot(db, ticket.id)
                selected = saved.get("selected_image_digests", [])
                input_changed = not hmac.compare_digest(
                    fingerprint, input_fingerprint(db, analysis_text(ticket), snapshot, selected)
                )
            else:
                input_changed = analysis_text(ticket) != lease.text
        if stale(ticket, job, live) or input_changed:
            if metadata is not None:
                metadata = {**metadata, "discarded": "superseded"}
            end_attempt(attempt, stamp, "superseded", metadata=metadata)
            complete_job(job, stamp, "superseded", "superseded")
            if (
                job.kind == "report_analysis"
                and job._report
                and job._report.revision == job.input_revision
                and job._report.worker_confirmed_revision is None
                and not job._report.reviewed
                and job._report.analysis_status != "dismissed"
            ):
                job._report.analysis_status = "superseded"
            elif live and job.kind == "ticket_analysis":
                await mark_live_ticket_superseded(db, ticket, job)
            return False
        if error == "skipped_short" and live:
            end_attempt(attempt, stamp, error, metadata=metadata)
            complete_job(job, stamp, "succeeded", error)
            set_status(ticket, job, "skipped_short")
            await preserve_successor_status(db, ticket, job)
            audit(
                db, ticket, stamp,
                "report_analysis_skipped_short" if job.report_id else "analysis_skipped_short",
                "입력이 짧아 AI 분석을 생략했어요 · 직접 확인 필요",
            )
            return False
        if error == "input_unchanged" and live and job.kind == "ticket_analysis":
            end_attempt(attempt, stamp, error, metadata=metadata)
            complete_job(job, stamp, "superseded", error)
            await mark_live_ticket_superseded(db, ticket, job)
            return False
        if error:
            end_attempt(attempt, stamp, error, metadata=metadata)
            job.error_code = error
            job.lease_token, job.lease_expires_at = None, None
            retryable = error in LIVE_RETRYABLE if live else error not in NON_RETRYABLE
            attempt_limit = LIVE_MAX_ATTEMPTS if live else MAX_ATTEMPTS
            if job.attempt_count >= attempt_limit or not retryable:
                complete_job(job, stamp, "failed", error)
                set_status(ticket, job, "failed")
                audit(
                    db,
                    ticket,
                    stamp,
                    failed_event(job, live),
                    "오늘 AI 사용 한도에 도달했어요. 직접 확인해 주세요."
                    if error in {"daily_budget_exceeded", "daily_cap_exceeded"}
                    else "AI 분석 불가 · 재시도 안 함 · 사람 확인 필요"
                    if not retryable
                    else "AI 분석 시도 소진 · 사람 확인 필요"
                    if live
                    else "재생 분석 시도 소진 · 사람 확인 필요",
                )
            else:
                job.state = "retry_wait"
                set_status(ticket, job, "retry_wait")
                job.next_attempt_at = stamp + timedelta(seconds=2**job.attempt_count)
            await preserve_successor_status(db, ticket, job)
            return False
        if job.kind == "report_analysis":
            structure = validate_report_result(result, live)
            report = job._report
            report.draft_data = structure.model_dump()
            report.revision += 1
            report.analysis_status = "drafted" if live else "replayed"
            if (live and attempt and (metadata or {}).get("sent") is True
                    and (attempt.metadata_json or {}).get("cache_key")):
                attempt.metadata_json = {
                    **attempt.metadata_json, "cache_result": structure.model_dump(),
                }
            end_attempt(attempt, stamp, None, applied=True, metadata=metadata)
            complete_job(job, stamp, "succeeded")
            audit(
                db,
                ticket,
                stamp,
                "report_analysis_reused" if live and (metadata or {}).get("cache_hit")
                else "report_analysis_drafted" if live else "report_analysis_succeeded",
                "같은 내용의 이전 AI 보고 초안 재사용 · 작업자 확인 필요"
                if live and (metadata or {}).get("cache_hit")
                else "AI 실호출 보고 초안 · 작업자 확인 필요 · 사진 비교 미실행"
                if live
                else "보고 정보 미확인 재생 초안 · 작업자 확인 필요 · 사진 비교 미실행",
            )
            return True
        if live:
            edited = await db.scalar(select(TicketEvent.id).where(
                TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "edited"
            ).limit(1))
            applied = apply_live_ticket(
                db, ticket, job, attempt, stamp, result, lease, metadata, bool(edited)
            )
            await preserve_successor_status(db, ticket, job)
            return applied
        # Defense against a broken/custom gateway implementation at the job boundary.
        settings = db.info.get("settings")
        context = await history_context(db, ticket, settings)
        if context != lease.history_replay_context:
            raise ValueError('history_replay_context_changed')
        validated = validate_ticket_replay(result, lease.text, context)
        if settings and settings.planning_dataset_provider == "db":
            # Replay categories belong to the fixed legacy catalog. They cannot
            # establish meaning in an independently activated school catalog.
            ticket.category_code = "UNKNOWN"
            ticket.catalog_revision = None
            ticket.catalog_snapshot = {}
        else:
            ticket.category_code = validated.category_code
        safety = dict(ticket.safety_data)
        safety["ai_urgency"] = validated.ai_urgency
        safety["automatic_urgency"] = min(
            validated.ai_urgency if validated.ai_urgency is not None else 3,
            safety.get("rule_floor", 3),
        )
        safety["assessment_status"] = (
            "replay_with_rules" if validated.ai_urgency is not None else "rules_only"
        )
        safety["emergency_banner"] = any(
            has_current_hazard(part) for part in safety_evidence_parts(ticket)
        )
        ticket.safety_data = safety
        if ticket.override_reason is None:
            ticket.effective_urgency = safety["automatic_urgency"]
        ticket.safety_version += 1
        ticket.location_version += 1
        ticket.location_data = {**ticket.location_data, "version": ticket.location_version}
        ticket.routing_data = suggest(
            ticket.category_code, ticket.location_data, safety, ticket.catalog_snapshot
        )
        matched = result.replay_match == "matched_replay"
        store_followup(db, ticket, stamp, result, lease.text)
        ticket.ai_status = "replayed" if matched else "unknown"
        end_attempt(attempt, stamp, None, applied=True, metadata=metadata)
        complete_job(job, stamp, "succeeded")
        audit(
            db,
            ticket,
            stamp,
            "analysis_matched_replay" if matched else "analysis_unmatched",
            "고정 합성 예시 재생 결과 · AI 실호출 아님 · 사람 확인 필요"
            if matched
            else "일치하는 재생 예시 없음 · AI 판단 미정 · 사람 확인 필요",
        )
        return True


def category_name(code, categories):
    names = dict(llm._allowed_categories(categories))
    return names.get(code) or CATEGORY_MAP.get(code, CATEGORY_MAP["UNKNOWN"])["label"]


def store_followup(db, ticket, stamp, result, input_text, *, reused=False):
    if ticket.ai_followup_data is not None or ticket.status != "received":
        return
    questions = clean_questions(list(result.followup_questions))
    if result.mode == "replay":
        # Exact fixed fixture is checked a second time at the job boundary.
        expected = "화장실 세면대 아래에서 물이 떨어집니다."
        if input_text != expected:
            return
        questions = clean_questions([{"kind": "since_when",
            "question": "언제부터 물이 떨어지기 시작했나요?", "options": []}])
    if not questions:
        return
    ticket.ai_followup_data = {"source": result.mode, "reused": reused,
        "provenance": "synthetic-fq-water-v1" if result.mode == "replay" else "live-analysis",
        "status": "pending", "questions": [
        {"id": f"q{index}", **question} for index, question in enumerate(questions, 1)
    ], "answers": []}
    description = "재사용" if reused else "실호출" if result.mode == "live" else "재생"
    source = "reused" if reused else result.mode
    audit(db, ticket, stamp, "ai_followup_proposed_" + source,
          f"AI 초안 · {description} 추가 질문 {len(questions)}개 제안")


def draft_image_evidence(metadata, *, followup=False, vision_configured=None):
    if not metadata or metadata.get("sent") is not True:
        return {}
    count = metadata.get("image_count")
    if type(count) is not int or not 0 <= count <= llm.MAX_IMAGES:
        return {}
    if count:
        return {"image_count": count}
    skipped = metadata.get("images_skipped")
    if followup:
        reason = "followup_text_only"
    elif skipped == "vision_model_unconfigured" or vision_configured is False:
        reason = "vision_unconfigured"
    elif skipped in ("image_rejected", "image_resize_failed"):
        reason = "preparation_failed"
    elif vision_configured is True:
        reason = "not_available"
    else:
        reason = None
    return {"image_count": 0, "image_skip_reason": reason}


def apply_live_ticket(db, ticket, job, attempt, stamp, result, lease, metadata, edited=False):
    output = validate_ticket_live(result, lease)
    reused = (metadata or {}).get("cache_hit") is True
    store_followup(db, ticket, stamp, result, lease.text, reused=reused)
    saved = (attempt.metadata_json or {}) if attempt else {}
    draft = {
        "mode": "live", "category_code": output.category_code,
        "category_name": category_name(output.category_code, lease.categories),
        "ai_urgency": output.ai_urgency, "summary": output.summary,
        "urgency_reason": output.urgency_reason,
        "model": result.model or (metadata or {}).get("model"),
        "prompt_version": result.prompt_version, "analysis_revision": job.input_revision,
        "input_hash": saved.get("input_hash"),
        "location_input_hash": saved.get("location_input_hash"),
        "selected_image_digests": saved.get("selected_image_digests", []),
    }
    if reused:
        draft["reused"] = True
    else:
        vision = getattr(db.info.get("settings"), "openai_model_vision", None)
        draft.update(draft_image_evidence(
            metadata, followup=lease.has_followup,
            vision_configured=bool(vision.strip()) if isinstance(vision, str) else None,
        ))
    if (attempt and (metadata or {}).get("sent") is True
            and (attempt.metadata_json or {}).get("cache_key")
            and not output.rule_signal_assessment and not output.hazard_signals):
        attempt.metadata_json = {
            **attempt.metadata_json, "cache_result": {
                **output.model_dump(), "followup_questions": list(result.followup_questions),
            },
        }
    safety = dict(ticket.safety_data)
    late = ticket.status != "received"
    if late:
        draft["late"] = True
        draft["more_urgent"] = output.ai_urgency < ticket.effective_urgency
        safety["ai_draft"] = draft
        ticket.safety_data = safety
    else:
        if ticket.category_code == "UNKNOWN" or not edited:
            settings = db.info.get("settings")
            if settings and settings.planning_dataset_provider == "db":
                ticket.category_code = "UNKNOWN"
                ticket.catalog_revision = None
                ticket.catalog_snapshot = {}
            else:
                ticket.category_code = output.category_code
        safety["ai_urgency"] = output.ai_urgency
        floor = safety.get("rule_floor")
        if type(floor) is int and 0 <= floor <= 3:
            metadata = {**(metadata or {}), "rule_disagreement": floor < output.ai_urgency}
        # Later evidence is durable and must survive subsequent AI reanalysis.
        # Recompute from the existing evidence fields for older tickets too.
        followup_texts = [
            item["text"] for item in (ticket.location_history or [])
            if item.get("kind") != "initial" and item.get("text")
        ]
        followup_texts += list(getattr(ticket, "information_safety_evidence", None) or [])
        followup_floor = min(
            (inspect_raw_text(text).rule_floor for text in followup_texts), default=3
        )
        guard = assess_live_urgency(lease.text, output.ai_urgency,
                                    [x.model_dump() for x in output.rule_signal_assessment])
        safety["rule_disagreement"] = guard.rule_disagreement
        safety["rule_guard_kept"] = guard.rule_kept
        safety["automatic_urgency"] = min(guard.urgency, followup_floor)
        safety["assessment_status"] = "live_priority"
        safety["emergency_banner"] = any(
            has_current_hazard(part) for part in safety_evidence_parts(ticket)
        )
        safety["ai_draft"] = draft
        ticket.safety_data = safety
        if ticket.override_reason is None:
            ticket.effective_urgency = safety["automatic_urgency"]
        ticket.safety_version += 1
        ticket.location_version += 1
        ticket.location_data = {**ticket.location_data, "version": ticket.location_version}
        ticket.routing_data = suggest(
            ticket.category_code, ticket.location_data, safety, ticket.catalog_snapshot
        )
    if not late and safety.get("rule_guard_kept"):
        audit(db, ticket, stamp, "safety_rule_guard", "안전 규칙 · 원문 위험 신호 유지")
    ticket.ai_status = "drafted"
    end_attempt(attempt, stamp, None, applied=True, metadata=metadata)
    complete_job(job, stamp, "succeeded")
    audit(
        db, ticket, stamp,
        "analysis_reused" if draft.get("reused") else
        "analysis_drafted_late" if late else "analysis_drafted",
        "같은 내용의 이전 AI 초안 재사용 · 사람 확인 필요"
        if draft.get("reused") else "AI 실호출 초안 · 사람 확인 필요",
    )
    return True


async def run_job(database, job_id, *, settings, gateway=None, admit_job=None):
    live = not settings.llm_fake
    lease = await acquire_lease(database, uuid.UUID(str(job_id)), live=live, admit_job=admit_job)
    if lease is None:
        return
    result, error, metadata = None, None, None
    try:
        if not settings.llm_fake:
            result = await run_live(database, lease, settings, gateway)
        elif gateway:
            result = await gateway(lease.text)
        elif lease.kind == "report_analysis":
            result = await llm.draft_report(
                lease.text,
                lease.report_result,
                policy=llm.GatewayPolicy(llm_fake=settings.llm_fake),
            )
        else:
            result = await llm.draft_ticket(
                lease.text, policy=llm.GatewayPolicy(
                    llm_fake=settings.llm_fake,
                    booth_history_replay=lease.history_replay_context is not None,
                ),
                history_replay_context=lease.history_replay_context,
            )
        metadata = attempt_metadata(
            lease.kind, live, asdict(result.metadata) if result.metadata else None,
            protocol=getattr(settings, "llm_protocol", "v1"),
        )
        if lease.kind == "report_analysis":
            validate_report_result(result, not settings.llm_fake)
        elif not settings.llm_fake:
            validate_ticket_live(result, lease)
        else:
            validate_ticket_replay(result, lease.text, lease.history_replay_context)
        if result.mode != ("replay" if settings.llm_fake else "live"):
            raise ValueError("unexpected_result_mode")
    except llm.GatewayError as exc:
        error, metadata = (
            exc.metadata.error,
            attempt_metadata(lease.kind, live, asdict(exc.metadata),
                             protocol=getattr(settings, "llm_protocol", "v1")),
        )
    except Exception:
        error = "gateway_failed"
        if metadata is None:
            metadata = attempt_metadata(lease.kind, live, None,
                                        protocol=getattr(settings, "llm_protocol", "v1"))
        metadata["error"] = error
    # CancelledError deliberately leaves a leased attempt for DB-clock expiry recovery.
    await finish_lease(database, lease, result, error=error, metadata=metadata, live=live)


async def run_live(database, lease, settings, gateway):
    """Live attempt. Incomplete configuration fails closed before any transport."""
    policy = llm.policy_from_settings(settings)
    report = lease.kind == "report_analysis"
    prompt_version, schema_version = llm.live_versions(lease.kind, policy.llm_protocol)
    synthetic_denied = settings.booth_synthetic_live_only and not allowed_live_input(
        lease.kind, lease.text, lease.report_result
    )
    booth_denied = settings.booth_free_input_live and (
        lease.kind != "ticket_analysis" or lease.has_followup or not lease.booth_live_intake
    )
    if synthetic_denied or booth_denied:
        raise llm.GatewayError(llm.LiveMetadata(
            prompt_version=prompt_version, schema_version=schema_version,
            protocol=policy.llm_protocol, kind=lease.kind,
            error="booth_input_not_allowed", sent=False,
        ))
    if llm.runtime_mode(settings) != "live":
        raise llm.GatewayError(
            llm.LiveMetadata(
                prompt_version=prompt_version,
                schema_version=schema_version,
                protocol=policy.llm_protocol,
                error="live_processing_unavailable",
            )
        )
    images = []
    if (not report and not lease.has_followup and lease.ticket_id is not None
            and policy.live_model(vision=True)):
        try:
            images = await approved_ticket_images(database, lease.ticket_id, settings)
        except Exception:
            images = []
    usable, _ = llm._usable_images(images)
    short = len(lease.text) < 15 if report else len("".join(lease.text.split())) < 4
    if short and (report or (not usable and lease.rule_floor != 0)):
        raise llm.GatewayError(llm.LiveMetadata(
            prompt_version=prompt_version, schema_version=schema_version,
            protocol=policy.llm_protocol,
            kind=lease.kind, error="skipped_short", skipped="short_input", sent=False,
        ))
    if gateway:
        # Test-only injected gateways do not send externally; still bind their input
        # to the attempt so the same response-time stale rules are exercised.
        if not report:
            async with database.transaction() as db:
                ticket, job = await locked_job(db, lease.job_id)
                if (not job or job.state != "running" or job.lease_token != lease.token
                        or stale(ticket, job, True) or analysis_text(ticket) != lease.text):
                    raise llm.GatewayError(llm.LiveMetadata(
                        prompt_version=prompt_version, schema_version=schema_version,
                        protocol=policy.llm_protocol, error="lease_lost",
                    ))
                attempt = await current_attempt(db, job)
                snapshot = await approved_image_snapshot(db, ticket.id)
                attempt.metadata_json = {
                    **(attempt.metadata_json or {}),
                    "input_hash": input_fingerprint(db, analysis_text(ticket), snapshot, []),
                    "location_input_hash": input_fingerprint(db, ticket.location_text, [], []),
                    "selected_image_digests": [],
                }
        return await gateway(lease.text)

    async def before_send(
        request_bytes, model, image_count, output_cap, image_digests, transfer_digests=None
    ):
        return await reserve_budget(
            database, lease, policy, request_bytes, model, image_count, output_cap,
            image_digests, transfer_digests,
        )

    async def cache_lookup(body, originals, transferred):
        return await lookup_cache(database, lease, policy, body, originals, transferred)

    if report:
        return await llm.draft_report(
            lease.text, lease.report_result, mode="live", policy=policy, before_send=before_send,
            cache_lookup=None if settings.booth_synthetic_live_only else cache_lookup,
        )
    categories = list(lease.categories) if lease.categories is not None else None
    if settings.booth_synthetic_live_only or settings.booth_free_input_live:
        # Never send catalog labels supplied by an editable school dataset.
        allowed_codes = {code for code, _ in lease.categories or ()}
        categories = [(item["code"], item["label"]) for item in CATEGORIES
                      if item["code"] in allowed_codes]
    return await llm.draft_ticket(
        lease.text,
        mode="live",
        policy=policy,
        categories=categories,
        images=images,
        before_send=before_send,
        require_image=short and lease.rule_floor != 0,
        cache_lookup=None if settings.booth_synthetic_live_only else cache_lookup,
    )


def queue_name(settings):
    return f"baroai:{settings.app_env}:ticket-analysis"


def json_serialize(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def json_deserialize(value):
    return json.loads(value)


async def dispatch_once(database, redis, *, queue_name, job_ids=None):
    async with database.transaction() as db:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        stamp = await db_clock(db)
        if not db.info["settings"].llm_fake:
            waiting_query = select(ProcessingJob.id).where(
                ProcessingJob.kind == "ticket_analysis", ProcessingJob.state == "queued",
                ProcessingJob.attempt_count == 0, ProcessingJob.next_attempt_at > stamp,
            )
            if job_ids is not None:
                waiting_query = waiting_query.where(ProcessingJob.id.in_(job_ids))
            if db.info["settings"].deployment_mode == "public_demo":
                from app.tickets import public_visible_ticket

                waiting_query = waiting_query.where(
                    ProcessingJob.ticket_id.in_(select(Ticket.id).where(public_visible_ticket(stamp)))
                )
            waiting = (await db.scalars(
                waiting_query.order_by(ProcessingJob.created_at).limit(100)
            )).all()
            for waiting_id in waiting:
                waiting_ticket, waiting_job = await locked_job(db, waiting_id)
                if waiting_ticket and waiting_job:
                    await release_photo_wait(db, waiting_ticket, waiting_job)
        query = (
            select(ProcessingJob.id)
            .where(
                or_(
                    (ProcessingJob.state == "queued")
                    & or_(ProcessingJob.next_attempt_at.is_(None),
                          ProcessingJob.next_attempt_at <= stamp),
                    (ProcessingJob.state == "retry_wait")
                    & (ProcessingJob.next_attempt_at <= stamp),
                    (ProcessingJob.state == "running") & (ProcessingJob.lease_expires_at <= stamp),
                ),
                or_(
                    ProcessingJob.last_enqueued_at.is_(None),
                    ProcessingJob.last_enqueued_at <= stamp - timedelta(seconds=REDISPATCH_SECONDS),
                ),
            )
            .order_by(ProcessingJob.created_at)
            .limit(100)
        )
        if job_ids is not None:
            query = query.where(ProcessingJob.id.in_(job_ids))
        if db.info["settings"].deployment_mode == "public_demo":
            from app.tickets import public_visible_ticket

            query = query.where(
                ProcessingJob.ticket_id.in_(select(Ticket.id).where(public_visible_ticket(stamp)))
            )
        ids = list((await db.scalars(query)).all())
    count = 0
    for job_id in ids:
        # Fresh dispatch IDs avoid retained ARQ result keys suppressing recovery.
        await redis.enqueue_job(
            "analyze_ticket",
            str(job_id),
            _queue_name=queue_name,
            _job_id=f"{queue_name}:{job_id}:{uuid.uuid4().hex}",
        )
        async with database.transaction() as db:
            from app.dispatch_lock import dispatch_lock

            await dispatch_lock(db)
            ticket, job = await locked_job(db, job_id)
            if job and job.state not in TERMINAL:
                job.last_enqueued_at = await db_clock(db)
        count += 1
    return count


async def analyze_ticket(ctx, job_id):
    await run_job(ctx["database"], job_id, settings=ctx["settings"], admit_job=ctx.get("admit_job"))


def redis_settings(settings):
    if not settings.redis_url:
        raise ValueError("redis_configuration_required")
    result = RedisSettings.from_dsn(settings.redis_url.get_secret_value())
    result.conn_retries, result.conn_timeout = 0, 1
    return result


def make_worker(settings, *, burst=False, admit_job=None, handle_signals=True):
    database = Database(settings)

    async def check_namespace(ctx):
        if settings.deployment_mode == "service":
            from app.auth_service import ensure_namespace

            async with database.transaction() as db:
                from app.dispatch_lock import dispatch_lock

                await dispatch_lock(db)
                await ensure_namespace(db, settings)

    async def close_database(ctx):
        await database.close()

    return Worker(
        [analyze_ticket],
        handle_signals=handle_signals,
        redis_settings=redis_settings(settings),
        queue_name=queue_name(settings),
        health_check_key=f"{queue_name(settings)}:health",
        ctx={"database": database, "settings": settings, "admit_job": admit_job},
        on_startup=check_namespace,
        on_shutdown=close_database,
        job_serializer=json_serialize,
        job_deserializer=json_deserialize,
        retry_jobs=False,
        max_tries=1,
        keep_result=0,
        # Replay keeps the short bound; live needs the provider timeout plus margin.
        job_timeout=10 if settings.llm_fake else int(settings.llm_timeout_s) + 20,
        max_jobs=4,
        burst=burst,
        log_results=False,
    )


def load_settings(test=False):
    if test and isolated_settings_requested():
        raise ValueError("isolated_settings_test_mode_forbidden")
    if not test:
        return Settings()
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    target, redis = values.get("TEST_DATABASE_URL"), values.get("TEST_REDIS_URL")
    if not target or not redis:
        raise ValueError("test_targets_required")
    db_url, redis_url = make_url(target), make_url(redis)
    dev = values.get("DATABASE_URL")
    if not db_url.database or not db_url.database.endswith("_test"):
        raise ValueError("isolated_test_database_required")
    if dev:
        dev_url = make_url(dev)
        if (db_url.host, db_url.port or 5432, db_url.database) == (
            dev_url.host,
            dev_url.port or 5432,
            dev_url.database,
        ):
            raise ValueError("isolated_test_database_required")
    dev_redis = make_url(values["REDIS_URL"]) if values.get("REDIS_URL") else None
    if redis_url.database != "1" or (
        dev_redis
        and (redis_url.host, redis_url.port or 6379, redis_url.database)
        == (dev_redis.host, dev_redis.port or 6379, dev_redis.database)
    ):
        raise ValueError("isolated_test_redis_required")
    return Settings(
        _env_file=None,
        database_url=target,
        redis_url=redis,
        app_env="test",
        llm_fake=values.get("LLM_FAKE", "true"),
        demo_auth=False,
    )


async def dispatch_loop(settings, *, once=False):
    database = Database(settings)
    try:
        if settings.deployment_mode == "service":
            from app.auth_service import ensure_namespace

            async with database.transaction() as db:
                from app.dispatch_lock import dispatch_lock

                await dispatch_lock(db)
                await ensure_namespace(db, settings)
        next_coordinate_cleanup = 0.0
        while True:
            if (
                settings.deployment_mode == "service"
                and time.monotonic() >= next_coordinate_cleanup
            ):
                from app.photo_coordinates import purge_photo_coordinates

                try:
                    await purge_photo_coordinates(database)
                except Exception:
                    logger.error("photo_coordinate_cleanup_unavailable")
                next_coordinate_cleanup = time.monotonic() + 300
            redis = None
            try:
                redis = await create_pool(
                    redis_settings(settings),
                    job_serializer=json_serialize,
                    job_deserializer=json_deserialize,
                    default_queue_name=queue_name(settings),
                )
                count = await dispatch_once(database, redis, queue_name=queue_name(settings))
                logger.info("dispatch_complete count=%d", count)
            except Exception:
                logger.error("dispatch_unavailable")
                if once:
                    raise RuntimeError("dispatch_unavailable") from None
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
    parser.add_argument("command", choices=["worker", "dispatch"])
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--burst", action="store_true")
    args = parser.parse_args()
    try:
        settings = load_settings(args.test)
        if args.command == "worker":
            make_worker(settings, burst=args.burst).run()
        else:
            asyncio.run(dispatch_loop(settings, once=args.once))
    except Exception:
        raise SystemExit("job_runtime_unavailable") from None


if __name__ == "__main__":
    main()
