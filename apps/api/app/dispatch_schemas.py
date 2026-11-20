"""Dispatcher-only synthetic planning boundary; times are offsets from a UTC instant."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.planning.schemas import Domain, PlanningInput, PlanResult, Policy
from app.schemas import ExecutionView, Input, WorkerHours

Minute = Annotated[int, Field(strict=True, ge=0, le=1440)]
Short = Annotated[str, Field(min_length=1, max_length=150)]


class CatalogLocationCandidate(BaseModel):
    code: str
    label: str
    building_code: str
    floor: str | None
    space_code: str
    sources: list[str] = []


class CatalogLocationCandidates(BaseModel):
    status: Literal["unavailable", "no_match", "one", "multiple"]
    catalog_revision: int | None
    location_version: int
    candidates: list[CatalogLocationCandidate]
    qr_text_conflict: bool = False


class CatalogLocationConfirm(Input):
    catalog_revision: Annotated[int, Field(strict=True, ge=1)]
    expected_location_version: Annotated[int, Field(strict=True, ge=0)]
    code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9:_-]{0,29}$")]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason_required")
        return value


class ServiceLocationDataset(BaseModel):
    version: str
    sha256: str
    revision: int
    locations: list[dict[str, str]]


class ServiceLocationConfirm(Input):
    expected_location_version: Annotated[int, Field(strict=True, ge=0)]
    dataset_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    dataset_revision: Annotated[int, Field(strict=True, ge=1)]
    code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9:_-]{0,99}$")]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason_required")
        return value


class PlanningNode(BaseModel):
    code: str
    label: str
    region: str


class PlanningPreset(BaseModel):
    id: str
    label: str
    version: str
    provenance: str


class PlanningCapabilities(BaseModel):
    available: bool
    travel_data_status: Literal["available", "unavailable", "unknown"] = "unknown"
    deployment_mode: str
    data_kind: Literal["synthetic", "service"] = "synthetic"
    dataset_hash: str | None = None
    dataset_revision: int | None = None
    reasons: list[str]
    domains: list[str]
    regions: list[str]
    nodes: list[PlanningNode]
    presets: list[PlanningPreset]
    time_unit: str = "minute"
    time_basis: str = "UTC plan_start_at + offset minutes"
    max_offset_min: int = 1440


class ScheduleInput(Input):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    expected_profile_revision: Annotated[int | None, Field(strict=True, ge=0)] = None
    enabled: bool
    skills: Annotated[list[Domain], Field(min_length=1, max_length=1000)]
    region: Short
    dataset_hash: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")] = None
    dataset_revision: Annotated[int | None, Field(strict=True, ge=1)] = None
    days_off: Annotated[list[date], Field(max_length=366)] = Field(default_factory=list)
    shift_start_min: Minute
    shift_end_min: Minute
    hours: WorkerHours | None = None
    start_node: Short
    end_node: Short
    assumptions_source: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def window(self):
        if self.shift_end_min <= self.shift_start_min or len(set(self.skills)) != len(self.skills):
            raise ValueError("invalid_schedule")
        return self


class ScheduleView(BaseModel):
    worker_id: UUID
    display_name: str
    staff_code: str
    revision: int
    profile_revision: int = 0
    configured: bool
    data: ScheduleInput | None = None


class PlanningTicketInput(Input):
    ticket_id: UUID
    release_min: Minute
    deadline_min: Minute
    service_min: Annotated[int, Field(strict=True, ge=1, le=1440)]
    assumptions_source: Annotated[str, Field(min_length=1, max_length=500)]


class PlanCreate(Input):
    worker_ids: Annotated[list[UUID], Field(min_length=1, max_length=20)]
    tickets: Annotated[list[PlanningTicketInput], Field(min_length=1, max_length=50)]
    preset_id: Short
    dataset_hash: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")] = None
    dataset_revision: Annotated[int | None, Field(strict=True, ge=1)] = None
    plan_start_at: datetime
    mode: Literal["domain", "region", "region_support"] = "domain"
    policy: Policy = "ortools"
    assumptions_version: Short
    solver_seconds: Annotated[float, Field(gt=0, le=3, allow_inf_nan=False)] = 1.0

    @field_validator("plan_start_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("utc_instant_required")
        return value

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.worker_ids)) != len(self.worker_ids) or len(
            {t.ticket_id for t in self.tickets}
        ) != len(self.tickets):
            raise ValueError("duplicate_id")
        return self


class PlanningExclusion(BaseModel):
    ticket_id: UUID | None = None
    worker_id: UUID | None = None
    reason: str
    urgent_escalation: bool = False


class InsertionTicketInput(PlanningTicketInput):
    release_min: Annotated[int, Field(strict=True, ge=-100000, le=100000)]
    deadline_min: Annotated[int, Field(strict=True, ge=-100000, le=100000)]


class InsertionCreate(Input):
    expected_parent_revision: Annotated[int, Field(strict=True, ge=1)]
    urgent_tickets: Annotated[list[InsertionTicketInput], Field(min_length=1, max_length=20)]
    policy: Policy = "ortools"
    solver_seconds: Annotated[float, Field(gt=0, le=3, allow_inf_nan=False)] = 1.0
    assumptions_version: Short


class DispatchPlanView(BaseModel):
    id: UUID
    revision: int
    status: str
    input_hash: str
    input: PlanningInput
    assumptions: PlanCreate | InsertionCreate
    parent_plan_id: UUID | None
    kind: str
    insertion: dict
    result: PlanResult
    excluded: list[PlanningExclusion]
    plan_start_at: datetime
    expires_at: datetime
    created_at: datetime
    captured_at: datetime
    approved_at: datetime | None


class PlanApprove(Input):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    input_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class PlannedAssignmentView(BaseModel):
    assignment_id: UUID
    ticket_id: UUID
    worker_id: UUID
    credential_revision: int
    revoked: bool
    membership_kind: str = "new"
    original_assignment_id: UUID | None = None
    execution: ExecutionView


class PlanApprovalView(BaseModel):
    plan: DispatchPlanView
    assignments: list[PlannedAssignmentView]


class CredentialInput(Input):
    request_id: UUID
    expected_credential_revision: Annotated[int, Field(strict=True, ge=0)]


class CredentialView(BaseModel):
    assignment_id: UUID
    credential_revision: int
    status: Literal["issued", "already_issued"]
    worker_token: str | None = None
