"""Independent checks of every route against original immutable planning inputs."""

import hashlib

from app.planning.schemas import Metrics, PlanningInput, PlanResult, TicketSpec, WorkerSpec


class PlanValidationError(ValueError):
    pass


def input_hash(data: PlanningInput) -> str:
    excluded = {"dataset_hash", "dataset_revision"} if data.data_kind == "synthetic" else set()
    return hashlib.sha256(data.model_dump_json(exclude=excluded).encode()).hexdigest()


def eligible(data: PlanningInput, worker: WorkerSpec, ticket: TicketSpec) -> bool:
    return ticket.domain in worker.skills and (
        data.mode != "region" or worker.region == ticket.region
    )


def available_start(worker, start, duration):
    if worker.lunch_min:
        lunch_start, lunch_end = worker.lunch_min
        if start < lunch_end and start + duration > lunch_start:
            return lunch_end
    return start


def travel_end(worker, start, duration):
    end = start + duration
    if worker.lunch_min:
        lunch_start, lunch_end = worker.lunch_min
        if start < lunch_end and end > lunch_start:
            end += lunch_end - max(start, lunch_start)
    return end


def earliest_arrival(worker, start, travel, ticket):
    arrival = max(travel_end(worker, start, travel), ticket.release_min)
    return available_start(worker, arrival, ticket.service_min)


def earliest_return(worker, completion, travel):
    return travel_end(worker, completion, travel)


def individually_feasible(data, worker, ticket):
    travel = data.travel.minutes
    arrival = earliest_arrival(
        worker, worker.shift_start_min, travel[worker.start_node][ticket.node], ticket
    )
    return (
        earliest_return(worker, arrival + ticket.service_min, travel[ticket.node][worker.end_node])
        <= worker.shift_end_min
    )


def unassigned_reason(data, ticket):
    allowed = [w for w in data.workers if eligible(data, w, ticket)]
    if not allowed:
        return "no_eligible_worker"
    if not any(individually_feasible(data, w, ticket) for w in allowed):
        return "outside_shift"
    return "capacity_or_order"


def metrics_for(data, routes, unassigned):
    tickets = {t.id: t for t in data.tickets}
    workers = {w.id: w for w in data.workers}
    travel_total, service_total, late_count, late_total, return_travel = 0, 0, 0, 0, 0
    for route in routes:
        travel_total += route.travel_min
        worker = workers[route.worker_id]
        if route.visits:
            return_travel += data.travel.minutes[tickets[route.visits[-1].ticket_id].node][
                worker.end_node
            ]
        for visit in route.visits:
            ticket = tickets[visit.ticket_id]
            service_total += ticket.service_min
            late = max(0, visit.arrival_min - ticket.deadline_min)
            late_count += int(late > 0)
            late_total += late
    return Metrics(
        travel_min=travel_total,
        service_min=service_total,
        late_count=late_count,
        late_minutes=late_total,
        unassigned_count=len(unassigned),
        urgent_unassigned_count=sum(u.urgent_escalation for u in unassigned),
        return_travel_min=return_travel,
    )


def validate_plan(data: PlanningInput, result: PlanResult) -> None:
    """Reject corruption; never repair, silently drop, or relabel an invalid plan."""
    tickets, workers = {t.id: t for t in data.tickets}, {w.id: w for w in data.workers}
    seen, used_workers = set(), set()

    def require(condition, code):
        if not condition:
            raise PlanValidationError(code)

    require(result.input_hash == input_hash(data), "input_hash_mismatch")
    require(
        result.matrix_version == data.travel.version
        and result.assumptions_version == data.assumptions_version,
        "version_mismatch",
    )
    for route in result.routes:
        require(
            route.worker_id in workers and route.worker_id not in used_workers,
            "unknown_or_duplicate_worker",
        )
        used_workers.add(route.worker_id)
        worker = workers[route.worker_id]
        require(bool(route.visits), "empty_route")
        require(
            worker.shift_start_min <= route.departure_min <= worker.shift_end_min,
            "departure_outside_shift",
        )
        if worker.lunch_min:
            lunch_start, lunch_end = worker.lunch_min
            require(
                not lunch_start <= route.departure_min < lunch_end
                and not lunch_start < route.return_min < lunch_end,
                "route_endpoint_during_lunch",
            )
        point, clock, travel = worker.start_node, route.departure_min, 0
        for visit in route.visits:
            require(
                visit.ticket_id in tickets and visit.ticket_id not in seen,
                "unknown_or_duplicate_ticket",
            )
            seen.add(visit.ticket_id)
            ticket = tickets[visit.ticket_id]
            # Explicit independent domain/region validation, not solver allowed-vehicle output.
            require(ticket.domain in worker.skills, "domain_mismatch")
            require(data.mode != "region" or worker.region == ticket.region, "region_mismatch")
            leg = data.travel.minutes[point][ticket.node]
            require(
                visit.arrival_min >= earliest_arrival(worker, clock, leg, ticket),
                "arrival_or_release_violation",
            )
            require(
                visit.arrival_min == available_start(worker, visit.arrival_min, ticket.service_min),
                "service_during_lunch",
            )
            require(
                visit.completion_min == visit.arrival_min + ticket.service_min,
                "service_duration_mismatch",
            )
            require(visit.completion_min <= worker.shift_end_min, "service_after_shift")
            travel += leg
            point, clock = ticket.node, visit.completion_min
        back = data.travel.minutes[point][worker.end_node]
        travel += back
        require(
            earliest_return(worker, clock, back) <= route.return_min <= worker.shift_end_min,
            "return_after_shift_or_teleport",
        )
        require(route.travel_min == travel, "travel_metric_mismatch")
    for item in result.unassigned:
        require(
            item.ticket_id in tickets and item.ticket_id not in seen,
            "duplicate_or_unknown_unassigned",
        )
        seen.add(item.ticket_id)
        require(
            item.urgent_escalation == (tickets[item.ticket_id].urgency <= 1),
            "urgent_escalation_missing",
        )
        if item.reason in {"no_eligible_worker", "outside_shift"}:
            require(
                item.reason == unassigned_reason(data, tickets[item.ticket_id]),
                "unassigned_reason_mismatch",
            )
    require(seen == set(tickets), "missing_ticket")
    if (
        result.policy == "ortools"
        and not result.fallback_used
        and result.solver_status
        in {"success", "partial_success_local_optimum_not_reached", "optimal_status"}
    ):
        require(
            not any(u.urgent_escalation for u in result.unassigned),
            "mandatory_urgent_missing_from_solver_solution",
        )
    require(
        result.metrics == metrics_for(data, result.routes, result.unassigned), "metrics_mismatch"
    )
    require(not result.optimality_proven, "optimality_claim_unavailable")
    require(
        not result.fallback_used
        or (result.fallback_policy is not None and result.underlying_solver_status is not None),
        "unlabelled_fallback",
    )
