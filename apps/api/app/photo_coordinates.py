"""Remove expired photo coordinates without touching photo storage."""

from datetime import timedelta

from sqlalchemy import or_, update

from app.models import Upload
from app.security import now


async def purge_photo_coordinates(database, *, at=None):
    stamp = at or now()
    async with database.transaction() as db:
        await db.execute(
            update(Upload).where(
                Upload.photo_coordinates.is_not(None),
                or_(Upload.deleted.is_(True), Upload.delete_after <= stamp,
                    (Upload.ticket_id.is_(None) &
                     (Upload.created_at <= stamp - timedelta(hours=1)))),
            ).values(photo_coordinates=None)
        )
