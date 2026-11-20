"""Named deterministic heuristics, respecting the same service/return hard limits."""

import time

from app.planning.schemas import PlanResult, Route, Unassigned, Visit
from app.planning.validation import (
    earliest_arrival,
    earliest_return,
    eligible,
    input_hash,
    metrics_for,
    unassigned_reason,
    validate_plan,
)


def build_result(data, routes, *, policy, status, started, underlying=None, fallback=False):
    visited = {v.ticket_id for r in routes for v in r.visits}
    unassigned = tuple(
        Unassigned(
            ticket_id=t.id,
            reason=(
                "solver_objective_or_search_limit"
                if policy == "ortools" and unassigned_reason(data, t) == "capacity_or_order"
                else unassigned_reason(data, t)
            ),
            urgent_escalation=t.urgency <= 1,
        )
        for t in data.tickets
        if t.id not in visited
    )
    result = PlanResult(
        policy=policy,
        solver_status=status,
        underlying_solver_status=underlying,
        routes=tuple(routes),
        unassigned=unassigned,
        metrics=metrics_for(data, routes, unassigned),
        input_hash=input_hash(data),
        assumptions_version=data.assumptions_version,
        matrix_version=data.travel.version,
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        solver_limit_seconds=data.solver_seconds,
        seed=data.seed,
        determinism_guaranteed=policy != "ortools" and not fallback,
        fallback_used=fallback,
        fallback_policy="urgency_fifo" if fallback else None,
    )
    validate_plan(data, result)
    return result


def _heuristic(data, policy):
    started = time.monotonic()
    travel = data.travel.minutes
    pending = {t.id: t for t in data.tickets}
    workers = {w.id: w for w in data.workers}
    cursor = {w.id: w.shift_start_min for w in data.workers}
    point = {w.id: w.start_node for w in data.workers}
    visits = {w.id: [] for w in data.workers}
    travel_total = {w.id: 0 for w in data.workers}
    available = set(workers)
    while pending and available:
        wid = min(available, key=lambda key: (cursor[key], key))
        worker = workers[wid]
        options = []
        for ticket in pending.values():
            if not eligible(data, worker, ticket):
                continue
            leg = travel[point[wid]][ticket.node]
            arrival = earliest_arrival(worker, cursor[wid], leg, ticket)
            if (
                earliest_return(
                    worker, arrival + ticket.service_min, travel[ticket.node][worker.end_node]
                )
                > worker.shift_end_min
            ):
                continue
            support = (
                data.region_support_penalty
                if data.mode == "region_support" and worker.region != ticket.region
                else 0
            )
            if policy == "fifo":
                key = (ticket.release_min, ticket.id)
            elif policy == "urgency_fifo":
                key = (ticket.urgency, ticket.release_min, ticket.id)
            else:
                key = (
                    0 if ticket.urgency <= 1 else 1,
                    leg + support,
                    ticket.release_min,
                    ticket.id,
                )
            options.append((key, ticket, arrival, leg))
        if not options:
            available.remove(wid)
            continue
        _, ticket, arrival, leg = min(options, key=lambda item: item[0])
        visits[wid].append(
            Visit(
                ticket_id=ticket.id,
                arrival_min=arrival,
                completion_min=arrival + ticket.service_min,
            )
        )
        cursor[wid], point[wid] = arrival + ticket.service_min, ticket.node
        travel_total[wid] += leg
        del pending[ticket.id]
    routes = []
    for worker in data.workers:
        if visits[worker.id]:
            back = travel[point[worker.id]][worker.end_node]
            routes.append(
                Route(
                    worker_id=worker.id,
                    departure_min=worker.shift_start_min,
                    return_min=earliest_return(worker, cursor[worker.id], back),
                    visits=tuple(visits[worker.id]),
                    travel_min=travel_total[worker.id] + back,
                )
            )
    return build_result(data, routes, policy=policy, status="heuristic", started=started)


def fifo(data):
    return _heuristic(data, "fifo")


def urgency_fifo(data):
    return _heuristic(data, "urgency_fifo")


def nearest(data):
    return _heuristic(data, "nearest")
