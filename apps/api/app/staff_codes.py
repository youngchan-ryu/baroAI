"""Display-only codes derived from the current eligible worker set."""

from sqlalchemy import select

from app.auth_service import worker_eligible
from app.models import User


async def eligible_workers(db, settings):
    workers = list(
        (await db.scalars(select(User).where(User.role == "worker").order_by(User.id))).all()
    )
    workers = [w for w in workers if worker_eligible(w, settings)]
    if settings.deployment_mode == "public_demo":
        from app.public_staff import staff_session_active

        workers = [w for w in workers if await staff_session_active(db, w.id)]
    return workers


def staff_codes(workers):
    ids = [worker.id.hex for worker in workers]
    return {
        worker.id: next(
            value[:n]
            for n in range(8, 33)
            if sum(other.startswith(value[:n]) for other in ids) == 1
        )
        for worker, value in zip(workers, ids, strict=True)
    }
