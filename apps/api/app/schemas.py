from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExecutionReportRef(BaseModel):
    id: UUID
    revision: int
    result: str


class RemainingView(BaseModel):
    minutes: int
    confirmed_at: datetime
    expected_end_at: datetime
    expires_at: datetime
    reason: str
    revision: int


class ExecutionView(BaseModel):
    active_plan_id: UUID | None = None
    active_plan_revision: int | None = None
    phase: Literal["queued", "moving", "working", "visit_ended", "stopped"]
    revision: int
    departed_at: datetime | None
    started_at: datetime | None
    visit_ended_at: datetime | None
    legacy_state_inferred: bool
    can_depart: bool
    can_start: bool
    can_report: bool
    can_stop_visit: bool
    can_end_visit: bool
    blocked_reason: str | None
    remaining: RemainingView | None = None
    latest_approved_report: ExecutionReportRef | None = None
    correction_requested: bool = False


class ExecutionInput(Input):
    expected_execution_revision: int | None = Field(default=None, ge=0)


class DepartInput(Input):
    expected_execution_revision: int = Field(ge=0)


class RemainingInput(DepartInput):
    remaining_min: int = Field(ge=1, le=480, strict=True)
    reason: str = Field(min_length=1, max_length=1000)


class StopVisitInput(DepartInput):
    expected_active_plan_id: UUID
    expected_plan_revision: int = Field(ge=1)
    stop_current_visit: Literal[True]
    reason: str = Field(min_length=1, max_length=1000)


class EndVisitInput(DepartInput):
    report_id: UUID
    expected_report_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class DemoLogin(Input):
    persona: Literal[
        "reporter_a", "reporter_b", "dispatcher", "worker_a", "worker_b", "worker_c", "worker_d",
        "worker_e", "worker_f", "worker_g"
    ]


class DemoWorkers(BaseModel):
    personas: list[Literal["worker_c", "worker_d", "worker_e", "worker_f", "worker_g"]]


class PrototypeLogin(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class UserView(BaseModel):
    id: UUID
    role: str
    display_name: str


class SessionView(BaseModel):
    user: UserView
    csrf_token: str
    auth_mode: Literal["demo", "email_otp", "prototype_admin", "public_session"] = "demo"
    data_scope: Literal["demo", "service", "prototype", "public_demo"] = "demo"


class PublicGuestStart(Input):
    notice_confirmed: Literal[True]


class PublicInviteExchange(Input):
    token: str = Field(min_length=40, max_length=200)


class OtpRequest(Input):
    email: str = Field(min_length=3, max_length=254)


class OtpVerify(OtpRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")


MailDeliveryStatus = Literal[
    "not_sent",
    "queued",
    "claimed",
    "sending",
    "retry_wait",
    "local_saved",
    "accepted",
    "unknown",
    "failed",
    "expired",
    "superseded",
]


class OtpAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    delivery: Literal["local", "smtp", "gmail", "disabled"] = "local"
    delivery_status: Literal["queued"] = "queued"
    expires_in_seconds: Literal[300] = 300


class PublicLimitPolicy(BaseModel):
    ticket_hourly: int
    active_sessions: int
    ip_hourly: int
    ticket_storage: int


class RuntimeView(BaseModel):
    photo_ai_capability: Literal["manual", "available", "restricted"] = "manual"
    public_limits: PublicLimitPolicy | None = None
    local_staff_auth: bool = False
    staff_password_auth: bool = False
    mode: Literal["demo", "service", "prototype", "public_demo"]
    auth_method: Literal["demo", "email_otp", "prototype_admin", "public_session"]
    llm_mode: Literal["replay", "live", "unavailable"]
    mail_delivery: Literal["local", "smtp", "gmail", "disabled"]


class PrivacyView(BaseModel):
    status: Literal["pending", "processed", "approved", "failed", "revoked", "deleted"]
    variant_id: UUID | None = None
    sha256: str | None = None
    pipeline_version: str | None = None
    detector_version: str | None = None
    preview_url: str | None = None
    face_count: int | None = None
    provider: str | None = None
    purpose: str | None = None
    approved_at: datetime | None = None
    delete_after: datetime | None = None


class PhotoLocationCandidate(BaseModel):
    code: str
    label: str
    latitude: float | None = None
    longitude: float | None = None


class PhotoView(BaseModel):
    id: UUID
    url: str
    location_candidates: list[str] = Field(default_factory=list)
    location_candidate_details: list[PhotoLocationCandidate] = Field(default_factory=list)
    privacy: PrivacyView = Field(default_factory=lambda: PrivacyView(status="pending"))


class PhotoCoordinatesView(BaseModel):
    id: UUID
    latitude: float
    longitude: float
    delete_after: datetime


class TicketPhotoCoordinatesView(BaseModel):
    photos: list[PhotoCoordinatesView]


class EventView(BaseModel):
    id: UUID
    type: str
    at: datetime
    label: str
    actor_kind: str | None = None
    actor_label: str | None = None
    reason: str
    from_status: str | None
    to_status: str | None


class ReportStructure(Input):
    actions: list[Annotated[str, StringConstraints(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=5
    )
    materials_used: list[Annotated[str, StringConstraints(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=5
    )
    residual_issue: str = Field(default="", max_length=500)
    revisit_needed: bool = Field(default=False, strict=True)
    photo_comparison: Literal["not_performed"] = "not_performed"
    source: Literal["replay", "live", "manual", "legacy"] = "manual"


class ReportView(BaseModel):
    created_at: datetime | None = None
    supersedes_report_id: UUID | None = None
    correction_requested_at: datetime | None = None
    correction_reason: str | None = None
    revision: int = 1
    analysis_status: str = "worker_confirmed"
    draft: ReportStructure | None = None
    confirmed: ReportStructure | None = None
    confirmed_result: Literal["completed", "temporary", "revisit", "needs_review"] | None = None
    worker_confirmed_revision: int | None = None
    warnings: list[str] = Field(default_factory=list)
    reply_text: str | None = None
    reply_preview: str | None = None
    publication_status: Literal["not_published", "screen_published"] = "not_published"
    delivery_status: MailDeliveryStatus = "not_sent"
    legacy_migrated: bool = False
    id: UUID
    result: Literal["completed", "temporary", "revisit", "needs_review"]
    text: str
    reviewed: bool
    photos: list[PhotoView]


class RuleHit(BaseModel):
    hit_id: str | None = None
    rule_id: str
    start: int
    end: int
    suggested_urgency: int = Field(ge=0, le=3)
    context: Literal["current", "negated", "past", "ambiguous"]


class SafetyView(BaseModel):
    own_automatic_urgency: int | None = None
    own_effective_urgency: int | None = None
    linked_rule_floor: int = Field(default=3, ge=0, le=3)
    linked_requires_review: bool = False
    source_text: str | None = None
    original_text_hash: str | None = None
    source_text_hash: str | None = None
    rules_version: str | None = None
    rule_hits: list[RuleHit] = Field(default_factory=list)
    rule_disagreement: bool | None = None
    rule_guard_kept: bool | None = None
    rule_floor: int = 3
    ai_urgency: int | None = None
    automatic_urgency: int = 3
    effective_urgency: int = 3
    assessment_status: str = "pending"
    requires_human_review: bool = True
    assessment_version: int = 0
    override_reason: str | None = None
    emergency_banner: bool = False


class GPS(Input):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy: float = Field(ge=0, le=100000, allow_inf_nan=False)
    timestamp_ms: float | None = Field(default=None, ge=0, le=1e15, allow_inf_nan=False)

    @field_validator("latitude", "longitude", "accuracy", "timestamp_ms", mode="before")
    @classmethod
    def reject_bool_or_strings(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("numeric_value_required")
        return value


class LocationResolve(Input):
    text: str = Field(default="", max_length=4000)
    location_text: str = Field(default="", max_length=300)
    loc_hint: str | None = Field(default=None, max_length=100)

    @field_validator("loc_hint", mode="before")
    @classmethod
    def bounded_hint(cls, value):
        return (
            "!" if value is not None and (not isinstance(value, str) or len(value) > 100) else value
        )

    gps: GPS | None = None


class LocationCandidate(BaseModel):
    latitude: float | None = None
    longitude: float | None = None
    code: str
    label: str
    building_code: str
    floor: str | None = None
    space_code: str | None = None
    sources: list[str]


class GPSPointDistance(BaseModel):
    code: str
    label: str
    distance_m: int


class GPSComparison(BaseModel):
    quality: Literal["absent", "missing_time", "stale", "future", "inaccurate", "ready"]
    availability: Literal["no_catalog", "no_match", "no_coordinates", "available"]
    catalog_revision: int | None = None
    accuracy_m: float | None = None
    points: list[GPSPointDistance] = Field(default_factory=list)


class LocationView(BaseModel):
    state: Literal["pending", "suggested", "needs_confirmation", "confirmed", "staff_review"] = (
        "pending"
    )
    version: int = 0
    clarification_count: int = 0
    can_answer: bool = True
    can_correct_text: bool = False
    selected: LocationCandidate | None = None
    candidates: list[LocationCandidate] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    question: str | None = None
    dictionary_version: str = "synthetic-locations-v1"
    source_type: Literal["synthetic", "unregistered", "service", "catalog"] = "synthetic"
    dataset_hash: str | None = None
    dataset_revision: int | None = None
    catalog_revision: int | None = None
    gps_comparison: GPSComparison | None = None


class ReviewDepartment(BaseModel):
    code: str
    label: str


class RoutingView(BaseModel):
    department_code: str = "REVIEW"
    department_label: str = "담당자 검토"
    co_review: list[ReviewDepartment] = Field(default_factory=list)
    status: Literal["suggested"] = "suggested"
    delivery_status: MailDeliveryStatus = "not_sent"
    rules_version: str = "routing-synthetic-v1"
    reason: str = "기존 티켓 · 위치와 부서 확인 필요"


class LocationUpdate(Input):
    action: Literal["confirm", "answer", "defer"]
    expected_version: int = Field(ge=0)
    candidate_code: str | None = Field(default=None, max_length=100)
    answer: str | None = Field(default=None, min_length=1, max_length=300)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def action_fields(self):
        if self.action == "confirm" and (not self.candidate_code or self.answer is not None):
            raise ValueError("confirm requires only candidate_code")
        if self.action == "answer" and (not self.answer or self.candidate_code is not None):
            raise ValueError("answer requires only answer text")
        if self.action == "defer" and (self.answer is not None or self.candidate_code is not None):
            raise ValueError("defer accepts no candidate or answer")
        return self


class ProcessingView(BaseModel):
    state: Literal["queued", "running", "retry_wait", "succeeded", "failed", "superseded"]
    attempt_count: int
    max_attempts: Literal[3] = 3
    error_code: str | None = None
    next_attempt_at: datetime | None = None


MergeVersion = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class MergedProgress(BaseModel):
    revision: int = Field(ge=1)
    subscribed: bool
    target_status: (
        Literal[
            "received",
            "confirmed",
            "assigned",
            "in_progress",
            "completed",
            "transferred",
            "cancelled",
            "reopened",
            "rejected",
        ]
        | None
    ) = None
    message: str | None = None
    delivery_status: MailDeliveryStatus = "not_sent"


class MergeHistoryView(BaseModel):
    linked_floor: int = Field(default=3, ge=0, le=3)
    source_automatic_urgency: int | None = None
    source_effective_urgency: int | None = None
    source_rules_version: str | None = None
    id: UUID
    source_id: UUID
    target_id: UUID
    source_previous_status: str
    active: bool
    revision: int
    subscribed: bool
    reassignment_required: bool = False
    created_at: datetime
    cancelled_at: datetime | None = None


class DuplicateCandidate(BaseModel):
    target_id: UUID
    public_no: str
    status: str
    category_code: str
    location_label: str
    automatic_urgency: int = Field(ge=0, le=3)
    effective_urgency: int = Field(ge=0, le=3)
    merge_version: MergeVersion
    evidence: Literal["same_space_24h", "same_space", "same_floor"]
    has_active_assignment: bool
    has_pending_report: bool


class DuplicateCandidates(BaseModel):
    source_version: MergeVersion
    candidates: list[DuplicateCandidate]
    embedding_status: Literal["not_performed"] = "not_performed"


class MergeInput(Input):
    target_id: UUID
    expected_source_version: MergeVersion
    expected_target_version: MergeVersion
    stop_source_work: bool = Field(default=False, strict=True)
    reason: str = Field(min_length=1, max_length=1000)


class DuplicateDismissInput(Input):
    target_id: UUID
    expected_source_version: MergeVersion
    expected_target_version: MergeVersion
    reason: str = Field(min_length=1, max_length=1000)


class UnmergeInput(Input):
    expected_source_version: MergeVersion
    expected_target_version: MergeVersion
    reason: str = Field(min_length=1, max_length=1000)


class MergeSubscriptionInput(Input):
    expected_merge_revision: int = Field(ge=1)
    subscribed: bool = Field(strict=True)


class LinkedSafetyReviewInput(Input):
    expected_merge_version: MergeVersion
    reason: str = Field(min_length=1, max_length=1000)


class AIDraftView(BaseModel):
    """Dispatcher-only AI draft. A suggestion for human review, never a decision."""

    mode: Literal["live"]
    category_code: str
    category_name: str | None = None
    ai_urgency: int = Field(ge=0, le=3)
    summary: str
    urgency_reason: str
    model: str | None = None
    prompt_version: str
    analysis_revision: int | None = None
    # The private input fingerprint itself is never returned.
    stale: bool = False
    late: bool = False
    more_urgent: bool = False
    reused: bool = False
    image_count: int | None = Field(default=None, ge=0, le=3)
    image_skip_reason: Literal[
        "not_available", "vision_unconfigured", "preparation_failed", "followup_text_only"
    ] | None = None


class ReporterIdentity(BaseModel):
    id: UUID
    email: str | None


class AIFollowupQuestion(BaseModel):
    id: Literal["q1", "q2"]
    kind: Literal["since_when", "still_ongoing", "scope", "access", "hazard_detail", "other"]
    question: str
    options: list[str]


class AIFollowupAnswer(BaseModel):
    question_id: Literal["q1", "q2"]
    answer: str


class AIFollowupView(BaseModel):
    source: Literal["live", "replay"]
    reused: bool | None = None
    status: Literal["pending", "answered", "skipped"]
    questions: list[AIFollowupQuestion]
    answers: list[AIFollowupAnswer]


class TicketView(BaseModel):
    ai_followup: AIFollowupView | None = None
    information_mail_allowed: bool = False
    information_status: Literal["waiting", "answered"] | None = None
    reporter_identity: ReporterIdentity | None = None
    catalog_revision: int | None = None
    merge_version: str = ""
    merged_progress: MergedProgress | None = None
    merge_history: list[MergeHistoryView] = Field(default_factory=list)
    analysis_revision: int = 0
    processing: ProcessingView | None = None
    location: LocationView = Field(default_factory=LocationView)
    routing: RoutingView = Field(default_factory=RoutingView)
    safety: SafetyView = Field(default_factory=SafetyView)
    id: UUID
    public_no: str
    status: str
    text: str
    location_text: str
    category_code: str
    category_label: str
    domain: str
    department_code: str
    ai_mode: Literal["replay", "live"] = "replay"
    ai_reused: bool | None = None
    ai_status: str
    ai_draft: AIDraftView | None = None
    needs_human_review: bool = True
    photos: list[PhotoView]
    timeline: list[EventView]
    assignment_id: UUID | None
    worker_name: str | None
    visit_followup: Literal["end_visit", "reassign"] | None = None
    orphaned_reports: list[ReportView] = Field(default_factory=list)
    approved_reports: list[ReportView] = Field(default_factory=list)
    latest_report: ReportView | None
    reply_text: str | None
    created_at: datetime
    updated_at: datetime


class TicketList(BaseModel):
    items: list[TicketView]


class CategoryView(BaseModel):
    code: str
    label: str
    domain: str
    department_code: str


class WorkerView(BaseModel):
    id: UUID
    display_name: str
    staff_code: str
    is_local_test_account: bool = Field(default_factory=bool)


class CatalogView(BaseModel):
    catalog_revision: int | None = None
    categories: list[CategoryView]
    workers: list[WorkerView]


class TicketCreate(Input):
    information_mail_allowed: bool | None = Field(default=None, strict=True)
    loc_hint: str | None = Field(default=None, max_length=100)

    @field_validator("loc_hint", mode="before")
    @classmethod
    def bounded_hint(cls, value):
        return (
            "!" if value is not None and (not isinstance(value, str) or len(value) > 100) else value
        )

    gps: GPS | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=4000
    )
    location_text: str = Field(default="", max_length=300)
    upload_ids: list[UUID] = Field(default_factory=list, max_length=3)

    @field_validator("upload_ids")
    @classmethod
    def unique_uploads(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Upload IDs must be unique")
        return value


class Reason(Input):
    reason: str = Field(min_length=1, max_length=1000)


class TicketPatch(Reason):
    expected_location_version: int = Field(ge=0)
    expected_category_code: str | None = Field(default=None, min_length=1, max_length=30)
    expected_catalog_revision: int | None = Field(default=None, ge=0)
    category_code: str | None = Field(default=None, min_length=1, max_length=30)
    location_text: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def require_change(self):
        if self.category_code is None and self.location_text is None:
            raise ValueError("category_or_location_required")
        return self


class AtomicLocationCandidate(Input):
    kind: Literal["candidate"]
    code: str = Field(min_length=1, max_length=100)


class AtomicLocationText(Input):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=300)


class AtomicDatasetLocation(Input):
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9:_-]{0,99}$")
    dataset_revision: int = Field(strict=True, ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConfirmAssignInput(Reason):
    worker_id: UUID
    expected_ticket_version: str = Field(min_length=64, max_length=64)
    expected_catalog_revision: int | None = Field(ge=0)
    category_code: str | None = Field(default=None, min_length=1, max_length=30)
    location_choice: (
        Annotated[AtomicLocationCandidate | AtomicLocationText, Field(discriminator="kind")] | None
    ) = None
    dataset_location: AtomicDatasetLocation | None = None

    def model_dump(self, *args, **kwargs):
        result = super().model_dump(*args, **kwargs)
        for field in ("category_code", "location_choice", "dataset_location"):
            if result.get(field) is None:
                result.pop(field, None)
        return result


class ConfirmAssignView(BaseModel):
    ticket_id: UUID
    assignment_id: UUID


class AssignmentWorkerCandidate(BaseModel):
    worker_id: UUID
    display_name: str
    staff_code: str
    is_local_test_account: bool = Field(default_factory=bool)
    eligible: bool
    reasons: list[str]
    open_assignments: int
    has_ongoing_visit: bool = False
    same_building: bool
    same_region: bool
    estimated_travel_min: int | None = None
    rank: int | None
    field_code: str | None


class AssignmentWorkerCandidates(BaseModel):
    context_reason: str | None
    candidates: list[AssignmentWorkerCandidate]


class AssignInput(Reason):
    stop_current_visit: bool = False
    expected_assignment_id: UUID | None = None
    expected_execution_revision: int | None = Field(default=None, ge=0)
    worker_id: UUID
    override_reason: Annotated[str | None, Field(max_length=1000)] = None


class AssignView(BaseModel):
    ticket: TicketView
    worker_token: str | None
    assignment_id: UUID


class WorkerAssignmentView(BaseModel):
    execution: ExecutionView
    assignment_id: UUID
    ticket: TicketView


class WorkerSafetySummary(BaseModel):
    own_automatic_urgency: int | None = None
    own_effective_urgency: int | None = None
    linked_rule_floor: int = 3
    linked_requires_review: bool = False
    rule_floor: int = 3
    ai_urgency: int | None = None
    automatic_urgency: int = 3
    effective_urgency: int = 3
    override_applied: bool = False


class WorkerAssignmentSummary(BaseModel):
    assignment_id: UUID
    assigned_at: datetime
    ticket_id: UUID
    public_no: str
    status: str
    location_text: str
    confirmed_location_label: str | None = None
    safety: WorkerSafetySummary
    execution: ExecutionView
    map_latitude: float | None = None
    map_longitude: float | None = None
    plan_group: str | None = None
    plan_sequence: int | None = None


class WorkerAssignmentList(BaseModel):
    items: list[WorkerAssignmentSummary]
    total: int


ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


class WorkerHours(Input):
    shift_start: ClockTime
    shift_end: ClockTime
    lunch_start: ClockTime | None = None
    lunch_end: ClockTime | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.shift_start >= self.shift_end:
            raise ValueError("invalid_worker_hours")
        if (self.lunch_start is None) != (self.lunch_end is None):
            raise ValueError("lunch_start_and_end_required")
        if self.lunch_start is not None and self.lunch_end is not None and not (
            self.shift_start < self.lunch_start < self.lunch_end < self.shift_end
        ):
            raise ValueError("lunch_must_be_inside_shift")
        return self


class WorkerOwnHoursInput(Input):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    hours: WorkerHours


class WorkerOwnSettings(BaseModel):
    field_codes: list[str]
    department_code: str | None
    schedule_configured: bool
    schedule_enabled: bool | None
    schedule_revision: int
    hours: WorkerHours | None
    shift_start_min: int | None
    shift_end_min: int | None
    days_off: list[str]
    profile_stale: bool
    schedule_stale: bool


class ReportInput(ExecutionInput):
    supersedes_report_id: UUID | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=4000
    )
    upload_ids: list[UUID] = Field(default_factory=list, max_length=3)
    result: Literal["completed", "temporary", "revisit", "needs_review"]


class CorrectionInput(Reason):
    expected_report_revision: int = Field(ge=1)


class ReviewInput(Reason):
    report_id: UUID
    expected_report_revision: int = Field(ge=1)
    review_outcome: Literal["revisit"] | None = None


class ReportDismissInput(Reason):
    expected_report_revision: int = Field(ge=1)


class ReportConfirmInput(Reason):
    expected_revision: int = Field(ge=1)
    confirmed_result: Literal["completed", "temporary", "revisit", "needs_review"]
    structure: ReportStructure


class Mask(Input):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def contained(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Mask must fit within image")
        return self


class ProcessInput(Input):
    masks: list[Mask] = Field(default_factory=list, max_length=100)
    detect_faces: bool = False


class ApprovalInput(Input):
    variant_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pipeline_version: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    purpose: Literal["ticket_analysis", "completion_analysis"]


class UrgencyInput(Reason):
    effective_urgency: int = Field(ge=0, le=3, strict=True)
    expected_assessment_version: int = Field(ge=1)
