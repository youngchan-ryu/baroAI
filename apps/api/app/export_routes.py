"""Authenticated, audited CSV preparation. Business tickets remain unchanged."""

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, time, timedelta

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from starlette.responses import Response

from app.catalog import CATEGORIES, CATEGORY_MAP
from app.duplicates import aggregate_safety, merge_version
from app.export_schemas import (
    ExportCreate,
    ExportPreview,
    ExportPreviewInput,
    ExportSelection,
    ExportView,
)
from app.models import ExportDownload, ExportRecord, Ticket
from app.security import authenticate, role
from app.services.routing import LABELS
from app.services.sfims_adapter import COLUMNS, SCHEMA_VERSION, CsvExport, safe_cell


def export_enabled(request: Request):
    if request.app.state.settings.deployment_mode in {"prototype", "service"}:
        raise HTTPException(404, "export_unavailable")


router = APIRouter(
    prefix="/v1/admin", tags=["csv-preparation"], dependencies=[Depends(export_enabled)]
)
CANONICAL = {
    c["code"] for c in CATEGORIES if c["code"] not in {"PLUMBING", "ELECTRICAL", "HVAC", "BUILDING"}
}


def hash_value(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def namespace(settings):
    return f"{settings.deployment_mode}:{settings.app_env}"


def cipher(settings):
    if not settings.export_encryption_key.get_secret_value():
        raise HTTPException(503, "export_key_unconfigured")
    return Fernet(settings.export_encryption_key.get_secret_value().encode())


def decode_snapshot(settings, record):
    return json.loads(cipher(settings).decrypt(record.encrypted_snapshot.encode()))


def decode_audit(settings, record):
    return json.loads(cipher(settings).decrypt(record.encrypted_audit.encode()))


async def dispatcher(request, db):
    principal = await authenticate(request, db)
    role(principal, "dispatcher")
    return principal


async def selected(db, selection):
    query = select(Ticket)
    if selection.ticket_ids:
        query = query.where(Ticket.id.in_(selection.ticket_ids))
    else:
        filters = selection.filters
        start = datetime.combine(filters.from_date, time.min, tzinfo=UTC)
        end = datetime.combine(filters.through_date + timedelta(days=1), time.min, tzinfo=UTC)
        query = query.where(Ticket.created_at >= start, Ticket.created_at < end)
        if filters.statuses:
            query = query.where(Ticket.status.in_(filters.statuses))
    tickets = list(await db.scalars(query.order_by(Ticket.id).limit(51)))
    if len(tickets) > 50:
        raise HTTPException(422, "export_limit_exceeded")
    if not tickets:
        raise HTTPException(422, "export_selection_empty")
    if selection.ticket_ids and len(tickets) != len(selection.ticket_ids):
        raise HTTPException(404, "export_ticket_not_found")
    return tickets


@router.post("/exports/select")
async def select_export(payload: ExportSelection, request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db)
        tickets = await selected(db, payload)
        return {
            "items": [
                {
                    "ticket_id": str(t.id),
                    "public_no": t.public_no or f"B-{str(t.id)[:8]}",
                    "status": t.status,
                    "category_code": t.category_code,
                }
                for t in tickets
            ]
        }


async def capture(db, settings, payload):
    tickets = await selected(db, payload.selection)
    supplied = {row.ticket_id: row for row in payload.rows}
    if len(supplied) != len(payload.rows) or set(supplied) != {t.id for t in tickets}:
        raise HTTPException(409, "export_selection_changed")
    stamp = getattr(payload, "expected_snapshot_at", None) or await db.scalar(
        select(func.clock_timestamp())
    )
    rows, basis = [], []
    for ticket in tickets:
        draft = supplied[ticket.id]
        # Legacy coarse categories are explicitly unknown in this neutral schema;
        # never invent a detailed SFIMS mapping from them.
        code = ticket.category_code if ticket.category_code in CANONICAL else "UNKNOWN"
        category = CATEGORY_MAP[code]
        for value in [
            ticket.effective_urgency,
            ticket.linked_safety_floor,
            *[ticket.safety_data.get(k) for k in ("ai_urgency", "rule_floor", "automatic_urgency")],
        ]:
            if value is not None and (type(value) is not int or value not in range(4)):
                raise HTTPException(422, "export_invalid_safety_data")
        risk = aggregate_safety(ticket)
        department = ticket.routing_data.get("department_code", "REVIEW")
        if department not in LABELS:
            department = "REVIEW"
        row = [
            SCHEMA_VERSION,
            settings.deployment_mode,
            stamp.isoformat(),
            "",
            ticket.created_at.isoformat(),
            ticket.updated_at.isoformat(),
            "unverified",
            "not_sent",
            ticket.public_no or f"B-{str(ticket.id)[:8]}",
            ticket.status,
            code,
            category["domain"],
            department,
            "suggested",
            ticket.location_data.get("state", "pending"),
            ticket.location_data.get("source_type", "unregistered"),
            draft.export_description,
            draft.export_location,
            "dispatcher_authored",
            ticket.safety_data.get("ai_urgency"),
            ticket.safety_data.get("rule_floor"),
            ticket.safety_data.get("automatic_urgency"),
            risk["linked_rule_floor"],
            ticket.effective_urgency,
            bool(ticket.override_reason),
            risk["effective_urgency"],
            risk["linked_requires_review"] or ticket.safety_data.get("requires_review", False),
            risk["emergency_banner"],
        ]
        rows.append([safe_cell(value) for value in row])
        basis.append(
            {
                "ticket": {c.name: getattr(ticket, c.name) for c in Ticket.__table__.columns},
                "related_version": await merge_version(db, ticket),
            }
        )
    snapshot_hash = hash_value(
        {
            "schema": SCHEMA_VERSION,
            "namespace": namespace(settings),
            "basis": basis,
            "rows": rows,
            "selection": payload.selection.model_dump(mode="json"),
        }
    )
    for row in rows:
        row[COLUMNS.index("snapshot_hash")] = snapshot_hash
    return ExportPreview(
        snapshot_at=stamp,
        data_scope=settings.deployment_mode,
        snapshot_hash=snapshot_hash,
        columns=COLUMNS,
        ticket_ids=[t.id for t in tickets],
        rows=rows,
        warnings=[
            "기관 매핑 미검증 · 파일 생성은 실제 이관이 아닙니다.",
            "개인정보 자동 검출은 완전하지 않습니다. 모든 셀을 직접 확인하세요.",
            "설명·위치는 담당자가 작성한 전달용 문구입니다. "
            "신고 원문이나 위치 확정 결과를 수정하지 않습니다.",
        ],
    )


@router.post("/exports/preview", response_model=ExportPreview)
async def preview_export(payload: ExportPreviewInput, request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatcher(request, db)
        return await capture(db, request.app.state.settings, payload)


def view(record, stamp):
    return ExportView(
        id=record.id,
        data_scope=record.namespace.split(":")[0],
        snapshot_hash=record.snapshot_hash,
        content_sha256=record.content_sha256,
        row_count=record.row_count,
        created_at=record.created_at,
        expires_at=record.expires_at,
        expired=record.expired_at is not None or record.expires_at <= stamp,
        download_path=f"/v1/admin/export.csv?export_id={record.id}",
    )


@router.post("/exports", response_model=ExportView)
async def generate_export(payload: ExportCreate, request: Request):
    settings = request.app.state.settings
    async with request.app.state.database.transaction() as db:
        principal = await dispatcher(request, db)
        encryption = cipher(settings)
        stamp = await db.scalar(select(func.clock_timestamp()))
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise HTTPException(422, "idempotency_key_required")
        request_hash = hash_value(payload.model_dump(mode="json"))
        record = await db.scalar(
            select(ExportRecord).where(
                ExportRecord.creator_id == principal.user.id,
                ExportRecord.namespace == namespace(settings),
                ExportRecord.request_key == key,
            )
        )
        if record:
            if record.request_hash != request_hash:
                raise HTTPException(409, "idempotency_conflict")
            return view(record, stamp)
        count = await db.scalar(
            select(func.count())
            .select_from(ExportRecord)
            .where(
                ExportRecord.creator_id == principal.user.id,
                ExportRecord.namespace == namespace(settings),
                ExportRecord.created_at >= stamp - timedelta(minutes=15),
            )
        )
        if count >= 5:
            raise HTTPException(429, "export_rate_limited")
        if payload.expected_snapshot_at.tzinfo is None:
            raise HTTPException(422, "export_snapshot_time_invalid")
        if payload.expected_snapshot_at > stamp or stamp - payload.expected_snapshot_at > timedelta(
            hours=24
        ):
            raise HTTPException(409, "export_snapshot_stale")
        preview = await capture(db, settings, payload)
        if preview.snapshot_hash != payload.expected_snapshot_hash:
            raise HTTPException(409, "export_snapshot_stale")
        if len(set(payload.confirmed_ticket_ids)) != len(payload.confirmed_ticket_ids) or set(
            payload.confirmed_ticket_ids
        ) != set(preview.ticket_ids):
            raise HTTPException(422, "export_rows_not_confirmed")
        artifact = CsvExport().prepare(preview.rows)
        snapshot = {
            "rows": preview.rows,
            "columns": preview.columns,
            "reason": payload.reason,
            "ticket_ids": [str(t) for t in preview.ticket_ids],
            "csv_base64": base64.b64encode(artifact.content).decode(),
        }
        record = ExportRecord(
            creator_id=principal.user.id,
            namespace=namespace(settings),
            request_key=key,
            request_hash=request_hash,
            snapshot_hash=preview.snapshot_hash,
            content_sha256=hashlib.sha256(artifact.content).hexdigest(),
            row_count=len(preview.rows),
            encrypted_audit=encryption.encrypt(
                json.dumps(
                    {
                        "reason": payload.reason,
                        "selection": payload.selection.model_dump(mode="json"),
                        "ticket_ids": [str(t) for t in preview.ticket_ids],
                        "confirmed_ticket_ids": [str(t) for t in payload.confirmed_ticket_ids],
                    },
                    ensure_ascii=False,
                ).encode()
            ).decode(),
            encrypted_snapshot=encryption.encrypt(
                json.dumps(snapshot, ensure_ascii=False).encode()
            ).decode(),
            expires_at=stamp + timedelta(hours=24),
        )
        db.add(record)
        await db.flush()
        result = view(record, stamp)
    # The audit row must commit before a successful response can be returned.
    return result


async def owned_record(db, request, export_id):
    principal = await dispatcher(request, db)
    record = await db.scalar(
        select(ExportRecord).where(ExportRecord.id == export_id).with_for_update()
    )
    if (
        not record
        or record.creator_id != principal.user.id
        or record.namespace != namespace(request.app.state.settings)
    ):
        raise HTTPException(404, "export_not_found")
    return principal, record


@router.get("/exports/{export_id}", response_model=ExportView)
async def get_export(export_id: uuid.UUID, request: Request):
    async with request.app.state.database.transaction() as db:
        _, record = await owned_record(db, request, export_id)
        return view(record, await db.scalar(select(func.clock_timestamp())))


@router.get("/export.csv", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
async def download_export(export_id: uuid.UUID, request: Request):
    expired = False
    async with request.app.state.database.transaction() as db:
        principal, record = await owned_record(db, request, export_id)
        stamp = await db.scalar(select(func.clock_timestamp()))
        if record.expires_at <= stamp or record.expired_at is not None:
            record.encrypted_snapshot, record.expired_at = None, record.expired_at or stamp
            expired = True
        else:
            try:
                content = base64.b64decode(
                    decode_snapshot(request.app.state.settings, record)["csv_base64"], validate=True
                )
            except (InvalidToken, TypeError, ValueError, KeyError):
                raise HTTPException(503, "export_snapshot_unavailable") from None
            if hashlib.sha256(content).hexdigest() != record.content_sha256:
                raise HTTPException(503, "export_snapshot_unavailable")
            db.add(ExportDownload(export_id=record.id, actor_id=principal.user.id))
            filename = f"baroai-{record.namespace.split(':')[0]}-{record.id}.csv"
    if expired:
        raise HTTPException(410, "export_expired")
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def expire_exports(database, settings, *, limit=100):
    """B19 retention hook: wipe ciphertext only, preserving audit metadata."""
    from app.dispatch_lock import dispatch_lock

    async with database.transaction() as db:
        await dispatch_lock(db)
        stamp = await db.scalar(select(func.clock_timestamp()))
        records = list(
            await db.scalars(
                select(ExportRecord)
                .where(
                    ExportRecord.namespace == namespace(settings),
                    ExportRecord.expires_at <= stamp,
                    ExportRecord.encrypted_snapshot.is_not(None),
                )
                .order_by(ExportRecord.expires_at)
                .limit(min(max(limit, 1), 100))
                .with_for_update()
            )
        )
        for record in records:
            record.encrypted_snapshot, record.expired_at = None, stamp
        return len(records)
