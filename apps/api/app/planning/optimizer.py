"""Real bounded OR-Tools routing, preserving actual status and named fallback."""

import time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.planning.baselines import build_result, urgency_fifo
from app.planning.schemas import Route, Visit
from app.planning.validation import eligible, individually_feasible, validate_plan

STATUS = {
    0: "not_solved",
    1: "success",
    2: "partial_success_local_optimum_not_reached",
    3: "fail_no_solution_found",
    4: "timeout_no_solution",
    5: "invalid_model",
    6: "proven_infeasible",
    7: "optimal_status",
}
LATE_WEIGHTS = (1000, 100, 10, 1)
DROP_PENALTIES = {2: 5000, 3: 500}


def _fallback(data, started, underlying):
    heuristic = urgency_fifo(data)
    result = heuristic.model_copy(
        update={
            "policy": "ortools",
            "solver_status": "heuristic_fallback",
            "underlying_solver_status": underlying,
            "fallback_used": True,
            "fallback_policy": "urgency_fifo",
            "determinism_guaranteed": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        }
    )
    validate_plan(data, result)
    return result


def solve_ortools(data, *, _solve=None):
    """The private solve hook enables zero-budget/status tests, never HTTP input.

    Urgency 0/1 nodes are mandatory in the solver model. If that model yields no
    solution, a clearly named heuristic may leave urgent work unassigned with
    escalation; it does not pretend the mandatory model was feasible.
    """
    started = time.monotonic()
    if not data.tickets:
        return build_result(data, (), policy="ortools", status="not_run_empty", started=started)
    if not data.workers:
        return _fallback(data, started, "not_run_no_workers")
    for ticket in data.tickets:
        if ticket.urgency <= 1 and not any(
            eligible(data, w, ticket) and individually_feasible(data, w, ticket)
            for w in data.workers
        ):
            return _fallback(data, started, "not_run_urgent_precheck_infeasible")
    n = len(data.tickets)
    physical = [t.node for t in data.tickets]
    service = [t.service_min for t in data.tickets]
    starts, ends = [], []
    for worker in data.workers:
        starts.append(len(physical))
        physical.append(worker.start_node)
        service.append(0)
        ends.append(len(physical))
        physical.append(worker.end_node)
        service.append(0)
    manager = pywrapcp.RoutingIndexManager(len(physical), len(data.workers), starts, ends)
    routing = pywrapcp.RoutingModel(manager)
    matrix = data.travel.minutes

    def transit(left, right):
        if routing.IsStart(left) and routing.IsEnd(right):
            return 0  # An unused worker is not dispatched from start to end.
        a, b = manager.IndexToNode(left), manager.IndexToNode(right)
        return service[a] + matrix[physical[a]][physical[b]]

    transit_index = routing.RegisterTransitCallback(transit)
    horizon = max(
        max(w.shift_end_min for w in data.workers),
        max(t.release_min + t.service_min for t in data.tickets),
    )
    routing.AddDimension(transit_index, horizon, horizon, False, "Time")
    clock = routing.GetDimensionOrDie("Time")
    # Hold references: each vehicle's region-support cost callback differs.
    callbacks = []
    lunches = []
    service_by_index = [service[manager.IndexToNode(i)] for i in range(routing.Size())]
    for k, worker in enumerate(data.workers):

        def cost(left, right, worker=worker):
            if routing.IsStart(left) and routing.IsEnd(right):
                return 0
            a, b = manager.IndexToNode(left), manager.IndexToNode(right)
            support = (
                data.region_support_penalty
                if b < n
                and data.mode == "region_support"
                and worker.region != data.tickets[b].region
                else 0
            )
            return matrix[physical[a]][physical[b]] + support

        callbacks.append(cost)
        routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(cost), k)
        clock.CumulVar(routing.Start(k)).SetRange(worker.shift_start_min, worker.shift_end_min)
        clock.CumulVar(routing.End(k)).SetRange(worker.shift_start_min, worker.shift_end_min)
        if worker.lunch_min:
            lunch_start, lunch_end = worker.lunch_min
            lunch = routing.solver().FixedDurationIntervalVar(
                lunch_start, lunch_start, lunch_end - lunch_start, False, f"lunch_{k}"
            )
            lunches.append(lunch)
            # Service cannot overlap lunch. Time reserved for the break adds to trip elapsed time.
            clock.SetBreakIntervalsOfVehicle([lunch], k, service_by_index)
            clock.CumulVar(routing.Start(k)).RemoveInterval(lunch_start, lunch_end - 1)
            if lunch_end - lunch_start > 1:
                clock.CumulVar(routing.End(k)).RemoveInterval(lunch_start + 1, lunch_end - 1)
        routing.AddVariableMinimizedByFinalizer(clock.CumulVar(routing.Start(k)))
        routing.AddVariableMinimizedByFinalizer(clock.CumulVar(routing.End(k)))
    for i, ticket in enumerate(data.tickets):
        index = manager.NodeToIndex(i)
        allowed = [k for k, w in enumerate(data.workers) if eligible(data, w, ticket)]
        if ticket.urgency >= 2:
            routing.AddDisjunction([index], DROP_PENALTIES[ticket.urgency])
        if allowed:
            routing.VehicleVar(index).SetValues(allowed + [-1])
        else:
            routing.ActiveVar(index).SetValue(0)
        clock.CumulVar(index).SetRange(ticket.release_min, horizon)
        clock.SetCumulVarSoftUpperBound(index, ticket.deadline_min, LATE_WEIGHTS[ticket.urgency])
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromMilliseconds(max(1, int(data.solver_seconds * 1000)))
    # Routing's classic CP search has no generic seed field. Do not misrepresent sat seed.
    if _solve:
        solution, status_code = _solve(routing, params)
    else:
        solution = routing.SolveWithParameters(params)
        status_code = routing.status()
    status = STATUS.get(status_code, f"unknown_status_{status_code}")
    if solution is None:
        return _fallback(data, started, status)
    routes = []
    for k, worker in enumerate(data.workers):
        current = routing.Start(k)
        departure = solution.Value(clock.CumulVar(current))
        visits = []
        previous = worker.start_node
        total = 0
        while not routing.IsEnd(current):
            node = manager.IndexToNode(current)
            if node < n:
                ticket = data.tickets[node]
                arrival = solution.Value(clock.CumulVar(current))
                visits.append(
                    Visit(
                        ticket_id=ticket.id,
                        arrival_min=arrival,
                        completion_min=arrival + ticket.service_min,
                    )
                )
                total += matrix[previous][ticket.node]
                previous = ticket.node
            current = solution.Value(routing.NextVar(current))
        if visits:
            total += matrix[previous][worker.end_node]
            routes.append(
                Route(
                    worker_id=worker.id,
                    departure_min=departure,
                    return_min=solution.Value(clock.CumulVar(current)),
                    visits=tuple(visits),
                    travel_min=total,
                )
            )
    return build_result(data, routes, policy="ortools", status=status, started=started)
