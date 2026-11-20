"""Dispatcher-only AI usage summary; all metrics come from the shared read service."""

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.security import authenticate, role
from app.services.ai_metrics import KST, read_metrics

router = APIRouter(prefix="/v1/admin", tags=["ai-usage"])


class AIUsageView(BaseModel):
    date: str
    timezone: str
    period_basis: str
    sent_n: int
    known_unsent_n: int
    sent_unknown_n: int
    cost_known_n: int
    cost_known_total_usd: str | None
    cost_mean_usd: str | None
    unresolved_reservations_n: int
    unresolved_reservations_usd: str | None
    latency_n: int
    latency_p50_ms: str | None
    provenance: str
    data_class: str


@router.get("/ai-usage", response_model=AIUsageView)
async def ai_usage(request: Request):
    async with request.app.state.database.transaction() as db:
        principal = await authenticate(request, db, worker_allowed=True)
        role(principal, "dispatcher")
        day = datetime.now(KST).date()
        result = await read_metrics(db, day, day)
    summary = result["summary"]
    cost = summary["cost_usd"]
    pending = summary["unsettled_reservations_usd"]
    latency = summary["duration_ms"]
    return AIUsageView(
        date=day.isoformat(),
        timezone="Asia/Seoul",
        period_basis="attempt_started_at",
        sent_n=summary["sent_n"],
        known_unsent_n=summary["known_unsent_n"],
        sent_unknown_n=summary["sent_unknown_n"],
        cost_known_n=cost["n"],
        cost_known_total_usd=cost["known_total"],
        cost_mean_usd=cost["mean"],
        unresolved_reservations_n=pending["n"],
        unresolved_reservations_usd=pending["total"],
        latency_n=latency["n"],
        latency_p50_ms=latency["p50"],
        provenance="mixed_or_unknown",
        data_class="unknown",
    )
