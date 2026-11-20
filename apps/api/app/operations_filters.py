"""Shared, opt-in dispatcher cohort predicates for operations and ticket list."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import case, func, select

from app.catalog import CATEGORY_MAP
from app.models import Ticket

KST = ZoneInfo("Asia/Seoul")


def bounds(start: date, end: date):
    if end == date.max or end < start or (end - start).days > 365:
        raise HTTPException(422, "invalid_operations_period")
    return (
        datetime.combine(start, time.min, KST).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, KST).astimezone(UTC),
    )


def category_domain():
    fallback = case(
        *[(Ticket.category_code == code, item["domain"]) for code, item in CATEGORY_MAP.items()],
        else_="UNKNOWN",
    )
    return func.coalesce(Ticket.catalog_snapshot["domain"].astext, fallback)


def conditions(
    start: date,
    end: date,
    category_group: str | None = None,
    building: str | None = None,
    urgency: int | None = None,
    category_code: str | None = None,
):
    first, after = bounds(start, end)
    result = [Ticket.created_at >= first, Ticket.created_at < after]
    if category_group is not None:
        if not category_group or len(category_group) > 100:
            raise HTTPException(422, "invalid_category_group")
        result.append(category_domain() == category_group)
    if building is not None:
        if not building or len(building) > 100:
            raise HTTPException(422, "invalid_building")
        result.append(Ticket.location_data["selected"]["building_code"].astext == building)
    if category_code is not None:
        if not category_code or len(category_code) > 100:
            raise HTTPException(422, "invalid_category_code")
        result.append(Ticket.category_code == category_code)
    if urgency is not None:
        if urgency not in range(4):
            raise HTTPException(422, "invalid_urgency")
        result.append(Ticket.effective_urgency == urgency)
    return result


def cohort(start, end, category_group=None, building=None, urgency=None):
    return select(Ticket.id).where(*conditions(start, end, category_group, building, urgency))
