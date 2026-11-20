"""Dispatcher-only operational summary."""

from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.operations_filters import KST
from app.operations_metrics import read_operations
from app.security import authenticate, role

router = APIRouter(prefix="/v1/admin", tags=["operations"])


class CountRow(BaseModel):
    key: str | None
    count: int


class Duration(BaseModel):
    sample_n: int
    median_seconds: float | None
    small_sample: bool


class Durations(BaseModel):
    receipt_to_confirmation: Duration
    confirmation_to_assignment: Duration
    assignment_to_completion: Duration


class Longest(BaseModel):
    ticket_id: str
    public_no: str | None
    seconds: float


class WorkerCount(BaseModel):
    worker_id: str
    name: str
    open_n: int
    completed_n: int


class InformationCounts(BaseModel):
    waiting: int
    answered: int


class WaitingCounts(BaseModel):
    open_n: int
    long_n: int
    threshold_minutes_by_urgency: list[int]


class AIMetrics(BaseModel):
    attempt_period_basis: str
    live_attempts_n: int
    replay_attempts_n: int
    live_applied_ticket_n: int
    cached_applied_ticket_n: int
    replay_applied_ticket_n: int
    known_settled_cost_n: int
    known_settled_cost_usd: str | None
    safety_comparable_attempt_n: int
    safety_raised_attempt_n: int
    comparison_basis: str
    category_comparable_n: int
    category_edited_and_differs_n: int
    urgency_comparable_n: int
    urgency_overridden_n: int


class OperationsView(BaseModel):
    start: date
    end: date
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    data_class: Literal["unknown"] = "unknown"
    cohort_n: int
    unattributed_completed_n: int
    completion_trend_basis: str
    status: dict[str, int]
    information: InformationCounts
    waiting: WaitingCounts
    ai: AIMetrics
    categories: list[CountRow]
    category_groups: list[CountRow]
    buildings: list[CountRow]
    durations: Durations
    longest: list[Longest]
    received_by_day: dict[str, int]
    completed_by_day: dict[str, int]
    workers: list[WorkerCount]


@router.get("/operations", response_model=OperationsView)
async def operations(
    request: Request,
    period: Literal["today", "7d", "30d", "custom"] = "7d",
    start: date | None = None,
    end: date | None = None,
    category_group: str | None = None,
    building: str | None = None,
    urgency: int | None = None,
    category_code: str | None = None,
):
    today = datetime.now(KST).date()
    if period == "custom":
        if start is None or end is None:
            raise HTTPException(422, "operations_period_required")
    else:
        if start is not None or end is not None:
            raise HTTPException(422, "unexpected_operations_period")
        end = today
        start = today - timedelta(days={"today": 0, "7d": 6, "30d": 29}[period])
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        role(principal, "dispatcher")
        visibility = None
        if request.app.state.settings.deployment_mode == "public_demo":
            from app.security import now
            from app.tickets import public_visible_ticket

            visibility = public_visible_ticket(now())
        result = await read_operations(
            db, start, end, category_group, building, urgency, category_code, visibility
        )
    return OperationsView(start=start, end=end, **result)
