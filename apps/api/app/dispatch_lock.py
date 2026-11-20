"""DB-local ordering gate, acquired before authentication/row locks, never during solve."""

from sqlalchemy import text


async def dispatch_lock(db):
    await db.execute(text("SELECT pg_advisory_xact_lock(24002216)"))
