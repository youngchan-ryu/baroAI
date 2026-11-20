"""Seed the supervisor-owned booth through product APIs; never target a service DB.

The supervisor must verify that the API listener it passes is its own child and
provide its exact generation environment. Replay is only for isolated rehearsal.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.booth_fixtures import (
    HISTORY_MERGE,
    HISTORY_TEMPLATES,
    LIVE_HISTORY,
    REPORT_TEXT,
    SCENARIOS,
)
from app.booth_showcase import (
    SHOWCASE,
    SHOWCASE_ANSWERED_REQUEST,
    SHOWCASE_FINAL_REPORT,
    SHOWCASE_REPORTS,
    SHOWCASE_URGENCY_OVERRIDES,
    ShowcaseReport,
)
from app.config import Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.location_workflow import refresh_safety
from app.models import CompletionReport, Ticket, TicketEvent
from app.services.safety import has_current_hazard


def history_scenarios():
    """Fictional replay-only cases; never part of the live-input allowlist."""
    templates = HISTORY_TEMPLATES
    rows = []
    for index in range(32):
        location, code, category, worker, description = templates[
            index % len(templates)
        ]
        if index == 0:
            description = "분전함에서 연기가 보여 확인을 요청합니다."
        elif index == 1:
            description = "콘센트에서 스파크가 보여 확인을 요청합니다."
        if index in (24, 25):
            worker = ("worker_g", "worker_f")[index - 24]
            if index == 24:
                location, code, _, _, description = templates[1]
                category = "PLUMBING"
        target = (
            "completed"
            if index < 24
            else "report"
            if index < 26
            else "temporary"
            if index == 26
            else "revisit"
            if index == 27
            else "queued"
        )
        if index >= 28:
            worker = ("worker_a", "worker_a", "worker_a", "worker_d")[index - 28]
            location, code, category, _, description = next(
                t for t in templates if t[3] == worker
            )
        rows.append(
            (
                f"history-{index:02}",
                location,
                code,
                description,
                category,
                target,
                worker,
            )
        )
    rows.extend(
        (
            (
                "history-transfer",
                "관악 301동",
                "GWANAK_301",
                "교내 다른 부서에 확인이 필요한 시설 문의입니다.",
                "ELEC_LIGHTING",
                "transfer",
                None,
            ),
        )
    )
    return tuple(rows)


def live_history_scenarios():
    """Complete finished visits before leaving other visits active for each worker."""
    rows = sorted(LIVE_HISTORY, key=lambda row: row[5] != "completed")
    return tuple(row[:7] for row in rows)


class Stop(Exception):
    pass


def scenario_date(reference: datetime, index: int) -> datetime:
    """Assign eight new scenarios to three Seoul dates without future timestamps."""
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("reference must be timezone-aware")
    if not 0 <= index < len(SCENARIOS):
        raise ValueError("invalid scenario index")
    local = reference.astimezone(ZoneInfo("Asia/Seoul"))
    capped = min(local, local.replace(hour=12, minute=0, second=0, microsecond=0))
    days_ago = 0 if index < 3 else 1 if index < 6 else 2
    return (capped - timedelta(days=days_ago)).astimezone(UTC)


SHOWCASE_DAY_SLOTS = {
    "showcase-done-a": (6, 9, 14),
    "showcase-done-b": (4, 10, 31),
    "showcase-temporary": (2, 11, 47),
    "showcase-report-a": (5, 9, 42),
    "showcase-report-b": (3, 9, 24),
    "showcase-progress-a": (1, 8, 35),
    "showcase-progress-b": (1, 8, 42),
    "showcase-queued-a": (1, 11, 18),
    "showcase-queued-b": (1, 9, 42),
    "showcase-confirm-b": (1, 9, 12),
    "showcase-info": (1, 9, 8),
}

SHOWCASE_REVISION = 3
LEGACY_SHOWCASE_REVISION = 2
LEGACY_SHOWCASE_ANSWER = "오늘 오전부터 확인했습니다."
LEGACY_SHOWCASE_OVERRIDE = "담당자가 신고 내용을 확인했습니다"


def showcase_followup_answer(name: str, question: dict) -> str | None:
    if name not in {"showcase-progress-a", "showcase-confirm-a"}:
        return None
    kind, wording = question["kind"], question["question"]
    wet_floor = name == "showcase-progress-a"
    if re.search(r"언제|부터|얼마나 됐", wording) or kind == "since_when":
        return "처음 확인한 건 약 30분 전입니다." if wet_floor else "점심 직전에 처음 확인했습니다."
    if re.search(r"바닥|고이|고여|미끄", wording):
        return (
            "네, 세면대 앞 바닥에 손바닥 두 뼘 정도 물이 고여 있습니다."
            if wet_floor else "아니요. 연결부 표면에만 물방울이 맺혀 있고 바닥은 젖지 않았습니다."
        )
    if re.search(r"계속|지금|떨어지|멈|흐르", wording) or kind == "still_ongoing":
        return (
            "네, 세면대를 쓰지 않을 때도 물방울이 몇 초에 한 번 떨어집니다."
            if wet_floor else "지금도 연결부 표면에 물방울이 맺혀 있지만 바닥으로 떨어지지는 않습니다."
        )
    if kind == "scope" and re.search(r"세면대|연결부|배관|물방울", wording):
        return (
            "세면대 한 곳에서만 떨어지고 다른 세면대는 마른 상태입니다."
            if wet_floor else "세면대 한 곳의 배관 연결부에만 물방울이 맺혀 있습니다."
        )
    return None


def showcase_ticket_ids(state: dict) -> dict[str, uuid.UUID]:
    items = state["items"]
    if not isinstance(items, dict) or set(items) != {row[0] for row in SHOWCASE}:
        raise Stop("state_identity")
    ids = {name: uuid.UUID(item["ticket"]) for name, item in items.items() if item.get("dated") is True}
    if len(ids) != len(items) or len(set(ids.values())) != len(ids):
        raise Stop("state_identity")
    return ids


async def lock_showcase_rows(session, ids: dict[str, uuid.UUID]) -> dict[str, Ticket]:
    from app.models import User

    rows = (await session.scalars(
        select(Ticket).join(User, Ticket.reporter_id == User.id)
        .where(User.persona == "reporter_a", Ticket.id.in_(ids.values()))
        .order_by(Ticket.id).with_for_update(of=Ticket)
    )).all()
    by_id = {ticket.id: ticket for ticket in rows}
    tickets = {name: by_id.get(ticket_id) for name, ticket_id in ids.items()}
    for name, location, _, description, *_ in SHOWCASE:
        row = tickets.get(name)
        if row is None or (row.description, row.location_text) != (description, location):
            raise Stop("ticket_identity")
    return tickets


def replace_legacy_followup(ticket: Ticket, name: str) -> None:
    data = ticket.ai_followup_data or {}
    evidence = ticket.followup_analysis_evidence or []
    if not any(answer["answer"] == LEGACY_SHOWCASE_ANSWER for answer in data.get("answers", [])):
        return
    questions = {question["id"]: question for question in data["questions"]}
    answers = []
    replacements = {}
    for answer in data["answers"]:
        question_id, value = answer["question_id"], answer["answer"]
        if value == LEGACY_SHOWCASE_ANSWER:
            value = showcase_followup_answer(name, questions[question_id])
            if value is None:
                raise Stop("showcase_followup_unmatched")
            replacements[question_id] = value
        answers.append({**answer, "answer": value})

    updated_evidence = []
    for item in evidence:
        if item.get("source") == "ai_followup" and item.get("source_id") in replacements:
            if item.get("analyze_with_ai") is not False or item.get("answer") != LEGACY_SHOWCASE_ANSWER:
                raise Stop("showcase_ai_input_changed")
            updated_evidence.append({**item, "answer": replacements[item["source_id"]]})
        else:
            updated_evidence.append(item)
    if len(replacements) != sum(
        item.get("source") == "ai_followup" and item.get("source_id") in replacements
        for item in evidence
    ):
        raise Stop("showcase_ai_input_changed")
    # These fixture replies were never opted into AI analysis. Safety still
    # consumes them alongside any separate information requests, in their
    # original order. Replace only the fixture's known answer.
    safety_evidence = list(ticket.information_safety_evidence or [])
    positions = [index for index, value in enumerate(safety_evidence) if value == LEGACY_SHOWCASE_ANSWER]
    if len(positions) != 1 or len(replacements) != 1:
        raise Stop("showcase_ai_input_changed")
    safety_evidence[positions[0]] = next(iter(replacements.values()))
    ticket.ai_followup_data = {**data, "answers": answers}
    ticket.followup_analysis_evidence = updated_evidence
    ticket.information_safety_evidence = safety_evidence
    refresh_safety(ticket)


async def upgrade_showcase_copy(session, ids: dict[str, uuid.UUID]) -> None:
    await dispatch_lock(session)
    tickets = await lock_showcase_rows(session, ids)
    await verify_live_ledger(session, [str(ticket_id) for ticket_id in ids.values()])
    events = (await session.scalars(select(TicketEvent).where(TicketEvent.ticket_id.in_(ids.values())))).all()
    by_id = {ticket.id: ticket for ticket in tickets.values()}
    for event in events:
        if event.event_type == "urgency_overridden":
            detail = json.loads(event.reason)
            if not isinstance(detail, dict) or detail.get("reason") != LEGACY_SHOWCASE_OVERRIDE:
                continue
            ticket = by_id[event.ticket_id]
            if ticket.override_reason != LEGACY_SHOWCASE_OVERRIDE:
                raise Stop("showcase_legacy_override_changed")
            ticket.override_reason = None
            ticket.effective_urgency = ticket.safety_data["automatic_urgency"]
            ticket.safety_version += 1
            # This is a regenerated fixture in a restored copy. Keep any
            # non-fixture audit and all original live attempt records intact.
            await session.delete(event)
        elif event.event_type == "created" and event.reason in {
            "합성 시연 접수 · AI 초안 대기", "합성 시연 접수 · 재생 모드"
        }:
            event.reason = event.reason.removeprefix("합성 시연 ")

    for name in ("showcase-progress-a", "showcase-confirm-a"):
        replace_legacy_followup(tickets[name], name)
    reports = (await session.scalars(select(CompletionReport).where(
        CompletionReport.ticket_id.in_(ids.values())
    ))).all()
    name_by_id = {ticket_id: name for name, ticket_id in ids.items()}
    for report in reports:
        name = name_by_id[report.ticket_id]
        fixture = SHOWCASE_REPORTS.get(name)
        if name == "showcase-temporary" and report.result == "completed":
            fixture = SHOWCASE_FINAL_REPORT
        if (fixture is None or report.text != REPORT_TEXT or report.result != fixture.result
                or report.confirmed_result != fixture.result
                or report.analysis_status != "worker_confirmed"):
            raise Stop("showcase_legacy_report_changed")
        # Keep the original live input, AI output and measurement intact. The
        # restored synthetic copy uses case-specific human-confirmed details.
        details = {**report.confirmed_data, **showcase_report_structure(fixture)}
        report.confirmed_data = details
        if report.reviewed and report.approved_snapshot:
            report.approved_snapshot = {**report.approved_snapshot, "structure": details}
    for ticket in tickets.values():
        safety = ticket.safety_data
        banner = has_current_hazard(safety["source_text"], safety["rule_hits"])
        if safety.get("emergency_banner") != banner:
            ticket.safety_data = {**safety, "emergency_banner": banner}
            ticket.safety_version += 1


async def translate_showcase_dates(session, tickets: dict[str, Ticket], dates: dict,
                                   reference: datetime) -> None:
    """Move each seeded chronology intact. Provider and authentication times stay real."""
    tables = {
        "ticket_events": ("created_at",),
        "assignments": ("created_at", "departed_at", "started_at", "stopped_at", "visit_ended_at"),
        "completion_reports": ("created_at", "superseded_at", "correction_requested_at", "approved_at"),
        "information_requests": ("created_at", "answered_at", "cancelled_at"),
    }
    event_stamps = " UNION ALL ".join(
        f"SELECT max(GREATEST({', '.join(columns)})) AS stamp FROM {table} WHERE ticket_id=:id"
        for table, columns in tables.items()
    )
    latest_event = text(f"SELECT max(stamp) FROM ({event_stamps}) AS stamps")
    for ticket in tickets.values():
        target = dates[str(ticket.id)]
        if target > reference:
            raise Stop("future_showcase_event")
        last_event_at = await session.scalar(latest_event, {"id": ticket.id})
        latest = max(ticket.created_at, ticket.updated_at, last_event_at or ticket.created_at)
        # A saved case can have a longer history than its suggested time slot allows.
        delta = min(target - ticket.created_at, reference - latest)
        for table, columns in tables.items():
            declarations = ", ".join(f"{column}={column} + CAST(:delta AS interval)" for column in columns)
            updated = await session.scalars(
                text(f"UPDATE {table} SET {declarations} WHERE ticket_id=:id "
                     f"RETURNING GREATEST({', '.join(columns)})"),
                {"delta": delta, "id": ticket.id},
            )
            if any(stamp > reference for stamp in updated if stamp is not None):
                raise Stop("future_showcase_event")
        ticket.created_at += delta
        ticket.updated_at += delta


def showcase_date(reference: datetime, index: int) -> datetime:
    """Keep earlier cases in Seoul daytime and the fresh intake queue recent."""
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("reference must be timezone-aware")
    case = SHOWCASE[index]
    slot = SHOWCASE_DAY_SLOTS.get(case[0])
    local = reference.astimezone(ZoneInfo("Asia/Seoul"))
    if slot is None:
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        # The fresh cases have at most six minutes of history, or a 42-minute
        # confirmation. Just after midnight, keep that history on the prior day.
        minimum = timedelta(minutes=42 if case[0] == "showcase-confirm-a" else 7)
        candidate = min(max(midnight, local - timedelta(hours=case[8])), local - minimum)
        return candidate.astimezone(UTC)
    days, hour, minute = slot
    return (local - timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).astimezone(UTC)


def key(name: str, action: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"baro-booth-scenario-v1/{name}/{action}").hex


def save(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def photo_bytes(*, after: bool = False) -> bytes:
    image = Image.new("RGB", (640, 480), "#e1ded7")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 330, 640, 480), fill="#a8a49d")
    if not after:
        draw.ellipse((220, 130, 405, 320), fill="#c1ad91")
        draw.ellipse((260, 160, 390, 285), fill="#b3a085")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85)
    return output.getvalue()


def validate(settings: Settings, generation: str, port: int, replay: bool) -> None:
    url = make_url(settings.database_url.get_secret_value())
    name = "booth_" + generation
    redis = (
        urlsplit(settings.redis_url.get_secret_value()) if settings.redis_url else None
    )
    if (
        not re.fullmatch(r"[0-9a-f]{32}", generation)
        or port in {8000, 8001, 8012}
        or not 1024 <= port <= 65535
        or settings.deployment_mode != "demo"
        or settings.app_env != "dev"
        or not settings.demo_auth
        or settings.mail_delivery != "disabled"
        or settings.planning_dataset_provider != "db"
        or settings.openai_model_vision
        or settings.llm_fake != replay
        or (not replay and not settings.booth_synthetic_live_only)
        or os.environ.get("BARO_ISOLATED_SETTINGS") != "1"
        or os.environ.get("BARO_CAMPUS_OWNED_SUPERVISOR") != "1"
        or url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.username != name
        or url.database != name
        or url.query
        or not url.port
        or url.port in {55432, 56431}
        or not redis
        or redis.scheme != "redis"
        or redis.hostname != "127.0.0.1"
        or not redis.port
        or redis.port in {6379}
        or redis.port == url.port
        or redis.path != "/0"
        or redis.query
        or Path(os.environ.get("HOME", "")).name != generation
    ):
        raise Stop("owned_demo_required")


def valid_live_attempt(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    if (
        metadata.get("sent") is not True
        or metadata.get("usage_valid") is not True
        or metadata.get("error") is not None
        or not isinstance(metadata.get("model"), str)
        or not metadata["model"].strip()
        or type(metadata.get("image_count")) is not int
        or metadata["image_count"] != 0
        or metadata.get("transfer_image_digests") not in (None, [])
    ):
        return False
    if any(
        type(metadata.get(name)) is not int or metadata[name] < 0
        for name in ("input_tokens", "output_tokens")
    ):
        return False
    if not isinstance(metadata.get("reservation_day"), str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", metadata["reservation_day"]
    ):
        return False
    reserved = metadata.get("reserved_usd")
    if isinstance(reserved, bool) or not isinstance(reserved, (str, int, float)):
        return False
    value = metadata.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        cost = Decimal(str(value))
        reserve = Decimal(str(reserved))
        return cost.is_finite() and cost > 0 and reserve.is_finite() and reserve > 0
    except (InvalidOperation, ValueError):
        return False


async def verify_live_ledger(
    session, ticket_ids: list[str], report_ids: list[tuple[str, str, int]] | None = None
) -> None:
    try:
        ids = [uuid.UUID(value) for value in ticket_ids]
    except (TypeError, ValueError, AttributeError):
        raise Stop("live_ledger_invalid") from None
    if not ids or len(set(ids)) != len(ids):
        raise Stop("live_ledger_invalid")
    rows = (
        await session.execute(
            text(
                "SELECT t.id, t.safety_data, j.input_revision, j.state, "
                "a.mode, a.ended_at, a.error_code, a.applied, a.metadata_json "
                "FROM tickets t LEFT JOIN processing_jobs j ON j.ticket_id=t.id "
                "AND j.kind='ticket_analysis' AND j.report_id IS NULL "
                "LEFT JOIN processing_attempts a ON a.job_id=j.id WHERE t.id = ANY(:ids)"
            ),
            {"ids": ids},
        )
    ).all()
    verified = set()
    for tid, safety, revision, state, mode, ended, error, applied, metadata in rows:
        draft = safety.get("ai_draft") if isinstance(safety, dict) else None
        if (
            isinstance(draft, dict)
            and draft.get("mode") == "live"
            and type(draft.get("analysis_revision")) is int
            and draft["analysis_revision"] == revision
            and state == "succeeded"
            and mode == "live"
            and ended is not None
            and error is None
            and applied is True
            and valid_live_attempt(metadata)
        ):
            verified.add(tid)
    if verified != set(ids):
        raise Stop("live_ledger_invalid")
    if report_ids is not None:
        try:
            expected = {(uuid.UUID(tid), uuid.UUID(rid), revision)
                        for tid, rid, revision in report_ids
                        if type(revision) is int and revision > 0}
        except (TypeError, ValueError, AttributeError):
            raise Stop("live_report_ledger_invalid") from None
        if len(expected) != len(report_ids) or len({rid for _, rid, _ in expected}) != len(expected):
            raise Stop("live_report_ledger_invalid")
        reports = (
            await session.execute(
                text(
                    "SELECT j.ticket_id, j.report_id, j.input_revision, r.revision, r.draft_data, "
                    "j.state, a.mode, a.ended_at, a.error_code, a.applied, a.metadata_json "
                    "FROM processing_jobs j JOIN completion_reports r ON r.id=j.report_id "
                    "AND r.ticket_id=j.ticket_id JOIN processing_attempts a ON a.job_id=j.id "
                    "WHERE j.report_id = ANY(:ids) AND j.kind='report_analysis'"
                ),
                {"ids": [rid for _, rid, _ in expected]},
            )
        ).all()
        valid = {
            (tid, rid, revision)
            for tid, rid, revision, current_revision, draft, state, mode, ended, error, applied, meta in reports
            if type(current_revision) is int and current_revision > revision
            and isinstance(draft, dict) and bool(draft)
            and state == "succeeded" and mode == "live" and ended is not None
            and error is None and applied is True and valid_live_attempt(meta)
        }
        if valid != expected:
            raise Stop("live_report_ledger_invalid")


async def identity_and_dates(
    settings: Settings,
    generation: str,
    dates: dict | None = None,
    photo_ticket: str | None = None,
    live_tickets: list[str] | None = None,
    history: bool = False,
    live_reports: list[tuple[str, str, int]] | None = None,
    showcase: bool = False,
    showcase_reference: datetime | None = None,
    showcase_ids: dict[str, uuid.UUID] | None = None,
    upgrade_showcase: bool = False,
    rebase_showcase: bool = False,
) -> dict:
    db = Database(settings)
    try:
        async with db.transaction() as session:
            row = (
                await session.execute(
                    text("SELECT current_database(), current_user, inet_server_port()")
                )
            ).one()
            url = make_url(settings.database_url.get_secret_value())
            if row != ("booth_" + generation, "booth_" + generation, url.port):
                raise Stop("database_identity")
            identity = (
                await session.execute(
                    text("SELECT mode, namespace FROM deployment_identity WHERE id=1")
                )
            ).one_or_none()
            if identity != ("demo", "demo"):
                raise Stop("deployment_identity")
            personas = dict(
                (
                    await session.execute(
                        text(
                            "SELECT persona, id FROM users WHERE persona IN "
                            "('worker_a','worker_b','worker_c','worker_d','worker_e','worker_f','worker_g')"
                        )
                    )
                ).all()
            )
            if set(personas) != {
                "worker_a",
                "worker_b",
                "worker_c",
                "worker_d",
                "worker_e",
                "worker_f",
                "worker_g",
            }:
                raise Stop("worker_identity")
            if photo_ticket:
                attempts = (
                    (
                        await session.execute(
                            text(
                                "SELECT a.metadata_json FROM processing_attempts a "
                                "JOIN processing_jobs j ON j.id=a.job_id "
                                "WHERE j.ticket_id=:id AND j.kind='ticket_analysis' AND a.applied"
                            ),
                            {"id": uuid.UUID(photo_ticket)},
                        )
                    )
                    .scalars()
                    .all()
                )
                if not attempts or any(
                    a.get("image_count") != 0
                    or a.get("transfer_image_digests") not in (None, [])
                    for a in attempts
                ):
                    raise Stop("photo_trace_not_text_only")
            if live_tickets is not None:
                await verify_live_ledger(session, live_tickets, live_reports)
            if upgrade_showcase:
                if not showcase_ids:
                    raise Stop("state_identity")
                await upgrade_showcase_copy(session, showcase_ids)
            if rebase_showcase:
                if not showcase_ids or not showcase_reference or not dates:
                    raise Stop("state_identity")
                await dispatch_lock(session)
                tickets = await lock_showcase_rows(session, showcase_ids)
                await translate_showcase_dates(session, tickets, dates, showcase_reference)
                return {persona: str(user_id) for persona, user_id in personas.items()}
            if dates:
                if showcase:
                    await dispatch_lock(session)
                    if showcase_ids is not None:
                        await lock_showcase_rows(session, showcase_ids)
                for tid, created in dates.items():
                    result = await session.execute(
                        text("UPDATE tickets SET created_at=:created WHERE id=:id"),
                        {"created": created, "id": uuid.UUID(tid)},
                    )
                    if result.rowcount != 1:
                        raise Stop("ticket_identity")
                    if history or showcase:
                        ticket_id = uuid.UUID(tid)
                        visits = []
                        events = []
                        if showcase:
                            visits = (await session.execute(text(
                                "SELECT id FROM assignments WHERE ticket_id=:id ORDER BY created_at, id"
                            ), {"id": ticket_id})).scalars().all()
                            if len(visits) == 2:
                                events = (await session.execute(text(
                                    "SELECT id, event_type FROM ticket_events WHERE ticket_id=:id "
                                    "ORDER BY created_at, id"
                                ), {"id": ticket_id})).all()
                        duration = timedelta(minutes=(180 if showcase else 50) + ticket_id.int % 180)
                        confirm_min = 10 + ticket_id.int % 29 if showcase else 5
                        assign_min = confirm_min + 10 + ticket_id.int % 60 if showcase else 10
                        reported = created + duration - timedelta(minutes=10)
                        requested = created + timedelta(minutes=5 if showcase else 3)
                        answered = created + timedelta(minutes=6 if showcase else 4)
                        await session.execute(
                            text(
                                "UPDATE assignments SET created_at=:assigned, "
                                "started_at=CASE WHEN started_at IS NULL THEN NULL ELSE CAST(:started AS timestamptz) END, "
                                "visit_ended_at=CASE WHEN visit_ended_at IS NULL THEN NULL ELSE CAST(:reported AS timestamptz) END "
                                "WHERE ticket_id=:id"
                            ),
                            {
                                "id": ticket_id,
                                "assigned": created + timedelta(minutes=assign_min),
                                "started": created + timedelta(minutes=assign_min + (5 if showcase else 10)),
                                "reported": reported + timedelta(minutes=1 if showcase else 0),
                            },
                        )
                        await session.execute(
                            text(
                                "UPDATE completion_reports SET created_at=:reported, "
                                "approved_at=CASE WHEN approved_at IS NULL THEN NULL ELSE CAST(:finished AS timestamptz) END "
                                "WHERE ticket_id=:id"
                            ),
                            {
                                "id": ticket_id,
                                "reported": reported,
                                "finished": created + duration,
                            },
                        )
                        await session.execute(
                            text(
                                "UPDATE ticket_events SET created_at=CASE "
                                "WHEN event_type LIKE 'report_analysis_%' THEN CAST(:report_analysis AS timestamptz) "
                                "ELSE CASE event_type "
                                "WHEN 'created' THEN CAST(:created AS timestamptz) "
                                "WHEN 'received' THEN CAST(:received AS timestamptz) "
                                "WHEN 'ai_followup_proposed_live' THEN CAST(:proposed AS timestamptz) "
                                "WHEN 'ai_followup_proposed_replay' THEN CAST(:proposed AS timestamptz) "
                                "WHEN 'ai_followup_skipped' THEN CAST(:followup_answered AS timestamptz) "
                                "WHEN 'ai_followup_answered' THEN CAST(:followup_answered AS timestamptz) "
                                "WHEN 'information_requested' THEN CAST(:requested AS timestamptz) "
                                "WHEN 'information_answered' THEN CAST(:answered AS timestamptz) "
                                "WHEN 'edited' THEN CAST(:edited AS timestamptz) "
                                "WHEN 'confirmed' THEN CAST(:confirmed AS timestamptz) "
                                "WHEN 'assigned' THEN CAST(:assigned AS timestamptz) "
                                "WHEN 'worker_credential_issued' THEN CAST(:credential AS timestamptz) "
                                "WHEN 'started' THEN CAST(:started AS timestamptz) "
                                "WHEN 'reported' THEN CAST(:reported AS timestamptz) "
                                "WHEN 'visit_ended' THEN CAST(:visit_ended AS timestamptz) "
                                "WHEN 'report_confirmed' THEN CAST(:report_confirmed AS timestamptz) "
                                "WHEN 'report_reviewed' THEN CAST(:finished AS timestamptz) "
                                "WHEN 'synthetic_department_guidance' THEN CAST(:confirmed AS timestamptz) "
                                "ELSE CAST(:analyzed AS timestamptz) END END "
                                "WHERE ticket_id=:id"
                            ),
                            {
                                "id": ticket_id,
                                "created": created,
                                "received": created + timedelta(minutes=1 if showcase else 0),
                                "analyzed": created + timedelta(minutes=2 if showcase else 1),
                                "proposed": created + timedelta(minutes=3 if showcase else 1),
                                "followup_answered": created + timedelta(minutes=4 if showcase else 2),
                                "requested": requested,
                                "answered": answered,
                                "edited": created + timedelta(minutes=7 if showcase else 4, seconds=0 if showcase else 30),
                                "confirmed": created + timedelta(minutes=confirm_min),
                                "assigned": created + timedelta(minutes=assign_min),
                                "credential": created + timedelta(minutes=assign_min + 1),
                                "started": created + timedelta(minutes=assign_min + (5 if showcase else 10)),
                                "reported": reported,
                                "visit_ended": reported + timedelta(minutes=1 if showcase else 0),
                                "report_analysis": created + duration - timedelta(minutes=8),
                                "report_confirmed": created
                                + duration
                                - timedelta(minutes=5),
                                "finished": created + duration,
                            },
                        )
                        if showcase:
                            await session.execute(text(
                                "UPDATE information_requests SET created_at=:requested, "
                                "answered_at=CASE WHEN answered_at IS NULL THEN NULL ELSE CAST(:answered AS timestamptz) END "
                                "WHERE ticket_id=:id"
                            ), {"id": ticket_id, "requested": requested, "answered": answered})
                        if len(visits) == 2:
                            # Preserve the API-created visit and report identities; only their synthetic clocks move.
                            assignment_minutes = [assign_min, assign_min + 120]
                            start_minutes = [minute + 5 for minute in assignment_minutes]
                            report_minutes = [minute + 60 for minute in assignment_minutes]
                            review_minutes = [minute + 10 for minute in report_minutes]
                            for number, visit_id in enumerate(visits):
                                saved = created + timedelta(minutes=report_minutes[number])
                                await session.execute(text(
                                    "UPDATE assignments SET created_at=:assigned, started_at=:started, "
                                    "visit_ended_at=:ended WHERE id=:id AND ticket_id=:ticket"
                                ), {"id": visit_id, "ticket": ticket_id,
                                    "assigned": created + timedelta(minutes=assignment_minutes[number]),
                                    "started": created + timedelta(minutes=start_minutes[number]),
                                    "ended": saved + timedelta(minutes=1)})
                                await session.execute(text(
                                    "UPDATE completion_reports SET created_at=:reported, approved_at=:approved "
                                    "WHERE assignment_id=:id AND ticket_id=:ticket"
                                ), {"id": visit_id, "ticket": ticket_id,
                                    "reported": saved,
                                    "approved": created + timedelta(minutes=review_minutes[number])})
                            offsets = {
                                "assigned": assignment_minutes[:1],
                                "reassigned": assignment_minutes[1:],
                                "started": start_minutes,
                                "reported": report_minutes,
                                "visit_ended": [minute + 1 for minute in report_minutes],
                                "report_analysis": [minute + 2 for minute in report_minutes],
                                "report_confirmed": [minute + 5 for minute in report_minutes],
                                "report_reviewed": review_minutes,
                            }
                            indices = {}
                            for event_id, kind in events:
                                if kind.startswith("report_analysis_"):
                                    kind = "report_analysis"
                                n = indices.get(kind, 0)
                                if kind in offsets and n < len(offsets[kind]):
                                    await session.execute(text(
                                        "UPDATE ticket_events SET created_at=:at WHERE id=:id AND ticket_id=:ticket"
                                    ), {"id": event_id, "ticket": ticket_id,
                                        "at": created + timedelta(minutes=offsets[kind][n])})
                                indices[kind] = n + 1
                        if showcase:
                            # The seed records a human override after the case reaches its target state.
                            await session.execute(text(
                                "UPDATE ticket_events SET created_at=("
                                "SELECT max(created_at) + interval '1 minute' FROM ticket_events "
                                "WHERE ticket_id=:id AND event_type <> 'urgency_overridden'"
                                ") WHERE ticket_id=:id AND event_type='urgency_overridden'"
                            ), {"id": ticket_id})
                            await session.execute(text(
                                "UPDATE tickets SET updated_at=("
                                "SELECT max(created_at) FROM ticket_events WHERE ticket_id=:id"
                                ") WHERE id=:id"
                            ), {"id": ticket_id})
                            if showcase_reference is not None and (await session.execute(text(
                                "SELECT updated_at > :reference FROM tickets WHERE id=:id"
                            ), {"id": ticket_id, "reference": showcase_reference})).scalar_one():
                                raise Stop("future_showcase_event")
                        else:
                            await session.execute(
                                text("UPDATE tickets SET updated_at=:finished WHERE id=:id"),
                                {"id": ticket_id, "finished": created + duration},
                            )
            return {persona: str(user_id) for persona, user_id in personas.items()}
    finally:
        await db.close()


async def mark_synthetic_transfer(
    settings: Settings, generation: str, ticket_id: str
) -> None:
    """Record an internal referral only; no delivery event or outbox is created."""
    if (
        not settings.llm_fake
        or settings.deployment_mode != "demo"
        or os.environ.get("BARO_ISOLATED_SETTINGS") != "1"
        or os.environ.get("BARO_CAMPUS_OWNED_SUPERVISOR") != "1"
        or Path(os.environ.get("HOME", "")).name != generation
    ):
        raise Stop("owned_replay_required")
    db = Database(settings)
    try:
        async with db.transaction() as session:
            identity = (
                await session.execute(
                    text("SELECT current_database(), current_user, inet_server_port()")
                )
            ).one()
            url = make_url(settings.database_url.get_secret_value())
            if identity != ("booth_" + generation, "booth_" + generation, url.port):
                raise Stop("database_identity")
            deployment = (
                await session.execute(
                    text("SELECT mode, namespace FROM deployment_identity WHERE id=1")
                )
            ).one_or_none()
            if deployment != ("demo", "demo"):
                raise Stop("deployment_identity")
            tid = uuid.UUID(ticket_id)
            row = (
                await session.execute(
                    text(
                        "SELECT status, description FROM tickets WHERE id=:id FOR UPDATE"
                    ),
                    {"id": tid},
                )
            ).one_or_none()
            if (
                not row
                or row.description != "교내 다른 부서에 확인이 필요한 시설 문의입니다."
            ):
                raise Stop("transfer_identity")
            if row.status == "transferred":
                return
            if row.status != "received":
                raise Stop("transfer_state")
            actor = await session.scalar(
                text("SELECT id FROM users WHERE persona='dispatcher'")
            )
            if not actor:
                raise Stop("transfer_actor")
            await session.execute(
                text("UPDATE tickets SET status='transferred' WHERE id=:id"),
                {"id": tid},
            )
            await session.execute(
                text(
                    "INSERT INTO ticket_events (id,ticket_id,actor_id,event_type,reason,from_status,to_status) "
                    "VALUES (:event,:id,:actor,'synthetic_department_guidance',"
                    "'교내 다른 부서 확인 안내. 외부 전달 없음', 'received','transferred')"
                ),
                {
                    "event": uuid.uuid5(
                        uuid.NAMESPACE_URL, "baro-booth-transfer/" + generation
                    ),
                    "id": tid,
                    "actor": actor,
                },
            )
    finally:
        await db.close()


async def prepare_merge_locations(
    settings: Settings, generation: str, port: int, ids: list[str]
) -> None:
    """Install only the two explicitly named replay fixtures, never a real location signal."""
    validate(settings, generation, port, True)
    if os.environ.get("BARO_BOOTH_HISTORY_REPLAY") != "1" or len(set(ids)) != 2:
        raise Stop("owned_history_required")
    from app.services.location import registered

    selected = registered("DEMO-A:2:WR-F", "synthetic_fixture")
    selected["label"] = "시설관 2층 화장실"
    db = Database(settings)
    try:
        async with db.transaction() as session:
            url = make_url(settings.database_url.get_secret_value())
            identity = (
                await session.execute(
                    text("SELECT current_database(), current_user, inet_server_port()")
                )
            ).one()
            deployment = (
                await session.execute(
                    text("SELECT mode, namespace FROM deployment_identity WHERE id=1")
                )
            ).one_or_none()
            if identity != (
                "booth_" + generation,
                "booth_" + generation,
                url.port,
            ) or deployment != ("demo", "demo"):
                raise Stop("database_identity")
            for tid, (location, description) in zip(ids, HISTORY_MERGE, strict=True):
                ticket_id = uuid.UUID(tid)
                row = (
                    await session.execute(
                        text(
                            "SELECT t.status,t.description,t.location_text,t.category_code,t.location_data,t.location_version,t.created_at,u.persona,u.role "
                            "FROM tickets t JOIN users u ON u.id=t.reporter_id WHERE t.id=:id FOR UPDATE OF t"
                        ),
                        {"id": ticket_id},
                    )
                ).one_or_none()
                if (
                    not row
                    or row.description != description
                    or row.location_text != location
                    or row.persona != "reporter_a"
                    or row.role != "reporter"
                    or row.category_code != "MECH_WATER_LEAK"
                    or row.status != "received"
                    or row.created_at < datetime.now(UTC) - timedelta(hours=72)
                ):
                    raise Stop("merge_fixture_identity")
                relationships = await session.scalar(
                    text(
                        "SELECT (SELECT count(*) FROM assignments WHERE ticket_id=:id) + "
                        "(SELECT count(*) FROM completion_reports WHERE ticket_id=:id) + "
                        "(SELECT count(*) FROM ticket_merges WHERE source_id=:id OR target_id=:id)"
                    ),
                    {"id": ticket_id},
                )
                if relationships:
                    raise Stop("merge_fixture_relationship")
                existing = row.location_data or {}
                if (
                    existing.get("selected") == selected
                    and existing.get("state") == "confirmed"
                ):
                    continue
                if existing.get("state") == "confirmed":
                    raise Stop("merge_fixture_location_changed")
                location_data = dict(
                    existing,
                    state="confirmed",
                    selected=selected,
                    candidates=[selected],
                    version=row.location_version + 1,
                    source_type="synthetic",
                    dictionary_version="synthetic-locations-v1",
                )
                await session.execute(
                    text(
                        "UPDATE tickets SET location_data=CAST(:location AS jsonb), "
                        "location_version=location_version+1 WHERE id=:id"
                    ),
                    {
                        "location": json.dumps(location_data, ensure_ascii=False),
                        "id": ticket_id,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO ticket_events (id,ticket_id,actor_id,event_type,reason,from_status,to_status) "
                        "VALUES (:event,:id,NULL,'synthetic_location_fixture',"
                        "'화장실 위치를 확인했습니다','received','received')"
                    ),
                    {
                        "event": uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            "baro-booth-location/" + generation + "/" + tid,
                        ),
                        "id": ticket_id,
                    },
                )
    finally:
        await db.close()


async def verify_merge_fixture(
    settings: Settings, generation: str, port: int, ids: list[str]
) -> None:
    validate(settings, generation, port, True)
    if os.environ.get("BARO_BOOTH_HISTORY_REPLAY") != "1":
        raise Stop("owned_history_required")
    db = Database(settings)
    try:
        async with db.transaction() as session:
            url = make_url(settings.database_url.get_secret_value())
            identity = (
                await session.execute(
                    text("SELECT current_database(), current_user, inet_server_port()")
                )
            ).one()
            deployment = (
                await session.execute(
                    text("SELECT mode, namespace FROM deployment_identity WHERE id=1")
                )
            ).one_or_none()
            if identity != (
                "booth_" + generation,
                "booth_" + generation,
                url.port,
            ) or deployment != ("demo", "demo"):
                raise Stop("database_identity")
            parsed = [uuid.UUID(value) for value in ids]
            if len(set(parsed)) != 2:
                raise Stop("merge_ticket_identity")
            from app.services.location import registered

            selected = registered("DEMO-A:2:WR-F", "synthetic_fixture")
            selected["label"] = "시설관 2층 화장실"
            for index, (tid, (location, description)) in enumerate(
                zip(parsed, HISTORY_MERGE, strict=True)
            ):
                row = (
                    await session.execute(
                        text(
                            "SELECT t.status,t.description,t.location_text,t.category_code,t.location_data,t.created_at,u.persona,u.role "
                            "FROM tickets t JOIN users u ON u.id=t.reporter_id WHERE t.id=:id"
                        ),
                        {"id": tid},
                    )
                ).one_or_none()
                if (
                    not row
                    or row.status != ("merged" if index == 0 else "received")
                    or row.description != description
                    or row.location_text != location
                    or row.category_code != "MECH_WATER_LEAK"
                    or row.persona != "reporter_a"
                    or row.role != "reporter"
                    or row.location_data.get("source_type") != "synthetic"
                    or row.location_data.get("state") != "confirmed"
                    or row.location_data.get("selected") != selected
                ):
                    raise Stop("merge_fixture_identity")
                count = await session.scalar(
                    text(
                        "SELECT (SELECT count(*) FROM assignments WHERE ticket_id=:id) + "
                        "(SELECT count(*) FROM completion_reports WHERE ticket_id=:id)"
                    ),
                    {"id": tid},
                )
                events = await session.scalar(
                    text(
                        "SELECT count(*) FROM ticket_events WHERE ticket_id=:id AND actor_id IS NULL "
                        "AND event_type='synthetic_location_fixture' AND created_at>=:created"
                    ),
                    {"id": tid, "created": row.created_at},
                )
                if count or events != 1:
                    raise Stop("merge_fixture_relationship")
            links = (
                await session.execute(
                    text(
                        "SELECT source_id,target_id,active,previous_status,created_at,safety_snapshot "
                        "FROM ticket_merges WHERE source_id = ANY(:ids) OR target_id = ANY(:ids)"
                    ),
                    {"ids": parsed},
                )
            ).all()
            if len(links) != 1 or (
                links[0].source_id,
                links[0].target_id,
                links[0].active,
                links[0].previous_status,
            ) != (parsed[0], parsed[1], True, "received"):
                raise Stop("merge_result_invalid")
            safety = (
                await session.execute(
                    text(
                        "SELECT s.created_at,t.created_at,s.safety_data,s.effective_urgency,"
                        "t.linked_safety_floor FROM tickets s JOIN tickets t ON t.id=:target WHERE s.id=:source"
                    ),
                    {"source": parsed[0], "target": parsed[1]},
                )
            ).one()
            floor = min(
                safety.safety_data.get("automatic_urgency", 3), safety.effective_urgency
            )
            if (
                links[0].created_at < max(safety[0], safety[1])
                or links[0].created_at - min(safety[0], safety[1]) > timedelta(hours=72)
                or links[0].safety_snapshot.get("floor") != floor
                or safety.linked_safety_floor > floor
            ):
                raise Stop("merge_safety_or_dates_invalid")
            valid = await session.scalar(
                text(
                    "SELECT count(*) FROM ticket_events e JOIN users u ON u.id=e.actor_id "
                    "JOIN tickets t ON t.id=e.ticket_id WHERE e.ticket_id=:id AND e.event_type='merged' "
                    "AND u.persona='dispatcher' AND u.role='dispatcher' AND e.created_at>=t.created_at"
                ),
                {"id": parsed[0]},
            )
            if valid != 1:
                raise Stop("merge_event_invalid")
            received = await session.scalar(
                text(
                    "SELECT count(*) FROM ticket_events e JOIN users u ON u.id=e.actor_id "
                    "JOIN tickets t ON t.id=e.ticket_id WHERE e.ticket_id=:id AND e.event_type='merge_received' "
                    "AND u.persona='dispatcher' AND u.role='dispatcher' AND e.created_at>=t.created_at"
                ),
                {"id": parsed[1]},
            )
            if received != 1:
                raise Stop("merge_event_invalid")
    finally:
        await db.close()


def live_history_http_failure(method: str, path: str, status: int) -> str:
    """Classify the seed stage without exposing IDs or request contents."""
    if method == "POST" and path.endswith("/confirm-assign"):
        stage = "assign"
    elif method == "POST" and path.endswith("/start"):
        stage = "start"
    elif method == "POST" and path.endswith("/report"):
        stage = "report"
    elif method == "POST" and path.endswith("/confirm"):
        stage = "confirm"
    elif method == "POST" and path.endswith("/review-report"):
        stage = "review"
    elif method == "POST" and path == "/tickets":
        stage = "ticket"
    else:
        return "http_" + str(status)
    return "http_" + stage + "_" + str(status)


def showcase_report_structure(report: ShowcaseReport) -> dict:
    return {
        "actions": list(report.actions),
        "materials_used": list(report.materials),
        "residual_issue": report.residual_issue,
        "revisit_needed": report.result in {"temporary", "revisit"},
        "source": "manual",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--allow-replay", action="store_true")
    parser.add_argument("--history-only", action="store_true")
    parser.add_argument("--live-history-only", action="store_true")
    parser.add_argument("--showcase-only", action="store_true")
    showcase_actions = parser.add_mutually_exclusive_group()
    showcase_actions.add_argument("--rebase-showcase", action="store_true", help="Move the saved showcase dates to today in Seoul")
    showcase_actions.add_argument("--upgrade-showcase-copy", action="store_true", help="Refresh a restored copy of the previous live showcase in replay mode")
    if "--live-history-only" in sys.argv and (
        "--allow-replay" in sys.argv or "--history-only" in sys.argv
    ):
        raise Stop("live_history_requires_live")
    args = parser.parse_args()
    if args.rebase_showcase or args.upgrade_showcase_copy:
        if not args.allow_replay or args.showcase_only:
            raise Stop("showcase_requires_isolated_demo")
        if args.upgrade_showcase_copy and args.api_port == 8011:
            raise Stop("showcase_restore_required")
        args.showcase_only = True
    if args.showcase_only and (args.history_only or args.live_history_only):
        raise Stop("showcase_requires_isolated_demo")
    if args.history_only and not args.allow_replay:
        raise Stop("history_requires_replay")
    settings = Settings(_env_file=None)
    validate(settings, args.generation, args.api_port, args.allow_replay)
    home = Path(os.environ["HOME"])
    state_path = home / (
        "showcase-seed-v1.json"
        if args.showcase_only
        else "live-history-seed-v1.json"
        if args.live_history_only
        else "history-seed-v1.json"
        if args.history_only
        else "scenario-seed-v1.json"
    )
    if state_path.exists():
        if state_path.is_symlink():
            raise Stop("state_identity")
        state = json.loads(state_path.read_text())
        if state.get("generation") != args.generation or (
            args.showcase_only and state.get("showcase_revision") != (
                LEGACY_SHOWCASE_REVISION if args.upgrade_showcase_copy else SHOWCASE_REVISION
            )
        ):
            raise Stop("state_identity")
    else:
        if args.rebase_showcase or args.upgrade_showcase_copy:
            raise Stop("state_identity")
        state = {"generation": args.generation, "items": {}}
        if args.showcase_only:
            # Changed report payloads and case targets must use a fresh generation.
            state["showcase_revision"] = SHOWCASE_REVISION
        save(state_path, state)
    existing_ids = showcase_ticket_ids(state) if args.rebase_showcase or args.upgrade_showcase_copy else None
    if args.rebase_showcase:
        reference = datetime.now(UTC)
        dates = {str(existing_ids[row[0]]): showcase_date(reference, index) for index, row in enumerate(SHOWCASE)}
        asyncio.run(identity_and_dates(settings, args.generation, dates, showcase=True,
                                       showcase_ids=existing_ids, showcase_reference=reference,
                                       rebase_showcase=True))
        state["reference"] = reference.isoformat()
        save(state_path, state)
        print("SHOWCASE_REBASED", len(dates), flush=True)
        return
    workers = asyncio.run(identity_and_dates(settings, args.generation))
    base = f"http://127.0.0.1:{args.api_port}"
    clients = {
        name: httpx.Client(
            base_url=base,
            headers={"Origin": settings.app_base_url},
            trust_env=False,
            timeout=20,
        )
        for name in (
            "reporter_a",
            "dispatcher",
            "worker_a",
            "worker_b",
            "worker_c",
            "worker_d",
            *(("worker_f",) if args.showcase_only else ()),
        )
    }

    def request(client, method, path, body=None, *, idem=None, files=None):
        headers = {"Idempotency-Key": idem} if idem else {}
        response = client.request(
            method, "/v1" + path, json=body, files=files, headers=headers
        )
        if response.status_code not in (200, 201):
            if args.showcase_only and response.status_code == 409:
                detail = response.json().get("error", {}).get("code")
                allowed = {"atomic_location_category_unconfirmed", "atomic_worker_field_unavailable", "atomic_worker_schedule_stale",
                           "atomic_worker_day_off", "ticket_revision_conflict", "catalog_revision_conflict",
                           "assignment_snapshot_conflict", "location_relationship_conflict"}
                if detail in allowed:
                    raise Stop(detail)
            reason = (live_history_http_failure(method, path, response.status_code)
                      if args.live_history_only or args.showcase_only else "http_" + str(response.status_code))
            raise Stop(reason)
        return response.json()

    try:
        runtime = request(clients["dispatcher"], "GET", "/runtime")
        if (
            runtime.get("mode"),
            runtime.get("llm_mode"),
            runtime.get("mail_delivery"),
            runtime.get("auth_method"),
        ) != ("demo", "replay" if args.allow_replay else "live", "disabled", "demo"):
            raise Stop("runtime_mismatch")
        for persona, client in clients.items():
            session = request(client, "POST", "/auth/demo", {"persona": persona})
            client.headers["X-CSRF-Token"] = session["csrf_token"]
        reporter, dispatcher = clients["reporter_a"], clients["dispatcher"]
        catalog = request(dispatcher, "GET", "/admin/catalog")
        dataset = catalog["dataset"]
        expected_versions = ("2026-11-17-r10", "2026-11-17-r11") if args.showcase_only else ("2026-11-14-r08",)
        if dataset["version"] not in expected_versions or len(catalog["locations"]) != 251:
            raise Stop("catalog_mismatch")
        profiles = request(dispatcher, "GET", "/admin/workers")
        if not set(workers.values()).issubset({x["worker_id"] for x in profiles}):
            raise Stop("worker_profiles_missing")
        if args.upgrade_showcase_copy:
            for ticket_id in existing_ids.values():
                draft = request(dispatcher, "GET", f"/tickets/{ticket_id}").get("ai_draft")
                if not draft or draft.get("stale") is not False or draft.get("mode") != "live":
                    raise Stop("live_ledger_invalid")
            asyncio.run(identity_and_dates(settings, args.generation, showcase_ids=existing_ids,
                                           upgrade_showcase=True))
        dates = {}
        if (args.history_only or args.showcase_only) and "reference" not in state:
            state["reference"] = datetime.now(UTC).isoformat()
            save(state_path, state)
        reference = datetime.now(UTC) if args.upgrade_showcase_copy else (
            datetime.fromisoformat(state["reference"])
            if args.history_only or args.showcase_only
            else datetime.now(UTC)
        )
        cases = (
            tuple(row[:7] for row in SHOWCASE)
            if args.showcase_only
            else live_history_scenarios()
            if args.live_history_only
            else history_scenarios()
            if args.history_only
            else SCENARIOS
        )
        for index, (
            name,
            location,
            code,
            description,
            category,
            target,
            persona,
        ) in enumerate(cases):
            item = state["items"].setdefault(name, {})
            report_fixture = SHOWCASE_REPORTS.get(name) if args.showcase_only else None
            has_photo = name == "photo" or (args.showcase_only and index < 3)
            if has_photo and not item.get("upload"):
                if item.get("upload_pending"):
                    raise Stop("upload_outcome_unknown")
                item["upload_pending"] = True
                save(state_path, state)
                result = request(
                    reporter,
                    "POST",
                    "/uploads",
                    files={"file": ("fixture.jpg", photo_bytes(), "image/jpeg")},
                )
                item.update(upload=result["id"], upload_pending=False)
                save(state_path, state)
            payload = {
                "text": description,
                "location_text": location,
                "upload_ids": [item["upload"]] if has_photo else [],
            }
            ticket = (
                request(dispatcher, "GET", f"/tickets/{existing_ids[name]}")
                if args.upgrade_showcase_copy else
                request(reporter, "POST", "/tickets", payload, idem=key(name, "ticket"))
            )
            tid = ticket["id"]
            if item.get("ticket") not in (None, tid):
                raise Stop("ticket_identity")
            item["ticket"] = tid
            save(state_path, state)
            deadline = time.monotonic() + 180
            while True:
                ticket = request(dispatcher, "GET", "/tickets/" + tid)
                if ticket["ai_status"] in (
                    ("drafted",) if args.upgrade_showcase_copy or not args.allow_replay else ("replayed", "unknown")
                ):
                    break
                if (
                    ticket["ai_status"] in ("failed", "error", "unknown")
                    or time.monotonic() > deadline
                ):
                    raise Stop("ticket_analysis_incomplete")
                time.sleep(0.5)
            if not args.allow_replay and not ticket.get("ai_draft"):
                raise Stop("ticket_draft_missing")
            if args.history_only and ticket["ai_status"] != "replayed":
                raise Stop("history_fixture_not_matched")
            if name == "photo" and not args.allow_replay:
                asyncio.run(
                    identity_and_dates(settings, args.generation, photo_ticket=tid)
                )
            if args.showcase_only:
                if name in ("showcase-progress-a", "showcase-confirm-a"):
                    own = request(reporter, "GET", "/tickets/" + tid)
                    followup = own.get("ai_followup")
                    answers = [
                        {"question_id": question["id"], "answer": answer}
                        for question in (followup or {}).get("questions", [])
                        if (answer := showcase_followup_answer(name, question)) is not None
                    ]
                    if followup and followup["status"] == "pending" and answers:
                        request(reporter, "POST", f"/tickets/{tid}/ai-followup", {
                            "action": "answer", "analyze_with_ai": False,
                            "answers": answers,
                        }, idem=key(name, "ai-followup"))
                answered_name, question_text, answer_text = SHOWCASE_ANSWERED_REQUEST
                if name == answered_name:
                    path = f"/tickets/{tid}/information-requests"
                    questions = request(dispatcher, "GET", path)
                    question = next((item for item in questions if item["question"] == question_text), None)
                    if question is None:
                        question = request(
                            dispatcher, "POST", path, {"question": question_text},
                            idem=key(name, "information"),
                        )
                    if question["status"] == "open":
                        question = request(
                            reporter, "POST", f"{path}/{question['id']}/answer",
                            {"answer": answer_text, "analyze_with_ai": False}, idem=key(name, "answer"),
                        )
                    if question["status"] != "answered" or question["answer"] != answer_text:
                        raise Stop("showcase_saved_answer_changed")
                if target == "information":
                    questions = request(dispatcher, "GET", f"/tickets/{tid}/information-requests")
                    if not questions:
                        request(dispatcher, "POST", f"/tickets/{tid}/information-requests",
                                {"question": "얼룩이 보이는 위치를 조금 더 알려 주시겠어요?"},
                                idem=key(name, "information"))
            if args.showcase_only:
                # Answers are ticket mutations; always use the current assignment revision.
                ticket = request(dispatcher, "GET", "/tickets/" + tid)
                if not persona and ticket["category_code"] != category:
                    ticket = request(dispatcher, "PATCH", "/tickets/" + tid, {
                        "expected_location_version": ticket["location"]["version"],
                        "expected_category_code": ticket["category_code"],
                        "expected_catalog_revision": catalog["revision"],
                        "category_code": category,
                        "reason": "담당자가 신고 분야를 확인했습니다",
                    }, idem=key(name, "category"))
            if target == "confirmed" and ticket["status"] == "received":
                ticket = request(
                    dispatcher,
                    "POST",
                    f"/tickets/{tid}/confirm",
                    {"reason": "신고 내용을 확인했습니다"},
                )
            if target == "transfer":
                asyncio.run(mark_synthetic_transfer(settings, args.generation, tid))
                ticket = request(dispatcher, "GET", "/tickets/" + tid)
            if persona and (ticket["status"] == "received" or (
                args.upgrade_showcase_copy and ticket["status"] == "confirmed" and name == "showcase-confirm-b"
            )):
                assignment = request(
                    dispatcher,
                    "POST",
                    f"/tickets/{tid}/confirm-assign",
                    {
                        "worker_id": workers[persona],
                        "reason": "신고 내용을 확인하고 배정합니다",
                        "expected_ticket_version": ticket["merge_version"],
                        "expected_catalog_revision": catalog["revision"],
                        "category_code": category,
                        "location_choice": {"kind": "candidate", "code": code},
                        "dataset_location": {
                            "code": code,
                            "dataset_revision": dataset["revision"],
                            "sha256": dataset["hash"],
                        },
                    },
                    idem=key(name, "assign"),
                )
                ticket = request(dispatcher, "GET", "/tickets/" + tid)
                if ticket["assignment_id"] != assignment["assignment_id"]:
                    raise Stop("assignment_identity")
            if persona:
                aid = ticket["assignment_id"]
                if not aid:
                    raise Stop("assignment_missing")
                worker = clients.get(persona)
                path = "/worker/assignments/" + aid
                if worker is None and ticket["status"] == "assigned":
                    credential = request(
                        dispatcher,
                        "POST",
                        "/assignments/" + aid + "/credential",
                        {
                            "request_id": str(
                                uuid.uuid5(uuid.NAMESPACE_URL, key(name, "credential"))
                            ),
                            "expected_credential_revision": 0,
                        },
                    )
                    if not credential.get("worker_token"):
                        raise Stop("credential_outcome_unknown")
                    worker = httpx.Client(
                        base_url=base,
                        headers={
                            "Origin": settings.app_base_url,
                            "Authorization": "Bearer " + credential["worker_token"],
                        },
                        trust_env=False,
                        timeout=20,
                    )
                    clients[name] = worker
                if ticket["status"] == "assigned" and target != "queued":
                    request(worker, "POST", path + "/start", {})
                    ticket = request(dispatcher, "GET", "/tickets/" + tid)
                if target in ("report", "completed", "temporary", "revisit"):
                    if report_fixture is not None:
                        result_kind, report_text = report_fixture.result, report_fixture.text
                    else:
                        result_kind = target if target in ("temporary", "revisit") else "completed"
                        report_text = (
                            next(row[7] for row in LIVE_HISTORY if row[0] == name)
                            if args.live_history_only else REPORT_TEXT
                        )
                    report = ticket.get("latest_report")
                    if not report:
                        if args.showcase_only and index < 3 and not item.get("after_upload"):
                            if item.get("after_upload_pending"):
                                raise Stop("upload_outcome_unknown")
                            item["after_upload_pending"] = True
                            save(state_path, state)
                            uploaded = request(worker, "POST", path + "/uploads",
                                               files={"file": ("after.jpg", photo_bytes(after=True), "image/jpeg")})
                            item.update(after_upload=uploaded["id"], after_upload_pending=False)
                            save(state_path, state)
                        sent = request(
                            worker,
                            "POST",
                            path + "/report",
                            {
                                "result": result_kind,
                                "text": report_text,
                                "upload_ids": [item["after_upload"]] if args.showcase_only and index < 3 else [],
                            },
                            idem=key(name, "report"),
                        )
                        report = sent["latest_report"]
                    deadline = time.monotonic() + 180
                    while report["analysis_status"] not in (
                        "replayed",
                        "drafted",
                        "worker_confirmed",
                    ):
                        if (
                            report["analysis_status"] in ("failed", "error", "unknown")
                            or time.monotonic() > deadline
                        ):
                            raise Stop("report_analysis_incomplete")
                        report = request(worker, "GET", path)["ticket"]["latest_report"]
                        time.sleep(0.5)
                    if args.live_history_only and "report_id" not in item:
                        if report["analysis_status"] != "drafted" or report["revision"] < 2:
                            raise Stop("live_report_revision_unknown")
                        item["report_id"] = report["id"]
                        item["report_input_revision"] = report["revision"] - 1
                        save(state_path, state)
                    if args.live_history_only and item.get("report_id") != report["id"]:
                        raise Stop("live_report_identity")
                    if report["analysis_status"] != "worker_confirmed":
                        if not report.get("draft"):
                            raise Stop("report_draft_missing")
                        report = request(
                            worker,
                            "POST",
                            path + "/reports/" + report["id"] + "/confirm",
                            {
                                "expected_revision": report["revision"],
                                "confirmed_result": result_kind,
                                "structure": (
                                    showcase_report_structure(report_fixture)
                                    if report_fixture is not None else report["draft"]
                                ),
                                "reason": "작업 내용을 확인했습니다",
                            },
                            idem=key(name, "report-confirm"),
                        )["latest_report"]
                    if (
                        target in ("completed", "temporary", "revisit")
                        and not report["reviewed"]
                    ):
                        request(
                            dispatcher,
                            "POST",
                            f"/tickets/{tid}/review-report",
                            {
                                "report_id": report["id"],
                                "expected_report_revision": report["revision"],
                                "reason": "작업 보고를 확인했습니다",
                            },
                            idem=key(name, "report-review"),
                        )
                    if target in ("temporary", "revisit"):
                        execution = request(
                            dispatcher, "GET", "/assignments/" + aid + "/execution"
                        )
                        if execution["phase"] != "visit_ended":
                            report = request(dispatcher, "GET", "/tickets/" + tid)[
                                "latest_report"
                            ]
                            request(
                                dispatcher,
                                "POST",
                                "/assignments/" + aid + "/end-visit",
                                {
                                    "expected_execution_revision": execution[
                                        "revision"
                                    ],
                                    "report_id": report["id"],
                                    "expected_report_revision": report["revision"],
                                    "reason": "이번 방문을 종료합니다",
                                },
                                idem=key(name, "end-visit"),
                            )
            ticket = request(dispatcher, "GET", "/tickets/" + tid)
            if args.showcase_only and target == "temporary" and ticket["status"] != "completed":
                execution = request(dispatcher, "GET", "/assignments/" + aid + "/execution")
                if execution["phase"] != "visit_ended":
                    raise Stop("showcase_revisit_phase_changed")
                new_assignment = request(dispatcher, "POST", f"/tickets/{tid}/assign", {
                    "worker_id": workers[persona], "reason": "추가 작업을 위해 재방문합니다",
                    "expected_assignment_id": aid,
                    "expected_execution_revision": execution["revision"],
                })
                new_aid = new_assignment["assignment_id"]
                new_path = "/worker/assignments/" + new_aid
                request(worker, "POST", new_path + "/start", {})
                if not item.get("final_upload"):
                    if item.get("final_upload_pending"):
                        raise Stop("upload_outcome_unknown")
                    item["final_upload_pending"] = True
                    save(state_path, state)
                    uploaded = request(worker, "POST", new_path + "/uploads",
                                       files={"file": ("final.jpg", photo_bytes(after=True), "image/jpeg")})
                    item.update(final_upload=uploaded["id"], final_upload_pending=False)
                    save(state_path, state)
                report = request(worker, "POST", new_path + "/report", {
                    "result": SHOWCASE_FINAL_REPORT.result, "text": SHOWCASE_FINAL_REPORT.text,
                    "upload_ids": [item["final_upload"]],
                }, idem=key(name, "final-report"))["latest_report"]
                deadline = time.monotonic() + 180
                while report["analysis_status"] not in ("replayed", "drafted", "worker_confirmed"):
                    if report["analysis_status"] in ("failed", "error", "unknown") or time.monotonic() > deadline:
                        raise Stop("report_analysis_incomplete")
                    time.sleep(0.5)
                    report = request(worker, "GET", new_path)["ticket"]["latest_report"]
                if report["analysis_status"] != "worker_confirmed":
                    report = request(worker, "POST", new_path + "/reports/" + report["id"] + "/confirm", {
                        "expected_revision": report["revision"],
                        "confirmed_result": SHOWCASE_FINAL_REPORT.result,
                        "structure": showcase_report_structure(SHOWCASE_FINAL_REPORT),
                        "reason": "작업 내용을 확인했습니다",
                    }, idem=key(name, "final-confirm"))["latest_report"]
                request(dispatcher, "POST", f"/tickets/{tid}/review-report", {
                    "report_id": report["id"], "expected_report_revision": report["revision"],
                    "reason": "재방문 작업을 확인했습니다",
                }, idem=key(name, "final-review"))
                ticket = request(dispatcher, "GET", "/tickets/" + tid)
            if args.showcase_only and name in SHOWCASE_URGENCY_OVERRIDES:
                urgency, reason = SHOWCASE_URGENCY_OVERRIDES[name]
                if ticket["safety"]["effective_urgency"] != urgency and (
                    urgency < ticket["safety"]["effective_urgency"] or not ticket["safety"]["emergency_banner"]
                ):
                    ticket = request(dispatcher, "POST", f"/tickets/{tid}/urgency", {
                        "reason": reason,
                        "expected_assessment_version": ticket["safety"]["assessment_version"],
                        "effective_urgency": urgency,
                    })
            if args.upgrade_showcase_copy:
                draft = ticket.get("ai_draft")
                if not draft or draft.get("stale") is not False or draft.get("mode") != "live":
                    raise Stop("live_ledger_invalid")
            expected = {
                "information": "received",
                "review": "received",
                "confirmed": "confirmed",
                "progress": "in_progress",
                "queued": "assigned",
                "report": "in_progress",
                "temporary": "in_progress",
                "revisit": "in_progress",
                "transfer": "transferred",
                "completed": "completed",
            }[target]
            if args.showcase_only and target == "temporary":
                expected = "completed"
            if ticket["status"] != expected or (
                target == "report" and not ticket["latest_report"]
            ):
                raise Stop(f"showcase_case_status:{name}:{ticket['status']}:{expected}")
            if report_fixture is not None:
                expected_report = SHOWCASE_FINAL_REPORT if target == "temporary" else report_fixture
                latest = ticket["latest_report"]
                historical = bool(args.upgrade_showcase_copy and latest
                                  and latest["text"] == REPORT_TEXT)
                verified_structure = historical and all(
                    latest.get("confirmed", {}).get(field) == value
                    for field, value in showcase_report_structure(expected_report).items()
                )
                expected_reviewed = target != "report" or (historical and name == "showcase-report-a")
                if (
                    not latest
                    or (latest["text"] != expected_report.text and not verified_structure)
                    or latest["confirmed_result"] != expected_report.result
                    or latest["analysis_status"] != "worker_confirmed"
                    or latest["reviewed"] != expected_reviewed
                ):
                    raise Stop(f"showcase_report_state:{name}")
            if args.upgrade_showcase_copy or not item.get("dated"):
                dates[tid] = (
                    showcase_date(reference, index)
                    if args.showcase_only
                    else reference - timedelta(days=index % 10, hours=5 + index % 5)
                    if args.history_only
                    else scenario_date(reference, index)
                )
            print("READY", name, target, flush=True)
        if args.upgrade_showcase_copy or not args.allow_replay:
            asyncio.run(
                identity_and_dates(
                    settings,
                    args.generation,
                    live_tickets=[state["items"][name]["ticket"] for name, *_ in cases],
                    live_reports=(
                        [
                            (state["items"][row[0]]["ticket"],
                             state["items"][row[0]]["report_id"],
                             state["items"][row[0]]["report_input_revision"])
                            for row in LIVE_HISTORY
                            if row[7]
                        ]
                        if args.live_history_only
                        else None
                    ),
                )
            )
        if dates:
            asyncio.run(
                identity_and_dates(
                    settings, args.generation, dates, history=args.history_only, showcase=args.showcase_only,
                    showcase_ids=existing_ids, showcase_reference=reference if args.showcase_only else None,
                )
            )
            for item in state["items"].values():
                item["dated"] = True
            if args.upgrade_showcase_copy:
                state.update(showcase_revision=SHOWCASE_REVISION, reference=reference.isoformat())
            save(state_path, state)
        if args.history_only:
            ids = []
            for index, (location, description) in enumerate(HISTORY_MERGE):
                name = f"history-merge-{index}"
                item = state["items"].setdefault(name, {})
                created = request(
                    reporter,
                    "POST",
                    "/tickets",
                    {"text": description, "location_text": location, "upload_ids": []},
                    idem=key(name, "ticket"),
                )
                tid = created["id"]
                if item.get("ticket") not in (None, tid):
                    raise Stop("merge_ticket_identity")
                item["ticket"] = tid
                save(state_path, state)
                deadline = time.monotonic() + 180
                while True:
                    ticket = request(dispatcher, "GET", "/tickets/" + tid)
                    if ticket["ai_status"] == "replayed":
                        break
                    if (
                        ticket["ai_status"] in ("failed", "error", "unknown")
                        or time.monotonic() > deadline
                    ):
                        raise Stop("merge_analysis_incomplete")
                    time.sleep(0.5)
                if (
                    ticket["status"] == "received"
                    and ticket["category_code"] == "UNKNOWN"
                ):
                    ticket = request(
                        dispatcher,
                        "PATCH",
                        "/tickets/" + tid,
                        {
                            "expected_location_version": ticket["location"]["version"],
                            "expected_category_code": "UNKNOWN",
                            "expected_catalog_revision": catalog["revision"],
                            "category_code": "MECH_WATER_LEAK",
                            "reason": "누수 신고로 분류했습니다",
                        },
                        idem=key(name, "category"),
                    )
                ids.append(tid)
            source = request(dispatcher, "GET", "/tickets/" + ids[0])
            target = request(dispatcher, "GET", "/tickets/" + ids[1])
            if source["status"] == "received" and target["status"] == "received":
                asyncio.run(
                    prepare_merge_locations(
                        settings, args.generation, args.api_port, ids
                    )
                )
                candidates = request(
                    dispatcher, "GET", "/tickets/" + ids[0] + "/duplicates"
                )
                matches = [
                    candidate
                    for candidate in candidates["candidates"]
                    if candidate["target_id"] == ids[1]
                ]
                if len(matches) != 1 or matches[0]["evidence"] != "same_space_24h":
                    raise Stop("merge_candidate_missing")
                source = request(
                    dispatcher,
                    "POST",
                    "/tickets/" + ids[0] + "/merge",
                    {
                        "target_id": ids[1],
                        "expected_source_version": candidates["source_version"],
                        "expected_target_version": matches[0]["merge_version"],
                        "reason": "같은 시설의 중복 신고를 연결합니다",
                    },
                    idem=key("history-merge", "merge"),
                )
            links = [link for link in source["merge_history"] if link["active"]]
            if (
                source["status"] != "merged"
                or target["status"] != "received"
                or len(links) != 1
                or links[0]["source_id"] != ids[0]
                or links[0]["target_id"] != ids[1]
                or links[0]["source_previous_status"] != "received"
            ):
                raise Stop("merge_result_invalid")
            asyncio.run(
                verify_merge_fixture(settings, args.generation, args.api_port, ids)
            )
            print("HISTORY_MERGE_READY", flush=True)
        print(
            "SCENARIOS_READY",
            len(cases) + (len(HISTORY_MERGE) if args.history_only else 0),
            flush=True,
        )
    finally:
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    try:
        main()
    except (Stop, httpx.HTTPError, OSError, ValueError, KeyError, StopIteration) as exc:
        print(
            "STOP",
            str(exc) if isinstance(exc, Stop) else type(exc).__name__,
            file=sys.stderr,
        )
        sys.exit(1)
