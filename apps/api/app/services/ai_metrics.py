"""Read-only aggregate of processing attempts, without exposing per-record data."""

import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import ProcessingAttempt, ProcessingJob

KST = ZoneInfo("Asia/Seoul")
ERROR_CODES = {
    "timeout",
    "provider_rate_limited",
    "provider_unavailable",
    "transport_failed",
    "provider_auth_failed",
    "invalid_schema",
    "invalid_json",
    "invalid_output",
    "output_truncated",
    "refusal",
    "policy_violation",
    "daily_budget_exceeded",
    "ticket_cap_exceeded",
    "daily_cap_exceeded",
    "budget_invalid",
    "budget_authorization_required",
    "lease_lost",
    "interrupted",
    "input_unchanged",
    "skipped_short",
    "gateway_failed",
    "live_processing_unavailable",
}


def _number(value):
    try:
        if isinstance(value, bool) or value is None:
            return None
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= Decimal("1000000000") else None
    except (InvalidOperation, ValueError):
        return None


def _token(value):
    return value if type(value) is int and 0 <= value <= 1000000000 else None


def _model(value):
    if (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value)
        and not value.lower().startswith(("sk-", "key-", "bearer-"))
        and not re.search(r"[a-fA-F0-9]{24,}|[0-9]{8,}", value)
    ):
        return value
    return "unknown"


def _price_date(value):
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else "unknown"
    except ValueError:
        return "unknown"


def _stats(values):
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    values = sorted(values)
    n = len(values)

    def percentile(p):
        return str(values[math.ceil(p * n) - 1]) if n >= 20 else None

    return {
        "n": n,
        "mean": str(sum(values) / n),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "max": str(values[-1]),
    }


def _ratio(numerator, denominator):
    return str(Decimal(numerator) / denominator) if denominator else None


def _bucket(rows):
    sent = [r for r in rows if r["mode"] == "live" and r["meta"].get("sent") is True]
    unsent = [r for r in rows if r["mode"] == "live" and r["meta"].get("sent") is False]
    unknown = [
        r for r in rows if r["mode"] == "live" and r["meta"].get("sent") not in (True, False)
    ]
    # Exact booleans only: integers 0/1 must not be interpreted as a send observation.
    unknown += [
        r
        for r in rows
        if r["mode"] == "live"
        and r["meta"].get("sent") in (True, False)
        and type(r["meta"].get("sent")) is not bool
    ]
    costs = [v for r in sent if (v := _number(r["meta"].get("cost_usd"))) is not None]
    unresolved = [
        v
        for r in rows
        if r["mode"] == "live"
        and r["meta"].get("sent") is not False
        and (r["meta"].get("sent") is not True or _number(r["meta"].get("cost_usd")) is None)
        if (v := _number(r["meta"].get("reserved_usd"))) is not None
    ]
    unsent_reserved = [
        v
        for r in unsent
        if _number(r["meta"].get("cost_usd")) is None
        if (v := _number(r["meta"].get("reserved_usd"))) is not None
    ]
    tokens = {}
    for name in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
        tokens[name] = _stats(
            [Decimal(v) for r in sent if (v := _token(r["meta"].get(name))) is not None]
        )
    durations = [v for r in sent if (v := _number(r["meta"].get("duration_ms"))) is not None]
    errors = Counter()
    for row in sent:
        error = row["error"]
        if error:
            errors[error if error in ERROR_CODES else "other"] += 1
    applied = sum(r["applied"] is True for r in sent)
    live_rows = [r for r in rows if r["mode"] == "live"]
    cache_known = [
        r
        for r in live_rows
        if type(r["meta"].get("sent")) is bool
        and (
            r["meta"].get("cache_hit") is False
            or (r["meta"].get("cache_hit") is True and r["meta"].get("sent") is False)
        )
    ]
    reused = [r for r in unsent if r["meta"].get("cache_hit") is True]
    token_pairs = [(r["meta"].get("input_tokens"), r["meta"].get("cached_tokens")) for r in sent]
    token_pairs = [
        (i, c) for i, c in token_pairs if _token(i) is not None and _token(c) is not None and c <= i
    ]
    skip_known = [
        r
        for r in unsent
        if r["error"] in ("skipped_short", "input_unchanged")
        or r["meta"].get("skipped") in ("short_input", "input_unchanged")
    ]
    image_omitted = [
        r
        for r in sent
        if r["meta"].get("images_skipped")
        in ("image_limit", "image_resize_failed", "image_rejected", "vision_model_unconfigured")
    ]

    discard_known = [r for r in sent if r["meta"].get("discarded") is not None]
    return {
        "attempts_n": len(rows),
        "replay_attempts_n": sum(r["mode"] == "replay" for r in rows),
        "mode_unknown_n": sum(r["mode"] not in ("live", "replay") for r in rows),
        "live_attempts_n": len(sent) + len(unsent) + len(unknown),
        "sent_n": len(sent),
        "known_unsent_n": len(unsent),
        "sent_unknown_n": len(unknown),
        "applied_n": applied,
        "applied_denominator": len(sent),
        "success_rate": _ratio(applied, len(sent)),
        "success_definition": "draft applied; excludes received but discarded responses",
        "errors": dict(sorted(errors.items())),
        "cost_usd": {"known_total": str(sum(costs)) if costs else None, **_stats(costs)},
        "unsettled_reservations_usd": {
            "n": len(unresolved),
            "total": str(sum(unresolved)) if unresolved else None,
            "amount_unknown_n": sum(
                r["mode"] == "live"
                and r["meta"].get("sent") is not False
                and (
                    r["meta"].get("sent") is not True or _number(r["meta"].get("cost_usd")) is None
                )
                and _number(r["meta"].get("reserved_usd")) is None
                for r in rows
            ),
        },
        "known_unsent_reservations_usd": {
            "n": len(unsent_reserved),
            "total": str(sum(unsent_reserved)) if unsent_reserved else None,
        },
        "tokens": tokens,
        "duration_ms": _stats(durations),
        "cache_reuse": {
            "reused_n": len(reused),
            "known_cache_flag_n": len(cache_known),
            "known_rate": _ratio(len(reused), len(cache_known)),
            "eligible_live_attempts_n": len(live_rows),
            "definition": "explicit cache hit with no provider send",
        },
        "provider_cached_tokens": {
            "known_n": len(token_pairs),
            "sent_denominator": len(sent),
            "cached_total": sum(c for _, c in token_pairs) if token_pairs else None,
            "input_total": sum(i for i, _ in token_pairs) if token_pairs else None,
            "ratio": _ratio(sum(c for _, c in token_pairs), sum(i for i, _ in token_pairs)),
        },
        "skipped": {
            "known_n": len(skip_known),
            "eligible_live_attempts_n": len(live_rows),
            "definition": "known unsent short or duplicate input",
        },
        "sent_image_omission_n": len(image_omitted),
        "discarded": {"known_n": len(discard_known), "denominator": len(sent)},
        "retry": {
            "n": sum(r["attempt_no"] > 1 for r in sent),
            "denominator": len(sent),
            "rate": _ratio(sum(r["attempt_no"] > 1 for r in sent), len(sent)),
        },
    }


def aggregate(rows, start: date, end: date):
    """Rows are already scoped to [start, end] KST; no row IDs appear in output."""
    groups, days, tickets = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        m = row["meta"]
        kind = row["kind"] if row["kind"] in {"ticket_analysis", "report_analysis"} else "unknown"
        protocol = m.get("protocol") if m.get("protocol") in ("v1", "v2") else "unknown"
        deployment = m.get("deployment_mode")
        source = (
            deployment
            if deployment in ("demo", "service", "prototype", "public_demo")
            else "unknown"
        )
        mode = row["mode"] if row["mode"] in ("live", "replay") else "unknown"
        key = (
            kind,
            mode,
            protocol,
            _model(m.get("model")),
            _price_date(m.get("price_as_of")),
            source,
        )
        groups[key].append(row)
        days[row["started_at"].astimezone(KST).date().isoformat()].append(row)
        tickets[row["ticket_id"]].append(row)
    complete = []
    for attempts in tickets.values():
        if any(r["mode"] not in ("live", "replay") for r in attempts):
            continue
        live = [r for r in attempts if r["mode"] == "live"]
        if not live or not any(r["meta"].get("sent") is True for r in live):
            continue
        if any(type(r["meta"].get("sent")) is not bool for r in live):
            continue
        sent = [r for r in live if r["meta"].get("sent") is True]
        values = [_number(r["meta"].get("cost_usd")) for r in sent]
        if all(v is not None for v in values):
            complete.append(sum(values))
    eligible_safety = [
        r
        for r in rows
        if r["mode"] == "live" and r["kind"] == "ticket_analysis" and r["applied"] is True
    ]
    return {
        "period": {
            "from": start.isoformat(),
            "through": end.isoformat(),
            "timezone": "Asia/Seoul",
            "basis": "attempt_started_at",
        },
        "source": "unknown",
        "data_class": "unknown",
        "summary": _bucket(rows),
        "groups": [
            {
                "kind": k[0],
                "mode": k[1],
                "protocol": k[2],
                "model": k[3],
                "price_as_of": k[4],
                "source": k[5],
                **_bucket(v),
            }
            for k, v in sorted(groups.items())
        ],
        "days": [{"date": k, **_bucket(v)} for k, v in sorted(days.items())],
        "observed_ticket_cost_usd": {
            **_stats(complete),
            "definition": "period attempts with all observed live sends costed; not lifecycle cost",
        },
        "safety_rule_effect": {
            "known_n": sum(
                type(r["meta"].get("rule_floor_raised")) is bool for r in eligible_safety
            ),
            "raised_n": sum(r["meta"].get("rule_floor_raised") is True for r in eligible_safety),
            "eligible_denominator": len(eligible_safety),
        },
        "notes": [
            "legacy source and all data classes may be unavailable",
            "percentiles require at least 20 values",
            "cohort is KST attempt start date, not reservation day or daily budget ledger",
            "sent means dispatch started, not confirmed receipt",
            "cost is usage-based, not a provider invoice",
            "missing metadata is unknown",
        ],
    }


async def read_metrics(db, start: date, end: date):
    if end < start or (end - start).days > 365:
        raise ValueError("invalid_period")
    lower = datetime.combine(start, time.min, KST).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), time.min, KST).astimezone(UTC)
    result = await db.execute(
        select(
            ProcessingAttempt.started_at,
            ProcessingAttempt.mode,
            ProcessingAttempt.attempt_no,
            ProcessingAttempt.metadata_json,
            ProcessingAttempt.error_code,
            ProcessingAttempt.applied,
            ProcessingJob.kind,
            ProcessingJob.ticket_id,
        )
        .join(ProcessingJob, ProcessingJob.id == ProcessingAttempt.job_id)
        .where(ProcessingAttempt.started_at >= lower, ProcessingAttempt.started_at < upper)
    )
    rows = [
        {
            "started_at": r.started_at,
            "mode": r.mode,
            "attempt_no": r.attempt_no,
            "meta": r.metadata_json if isinstance(r.metadata_json, dict) else {},
            "error": r.error_code,
            "applied": r.applied,
            "kind": r.kind,
            "ticket_id": r.ticket_id,
        }
        for r in result
    ]
    return aggregate(rows, start, end)
