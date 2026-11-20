"""Immutable, PII-free planning input. All times are integer minutes from one epoch."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.planning.datasets import TravelEstimateParameters

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Minute = Annotated[int, Field(strict=True, ge=0, le=100000)]
Domain = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9:_-]{0,29}$")]
Policy = Literal["fifo", "urgency_fifo", "nearest", "ortools"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class WorkerSpec(Frozen):
    id: Identifier
    skills: tuple[Domain, ...]
    region: Identifier | None = None
    start_node: Minute
    end_node: Minute
    shift_start_min: Minute
    shift_end_min: Minute
    # Omitting the default keeps the input hash of stored plans stable.
    lunch_min: tuple[Minute, Minute] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def valid_worker(self):
        if (
            self.shift_end_min <= self.shift_start_min
            or not self.skills
            or len(set(self.skills)) != len(self.skills)
        ):
            raise ValueError("invalid_worker_window_or_skills")
        if self.lunch_min and not (
            self.shift_start_min < self.lunch_min[0] < self.lunch_min[1] < self.shift_end_min
        ):
            raise ValueError("invalid_lunch_window")
        return self


class TicketSpec(Frozen):
    id: Identifier
    node: Minute
    domain: Domain
    region: Identifier | None = None
    release_min: Minute
    deadline_min: Minute
    service_min: Annotated[int, Field(strict=True, ge=1, le=10000)]
    urgency: Annotated[int, Field(strict=True, ge=0, le=3)]


class TravelMatrix(Frozen):
    nodes: tuple[Identifier, ...]
    minutes: tuple[tuple[Minute, ...], ...]
    version: Identifier
    provenance: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    estimate_parameters: TravelEstimateParameters | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def square(self):
        size = len(self.nodes)
        if not size or size > 1000 or len(set(self.nodes)) != size or len(self.minutes) != size:
            raise ValueError("invalid_matrix_size_or_nodes")
        if any(len(row) != size or row[i] != 0 for i, row in enumerate(self.minutes)):
            raise ValueError("invalid_matrix_shape_or_diagonal")
        return self


class PlanningInput(Frozen):
    workers: tuple[WorkerSpec, ...]
    tickets: tuple[TicketSpec, ...]
    travel: TravelMatrix
    mode: Literal["domain", "region", "region_support"] = "domain"
    region_support_penalty: Minute = 15
    solver_seconds: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 2.0
    seed: Annotated[int, Field(strict=True, ge=0, le=2147483647)] = 0
    assumptions_version: Identifier
    data_kind: Literal["synthetic", "service"] = "synthetic"
    dataset_hash: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")] = None
    dataset_revision: Annotated[int | None, Field(strict=True, ge=1)] = None

    @model_validator(mode="after")
    def references(self):
        if self.data_kind == "service" and (
            self.dataset_hash is None or self.dataset_revision is None
        ):
            raise ValueError("service_dataset_generation_required")
        if self.data_kind == "synthetic" and (
            self.dataset_hash is not None or self.dataset_revision is not None
        ):
            raise ValueError("synthetic_dataset_generation_forbidden")
        if len(self.workers) > 50 or len(self.tickets) > 500:
            raise ValueError("planning_size_limit")
        if len({w.id for w in self.workers}) != len(self.workers) or len(
            {t.id for t in self.tickets}
        ) != len(self.tickets):
            raise ValueError("duplicate_input_id")
        size = len(self.travel.nodes)
        if any(node >= size for w in self.workers for node in (w.start_node, w.end_node)) or any(
            t.node >= size for t in self.tickets
        ):
            raise ValueError("unknown_matrix_node")
        if self.mode in {"region", "region_support"} and (
            any(w.region is None for w in self.workers)
            or any(t.region is None for t in self.tickets)
        ):
            raise ValueError("region_required")
        return self


class Visit(Frozen):
    ticket_id: Identifier
    arrival_min: Minute
    completion_min: Minute


class Route(Frozen):
    worker_id: Identifier
    departure_min: Minute
    return_min: Minute
    visits: tuple[Visit, ...]
    travel_min: Minute


class Unassigned(Frozen):
    ticket_id: Identifier
    reason: Literal[
        "no_eligible_worker",
        "outside_shift",
        "capacity_or_order",
        "solver_objective_or_search_limit",
        "solver_no_solution",
        "urgent_infeasible",
    ]
    urgent_escalation: bool


class Metrics(Frozen):
    travel_min: int
    service_min: int
    late_count: int
    late_minutes: int
    unassigned_count: int
    urgent_unassigned_count: int
    return_travel_min: int


class PlanResult(Frozen):
    policy: Policy
    solver_status: str
    underlying_solver_status: str | None = None
    optimality_proven: bool = False
    routes: tuple[Route, ...]
    unassigned: tuple[Unassigned, ...]
    metrics: Metrics
    input_hash: str
    engine_version: str = "planning-v1"
    assumptions_version: str
    matrix_version: str
    elapsed_ms: float
    solver_limit_seconds: float
    seed: int
    determinism_guaranteed: bool = False
    fallback_used: bool = False
    fallback_policy: Literal["urgency_fifo"] | None = None
