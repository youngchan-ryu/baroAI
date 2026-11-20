"""Transactional public intake controls; caller holds the dispatch lock."""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import math
import os
import sys
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.config import Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.models import AuthSession, PublicIntakeControl, PublicLimitEvent, Ticket, User
from app.public_staff import require_existing_marker
from app.security import now


def client_digest(request):
    settings = request.app.state.settings
    if settings.public_ip_boundary == "direct_local":
        if settings.app_env not in {"dev", "test"} or any(
            k.lower() == "forwarded"
            or k.lower().startswith("x-forwarded-")
            or k.lower().startswith("x-vercel-forwarded-")
            for k in request.headers
        ):
            raise HTTPException(503, "public_ip_boundary_unavailable")
        try:
            addr = ipaddress.ip_address(request.client.host)
            if not addr.is_loopback:
                raise ValueError
        except (ValueError, AttributeError):
            raise HTTPException(503, "public_ip_boundary_unavailable") from None
    else:
        if os.environ.get("VERCEL") != "1" or os.environ.get("VERCEL_ENV") not in {
            "preview",
            "production",
        }:
            raise HTTPException(503, "public_ip_boundary_unavailable")
        values = request.headers.getlist("x-vercel-forwarded-for")
        try:
            if len(values) != 1 or "%" in values[0] or "," in values[0]:
                raise ValueError
            addr = ipaddress.ip_address(values[0])
            if str(addr) != values[0]:
                raise ValueError
        except ValueError:
            raise HTTPException(503, "public_ip_boundary_unavailable") from None
    key = hmac.new(
        settings.jwt_secret.get_secret_value().encode(), b"public-ip-rate-limit-v1", hashlib.sha256
    ).digest()
    return hmac.new(key, addr.packed, hashlib.sha256).hexdigest()


async def control(db):
    row = await db.get(PublicIntakeControl, 1)
    if row is None:
        row = PublicIntakeControl(id=1, paused=False)
        db.add(row)
        await db.flush()
    return row


def limited(kind, retry=None):
    headers = {"Retry-After": str(max(1, math.ceil(retry)))} if retry is not None else None
    raise HTTPException(429, kind, headers=headers)


async def check_guest(db, request):
    settings = request.app.state.settings
    ip = client_digest(request)
    if (await control(db)).paused:
        raise HTTPException(503, "public_intake_paused")
    current = now()
    active = await db.scalar(
        select(func.count())
        .select_from(AuthSession)
        .join(User)
        .where(
            User.role == "reporter",
            AuthSession.revoked.is_(False),
            AuthSession.expires_at > current,
        )
    )
    if active >= settings.public_active_sessions_limit:
        next_expiry = await db.scalar(
            select(AuthSession.expires_at)
            .join(User)
            .where(
                User.role == "reporter",
                AuthSession.revoked.is_(False),
                AuthSession.expires_at > current,
            )
            .order_by(AuthSession.expires_at)
            .offset(active - settings.public_active_sessions_limit)
            .limit(1)
        )
        limited("public_session_limit", (next_expiry - current).total_seconds())
    cutoff = current - timedelta(hours=1)
    rows = list(
        await db.scalars(
            select(PublicLimitEvent.created_at)
            .where(
                PublicLimitEvent.kind == "guest",
                PublicLimitEvent.subject == ip,
                PublicLimitEvent.created_at > cutoff,
            )
            .order_by(PublicLimitEvent.created_at)
        )
    )
    if len(rows) >= settings.public_ip_hourly_limit:
        limited(
            "public_ip_limit",
            (
                rows[len(rows) - settings.public_ip_hourly_limit] + timedelta(hours=1) - current
            ).total_seconds(),
        )
    db.add(PublicLimitEvent(kind="guest", subject=ip, created_at=current))
    await db.execute(delete(PublicLimitEvent).where(PublicLimitEvent.created_at <= cutoff))


async def check_ticket(db, request, principal):
    settings = request.app.state.settings
    if (await control(db)).paused:
        raise HTTPException(503, "public_intake_paused")
    current = now()
    cutoff = current - timedelta(hours=1)
    subject = str(principal.session.id)
    rows = list(
        await db.scalars(
            select(PublicLimitEvent.created_at)
            .where(
                PublicLimitEvent.kind == "ticket",
                PublicLimitEvent.subject == subject,
                PublicLimitEvent.created_at > cutoff,
            )
            .order_by(PublicLimitEvent.created_at)
        )
    )
    if len(rows) >= settings.public_ticket_hourly_limit:
        limited(
            "public_ticket_limit",
            (
                rows[len(rows) - settings.public_ticket_hourly_limit] + timedelta(hours=1) - current
            ).total_seconds(),
        )
    if (
        await db.scalar(select(func.count()).select_from(Ticket))
        >= settings.public_ticket_storage_limit
    ):
        limited("public_storage_limit")
    db.add(PublicLimitEvent(kind="ticket", subject=subject, created_at=current))
    await db.execute(delete(PublicLimitEvent).where(PublicLimitEvent.created_at <= cutoff))


async def set_paused(database, settings, paused):
    async with database.transaction() as db:
        await dispatch_lock(db)
        await require_existing_marker(db, settings)
        (await control(db)).paused = paused


def main():
    parser = argparse.ArgumentParser(description="Public demo intake operator")
    parser.add_argument("action", choices=["stop", "resume"])
    args = parser.parse_args()

    async def run():
        settings = Settings()
        database = Database(settings)
        try:
            await set_paused(database, settings, args.action == "stop")
        finally:
            await database.close()

    try:
        asyncio.run(run())
    except Exception:
        print("Public intake operation failed.", file=sys.stderr)
        raise SystemExit(1) from None
    print("Public intake updated.")


if __name__ == "__main__":
    main()
