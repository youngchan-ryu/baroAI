import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IdentityTimestamps:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(IdentityTimestamps, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('reporter','dispatcher','worker','admin','system')", name="user_role"
        ),
    )
    auth_epoch: Mapped[int] = mapped_column(default=0)
    verified_email: Mapped[str | None] = mapped_column(String(254), unique=True)
    persona: Mapped[str | None] = mapped_column(String(40), unique=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)


class Ticket(IdentityTimestamps, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','needs_info','received','confirmed','assigned','in_progress',"
            "'completed','transferred','merged','rejected','cancelled','reopened')",
            name="ticket_status",
        ),
    )
    linked_safety_floor: Mapped[int] = mapped_column(default=3)
    linked_safety_requires_review: Mapped[bool] = mapped_column(default=False)
    analysis_revision: Mapped[int] = mapped_column(default=0)
    location_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    routing_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    location_version: Mapped[int] = mapped_column(default=0)
    location_answer_count: Mapped[int] = mapped_column(default=0)
    location_history: Mapped[list] = mapped_column(JSONB, default=list)
    information_safety_evidence: Mapped[list] = mapped_column(JSONB, default=list)
    followup_analysis_evidence: Mapped[list] = mapped_column(JSONB, default=list)
    ai_followup_data: Mapped[dict | None] = mapped_column(JSONB)
    information_mail_allowed: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    safety_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    safety_version: Mapped[int] = mapped_column(default=0)
    effective_urgency: Mapped[int] = mapped_column(default=3)
    override_reason: Mapped[str | None] = mapped_column(Text)
    public_no: Mapped[str | None] = mapped_column(String(30), unique=True)
    location_text: Mapped[str] = mapped_column(Text, default="")
    category_code: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    catalog_revision: Mapped[int | None] = mapped_column(BigInteger)
    catalog_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    ai_status: Mapped[str] = mapped_column(String(20), default="replayed")
    reply_text: Mapped[str | None] = mapped_column(Text)
    reporter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="created", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class InformationRequest(IdentityTimestamps, Base):
    __tablename__ = "information_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','answered','cancelled')", name="information_request_status"
        ),
        Index(
            "information_request_one_open",
            "ticket_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    question: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="open")
    answer: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mail_intent_failed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class TicketEvent(IdentityTimestamps, Base):
    __tablename__ = "ticket_events"
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))


class AuthSession(IdentityTimestamps, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)


class PublicIntakeControl(Base):
    __tablename__ = "public_intake_control"
    __table_args__ = (CheckConstraint("id = 1", name="public_intake_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    paused: Mapped[bool] = mapped_column(default=False, nullable=False)


class PublicLimitEvent(IdentityTimestamps, Base):
    __tablename__ = "public_limit_events"
    __table_args__ = (
        CheckConstraint("kind IN ('guest','ticket')", name="public_limit_kind"),
        Index("ix_public_limit_event_lookup", "kind", "subject", "created_at"),
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    subject: Mapped[str] = mapped_column(String(64), nullable=False)


class PublicStaffInvite(IdentityTimestamps, Base):
    __tablename__ = "public_staff_invites"
    __table_args__ = (
        CheckConstraint("role IN ('dispatcher','worker')", name="public_staff_invite_role"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id"), unique=True
    )


class Assignment(IdentityTimestamps, Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("id", "worker_id", name="assignment_id_worker_unique"),)
    catalog_revision: Mapped[int | None] = mapped_column(BigInteger)
    catalog_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    active_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dispatch_plans.id"))
    predecessor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assignments.id"))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    stop_reason: Mapped[str | None] = mapped_column(Text)
    execution_revision: Mapped[int] = mapped_column(default=0)
    departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visit_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_location_code: Mapped[str | None] = mapped_column(String(100))
    remaining_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    credential_revision: Mapped[int] = mapped_column(default=0)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dispatch_plans.id"))
    plan_sequence: Mapped[int | None] = mapped_column()
    planned_arrival_min: Mapped[int | None] = mapped_column()
    planned_completion_min: Mapped[int | None] = mapped_column()
    auth_epoch: Mapped[int] = mapped_column(default=0)
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), index=True)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)


class CompletionReport(IdentityTimestamps, Base):
    __tablename__ = "completion_reports"
    __table_args__ = (
        CheckConstraint(
            "result IN ('completed','temporary','revisit','needs_review')", name="report_result"
        ),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assignments.id"))
    supersedes_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("completion_reports.id"), unique=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_reason: Mapped[str | None] = mapped_column(Text)
    correction_requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    result: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(default=1)
    analysis_status: Mapped[str] = mapped_column(String(30), default="queued")
    draft_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    confirmed_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    worker_confirmed_revision: Mapped[int | None] = mapped_column()
    confirmed_result: Mapped[str | None] = mapped_column(String(20))
    approved_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    reply_text: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_migrated: Mapped[bool] = mapped_column(default=False)
    reviewed: Mapped[bool] = mapped_column(default=False)


class Upload(IdentityTimestamps, Base):
    __tablename__ = "uploads"
    __table_args__ = (
        Index(
            "ix_uploads_photo_coordinates_expiry",
            "delete_after",
            postgresql_where=text("photo_coordinates IS NOT NULL"),
        ),
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assignments.id"))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tickets.id"), index=True)
    report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("completion_reports.id"))
    information_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("information_requests.id")
    )
    purpose: Mapped[str] = mapped_column(String(20), default="ticket")
    delete_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now() + text("interval '30 days'")
    )
    deleted: Mapped[bool] = mapped_column(default=False)
    last_successful_variant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    current_variant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    object_key: Mapped[str] = mapped_column(String(100), unique=True)
    location_signal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    photo_coordinates: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    content_type: Mapped[str] = mapped_column(String(30), default="image/jpeg")
    size: Mapped[int] = mapped_column()


class IdempotencyRecord(IdentityTimestamps, Base):
    __tablename__ = "idempotency_records"
    scope: Mapped[str] = mapped_column(String(180), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)


class ImageVariant(IdentityTimestamps, Base):
    __tablename__ = "image_variants"
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id"), index=True)
    object_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    sha256: Mapped[str | None] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(100))
    detector_version: Mapped[str] = mapped_column(String(150))
    face_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20))
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))


class ImageApproval(IdentityTimestamps, Base):
    __tablename__ = "image_approvals"
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("image_variants.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(100))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(50))
    purpose: Mapped[str] = mapped_column(String(50))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessingJob(IdentityTimestamps, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index(
            "processing_ticket_revision",
            "ticket_id",
            "kind",
            "input_revision",
            unique=True,
            postgresql_where=text("report_id IS NULL"),
        ),
        Index(
            "processing_report_revision",
            "report_id",
            "kind",
            "input_revision",
            unique=True,
            postgresql_where=text("report_id IS NOT NULL"),
        ),
        CheckConstraint(
            "state IN ('queued','running','retry_wait','succeeded','failed','superseded')",
            name="processing_job_state",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="processing_job_attempt_count"),
        Index("ix_processing_jobs_due", "state", "next_attempt_at", "lease_expires_at"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("completion_reports.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), default="ticket_analysis")
    input_revision: Mapped[int] = mapped_column()
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempt_count: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))


class ProcessingAttempt(IdentityTimestamps, Base):
    __tablename__ = "processing_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt_no", name="processing_attempt_number"),)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), index=True
    )
    attempt_no: Mapped[int] = mapped_column()
    mode: Mapped[str] = mapped_column(String(20), default="replay")
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column()
    error_code: Mapped[str | None] = mapped_column(String(50))
    applied: Mapped[bool] = mapped_column(default=False)


class TicketMerge(IdentityTimestamps, Base):
    __tablename__ = "ticket_merges"
    __table_args__ = (
        CheckConstraint("source_id <> target_id", name="ticket_merge_distinct"),
        CheckConstraint("revision >= 1", name="ticket_merge_revision"),
        CheckConstraint(
            "previous_status IN ('received','confirmed','assigned','in_progress')",
            name="ticket_merge_previous_status",
        ),
        Index(
            "ticket_merge_active_source",
            "source_id",
            unique=True,
            postgresql_where=text("active = true"),
        ),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    previous_status: Mapped[str] = mapped_column(String(20))
    approved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    source_version: Mapped[str] = mapped_column(String(64))
    target_version: Mapped[str] = mapped_column(String(64))
    safety_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(default=1)
    active: Mapped[bool] = mapped_column(default=True)
    subscribed: Mapped[bool] = mapped_column(default=False)
    subscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)


class DuplicateDismissal(IdentityTimestamps, Base):
    __tablename__ = "duplicate_dismissals"
    __table_args__ = (
        CheckConstraint("source_id <> target_id", name="duplicate_dismissal_distinct"),
        UniqueConstraint(
            "source_id",
            "target_id",
            "source_version",
            "target_version",
            name="duplicate_dismissal_versions",
        ),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    source_version: Mapped[str] = mapped_column(String(64))
    target_version: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)


class DeploymentIdentity(Base):
    __tablename__ = "deployment_identity"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))
    namespace: Mapped[str] = mapped_column(String(60))


class PlanningDatasetActive(Base):
    __tablename__ = "planning_dataset_active"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="planning_dataset_active_revision"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="planning_dataset_active_sha256"),
        ForeignKeyConstraint(
            ["namespace", "revision", "sha256", "dataset_id", "version"],
            [
                "planning_dataset_activations.namespace",
                "planning_dataset_activations.revision",
                "planning_dataset_activations.sha256",
                "planning_dataset_activations.dataset_id",
                "planning_dataset_activations.version",
            ],
            name="planning_dataset_active_activation_fk",
        ),
    )
    namespace: Mapped[str] = mapped_column(String(60), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(100))


class PlanningDatasetActivation(IdentityTimestamps, Base):
    __tablename__ = "planning_dataset_activations"
    __table_args__ = (
        UniqueConstraint("namespace", "revision", name="planning_dataset_activation_unique"),
        UniqueConstraint(
            "namespace",
            "revision",
            "sha256",
            "dataset_id",
            "version",
            name="planning_dataset_activation_identity",
        ),
        CheckConstraint("revision >= 1", name="planning_dataset_activation_revision"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="planning_dataset_activation_sha256"),
    )
    namespace: Mapped[str] = mapped_column(String(60))
    revision: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    operator_uid: Mapped[int] = mapped_column(BigInteger)


class StorageIdentity(Base):
    __tablename__ = "storage_identity"
    __table_args__ = (
        CheckConstraint("id = 1", name="storage_identity_singleton"),
        CheckConstraint("state IN ('pending','active')", name="storage_identity_state"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    storage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    mode: Mapped[str] = mapped_column(String(20))
    environment: Mapped[str] = mapped_column(String(20))
    namespace: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(10))
    device: Mapped[int] = mapped_column(BigInteger)
    inode: Mapped[int] = mapped_column(BigInteger)


class StorageOperation(IdentityTimestamps, Base):
    __tablename__ = "storage_operations"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="storage_operation_scope_key"),
        CheckConstraint("kind IN ('upload','reprocess')", name="storage_operation_kind"),
        CheckConstraint(
            "state IN ('active','published','abandoned')", name="storage_operation_state"
        ),
        CheckConstraint("purpose IN ('ticket','report')", name="storage_operation_purpose"),
        CheckConstraint(
            "(state = 'published' AND published_upload_id IS NOT NULL) OR "
            "(state != 'published' AND published_upload_id IS NULL)",
            name="storage_operation_publication",
        ),
    )
    storage_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_identity.storage_id", deferrable=True, initially="IMMEDIATE")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    actor_role: Mapped[str] = mapped_column(String(20))
    auth_epoch: Mapped[int] = mapped_column()
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assignments.id"))
    upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id"))
    published_upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id"))
    current_variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("image_variants.id"))
    previous_variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("image_variants.id"))
    last_variant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("image_variants.id"))
    previous_sha256: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))
    purpose: Mapped[str] = mapped_column(String(10))
    scope: Mapped[str] = mapped_column(String(160))
    key_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    mask_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(10))
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_data: Mapped[dict | None] = mapped_column(JSONB)


class StorageObject(IdentityTimestamps, Base):
    __tablename__ = "storage_objects"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','ready','deleting','deleted')", name="storage_object_state"
        ),
        CheckConstraint("kind IN ('original','variant')", name="storage_object_kind"),
        CheckConstraint("size >= 0", name="storage_object_size"),
        CheckConstraint(
            "(stage_device IS NULL AND stage_inode IS NULL) OR "
            "(stage_device IS NOT NULL AND stage_inode IS NOT NULL AND stage_inode > 0)",
            name="storage_object_stage_identity",
        ),
    )
    storage_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_identity.storage_id", deferrable=True, initially="IMMEDIATE"),
        index=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage_operations.id"))
    object_key: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(10))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column()
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage_device: Mapped[int | None] = mapped_column(BigInteger)
    stage_inode: Mapped[int | None] = mapped_column(BigInteger)


class EmailChallenge(Base):
    __tablename__ = "email_challenges"
    address_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    consumed: Mapped[bool] = mapped_column(default=False)


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(default=0)


class AuthRoleChange(IdentityTimestamps, Base):
    __tablename__ = "auth_role_changes"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    from_role: Mapped[str] = mapped_column(String(20))
    to_role: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    operator_uid: Mapped[int] = mapped_column()


class StaffNameChange(IdentityTimestamps, Base):
    __tablename__ = "staff_name_changes"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    before_name: Mapped[str] = mapped_column(String(100))
    after_name: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    operator_uid: Mapped[int] = mapped_column()


class WorkerSchedule(Base):
    __tablename__ = "worker_schedules"
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class SchoolCatalogImport(IdentityTimestamps, Base):
    __tablename__ = "school_catalog_imports"
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    base_revision: Mapped[int] = mapped_column(BigInteger)
    valid: Mapped[bool] = mapped_column()
    errors: Mapped[list] = mapped_column(JSONB)
    changes: Mapped[dict] = mapped_column(JSONB)
    candidate: Mapped[dict] = mapped_column(JSONB)


class SchoolCatalogVersion(Base):
    __tablename__ = "school_catalog_versions"
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("school_catalog_imports.id"))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    content: Mapped[dict] = mapped_column(JSONB)
    dataset_revision: Mapped[int | None] = mapped_column(BigInteger)
    dataset_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchoolPlanningDataset(Base):
    __tablename__ = "school_planning_datasets"
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(100))
    evidence: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchoolCatalogActive(Base):
    __tablename__ = "school_catalog_active"
    __table_args__ = (CheckConstraint("id = 1", name="school_catalog_active_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_revision: Mapped[int] = mapped_column(ForeignKey("school_catalog_versions.revision"))
    dataset_revision: Mapped[int | None] = mapped_column(
        ForeignKey("school_planning_datasets.revision")
    )
    dataset_hash: Mapped[str | None] = mapped_column(String(64))


class WorkerProfile(Base):
    __tablename__ = "worker_profiles"
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    department_code: Mapped[str | None] = mapped_column(String(30))
    field_codes: Mapped[list] = mapped_column(JSONB)
    catalog_revision: Mapped[int] = mapped_column(BigInteger)


class WorkerProfileChange(IdentityTimestamps, Base):
    __tablename__ = "worker_profile_changes"
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    before: Mapped[dict] = mapped_column(JSONB)
    after: Mapped[dict] = mapped_column(JSONB)


class DispatchPlan(IdentityTimestamps, Base):
    __tablename__ = "dispatch_plans"
    parent_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dispatch_plans.id"))
    kind: Mapped[str] = mapped_column(String(20), default="initial")
    insertion_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_reason: Mapped[str | None] = mapped_column(Text)
    cancellation_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_data: Mapped[dict] = mapped_column(JSONB)
    result_data: Mapped[dict] = mapped_column(JSONB)
    snapshot_data: Mapped[dict] = mapped_column(JSONB)
    request_data: Mapped[dict] = mapped_column(JSONB)
    excluded: Mapped[list] = mapped_column(JSONB, default=list)
    input_hash: Mapped[str] = mapped_column(String(64))


class AssignmentCredentialReceipt(IdentityTimestamps, Base):
    __tablename__ = "assignment_credential_receipts"
    __table_args__ = (UniqueConstraint("assignment_id", "request_id"),)
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assignments.id"))
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    expected_revision: Mapped[int] = mapped_column()
    issued_revision: Mapped[int] = mapped_column()


class PlanMembership(IdentityTimestamps, Base):
    __tablename__ = "plan_memberships"
    __table_args__ = (UniqueConstraint("plan_id", "assignment_id"),)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dispatch_plans.id"))
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assignments.id"))
    kind: Mapped[str] = mapped_column(String(20))
    original_assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assignments.id"))
    sequence: Mapped[int] = mapped_column()
    timing_data: Mapped[dict] = mapped_column(JSONB, default=dict)


class ServicePlanExecutionEntry(IdentityTimestamps, Base):
    __tablename__ = "service_plan_execution_entries"
    __table_args__ = (
        UniqueConstraint("plan_id", "assignment_id", name="service_entry_plan_assignment_unique"),
        UniqueConstraint(
            "plan_id", "worker_id", "execution_order", name="service_entry_path_unique"
        ),
        UniqueConstraint(
            "plan_id", "worker_id", "assignment_id", name="service_entry_identity_unique"
        ),
        ForeignKeyConstraint(
            ["assignment_id", "worker_id"],
            ["assignments.id", "assignments.worker_id"],
            name="service_entry_assignment_worker_fk",
        ),
        ForeignKeyConstraint(
            ["plan_id", "worker_id", "predecessor_assignment_id"],
            [
                "service_plan_execution_entries.plan_id",
                "service_plan_execution_entries.worker_id",
                "service_plan_execution_entries.assignment_id",
            ],
            name="service_entry_predecessor_fk",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("execution_order >= 0", name="service_entry_order_nonnegative"),
        CheckConstraint(
            "classification IN ('history','fixed','route','excluded')", name="service_entry_class"
        ),
        CheckConstraint(
            "predecessor_assignment_id IS NULL OR predecessor_assignment_id <> assignment_id",
            name="service_entry_not_self_predecessor",
        ),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dispatch_plans.id"))
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    worker_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    execution_order: Mapped[int] = mapped_column()
    predecessor_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_memberships.id")
    )
    classification: Mapped[str] = mapped_column(String(20))
    origin_data: Mapped[dict] = mapped_column(JSONB)


class ServicePlanDisposition(IdentityTimestamps, Base):
    __tablename__ = "service_plan_dispositions"
    __table_args__ = (
        UniqueConstraint(
            "plan_id", "parent_assignment_id", name="service_disposition_parent_unique"
        ),
        CheckConstraint(
            "(classification = 'replaced' AND replacement_assignment_id IS NOT NULL) OR "
            "(classification = 'unassigned' AND replacement_assignment_id IS NULL)",
            name="service_disposition_class",
        ),
        CheckConstraint(
            "replacement_assignment_id IS NULL OR "
            "replacement_assignment_id <> parent_assignment_id",
            name="service_disposition_not_self",
        ),
        ForeignKeyConstraint(
            ["plan_id", "replacement_assignment_id"],
            [
                "service_plan_execution_entries.plan_id",
                "service_plan_execution_entries.assignment_id",
            ],
            name="service_disposition_replacement_fk",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dispatch_plans.id"))
    parent_assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assignments.id"))
    parent_membership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plan_memberships.id"))
    classification: Mapped[str] = mapped_column(String(20))
    replacement_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ServicePlanResidual(IdentityTimestamps, Base):
    """Append-only provenance for a service insertion's unresolved ticket."""

    __tablename__ = "service_plan_residuals"
    __table_args__ = (
        UniqueConstraint("plan_id", "ticket_id", name="service_residual_plan_ticket_unique"),
        UniqueConstraint("id", "ticket_id", name="service_residual_identity_ticket_unique"),
        ForeignKeyConstraint(
            ["previous_residual_id", "ticket_id"],
            ["service_plan_residuals.id", "service_plan_residuals.ticket_id"],
            name="service_residual_previous_ticket_fk",
        ),
        ForeignKeyConstraint(
            ["plan_id", "assigned_assignment_id"],
            [
                "service_plan_execution_entries.plan_id",
                "service_plan_execution_entries.assignment_id",
            ],
            name="service_residual_assigned_entry_fk",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("version = 1", name="service_residual_version"),
        CheckConstraint(
            "origin_kind IN ('initial_input','insertion_urgent')",
            name="service_residual_origin_kind",
        ),
        CheckConstraint(
            "service_min > 0 AND deadline_at >= release_at", name="service_residual_timing"
        ),
        CheckConstraint("length(reason) > 0", name="service_residual_reason"),
        CheckConstraint(
            "(previous_assignment_id IS NULL) = (previous_disposition_id IS NULL)",
            name="service_residual_previous_disposition_pair",
        ),
        CheckConstraint(
            "previous_residual_id IS NULL OR previous_residual_id <> id",
            name="service_residual_not_self",
        ),
        CheckConstraint(
            "(outcome = 'assigned' AND assigned_assignment_id IS NOT NULL "
            "AND external_assignment_id IS NULL AND external_execution_revision IS NULL "
            "AND report_id IS NULL AND report_revision IS NULL AND terminal_status IS NULL "
            "AND terminal_updated_at IS NULL AND terminal_event_id IS NULL) OR "
            "(outcome = 'external' AND assigned_assignment_id IS NULL "
            "AND external_assignment_id IS NOT NULL AND external_execution_revision IS NOT NULL "
            "AND external_execution_revision >= 0 "
            "AND report_id IS NULL AND report_revision IS NULL AND terminal_status IS NULL "
            "AND terminal_updated_at IS NULL AND terminal_event_id IS NULL) OR "
            "(outcome = 'report' AND assigned_assignment_id IS NULL "
            "AND external_assignment_id IS NULL AND external_execution_revision IS NULL "
            "AND report_id IS NOT NULL AND report_revision IS NOT NULL "
            "AND report_revision > 0 AND terminal_status IS NULL "
            "AND terminal_updated_at IS NULL AND terminal_event_id IS NULL) OR "
            "(outcome = 'terminal' AND assigned_assignment_id IS NULL "
            "AND external_assignment_id IS NULL AND external_execution_revision IS NULL "
            "AND report_id IS NULL AND report_revision IS NULL "
            "AND terminal_status IS NOT NULL AND terminal_status IN "
            "('completed','merged','cancelled','rejected','transferred') "
            "AND terminal_updated_at IS NOT NULL AND terminal_event_id IS NOT NULL) OR "
            "(outcome IN ('unassigned','unavailable','planning_data') "
            "AND assigned_assignment_id IS NULL AND external_assignment_id IS NULL "
            "AND external_execution_revision IS NULL AND report_id IS NULL "
            "AND report_revision IS NULL "
            "AND terminal_status IS NULL AND terminal_updated_at IS NULL "
            "AND terminal_event_id IS NULL)",
            name="service_residual_outcome_evidence",
        ),
    )
    version: Mapped[int] = mapped_column(default=1)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dispatch_plans.id"))
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    source_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dispatch_plans.id"))
    origin_kind: Mapped[str] = mapped_column(String(20))
    previous_residual_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    release_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    service_min: Mapped[int] = mapped_column()
    previous_assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assignments.id"))
    previous_disposition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_plan_dispositions.id")
    )
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(100))
    assigned_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    external_assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assignments.id"))
    external_execution_revision: Mapped[int | None] = mapped_column()
    report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("completion_reports.id"))
    report_revision: Mapped[int | None] = mapped_column()
    terminal_status: Mapped[str | None] = mapped_column(String(20))
    terminal_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ticket_events.id"))


class MailJob(IdentityTimestamps, Base):
    """Encrypted delivery intent; no recipient or body appears in audit metadata."""

    __tablename__ = "mail_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','claimed','sending','retry_wait','local_saved',"
            "'accepted','unknown','failed','expired','superseded')",
            name="mail_state",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="mail_attempt_limit"),
        UniqueConstraint("dedupe_key", name="mail_dedupe"),
        UniqueConstraint("recovery_parent_id", name="mail_recovery_parent"),
        Index("mail_due", "state", "next_attempt_at"),
    )
    purpose: Mapped[str] = mapped_column(String(30))
    namespace: Mapped[str] = mapped_column(String(50))
    delivery: Mapped[str] = mapped_column(String(16))
    template_version: Mapped[str] = mapped_column(String(30), default="mail-v1")
    dedupe_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    encrypted_payload: Mapped[str | None] = mapped_column(Text)
    challenge_key: Mapped[str | None] = mapped_column(String(64))
    challenge_nonce: Mapped[str | None] = mapped_column(String(64))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE")
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("completion_reports.id", ondelete="CASCADE")
    )
    information_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("information_requests.id", ondelete="CASCADE")
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    merge_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ticket_merges.id", ondelete="CASCADE")
    )
    recovery_parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_jobs.id", ondelete="CASCADE")
    )
    approval_hash: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))


class MailAttempt(IdentityTimestamps, Base):
    __tablename__ = "mail_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt_no", name="mail_attempt_number"),)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mail_jobs.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column()
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(20), default="claimed")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))


class ExportRecord(IdentityTimestamps, Base):
    __tablename__ = "export_records"
    __table_args__ = (
        UniqueConstraint("creator_id", "namespace", "request_key", name="export_request_key"),
        CheckConstraint("row_count BETWEEN 1 AND 50", name="export_row_limit"),
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    namespace: Mapped[str] = mapped_column(String(50))
    request_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column()
    schema_version: Mapped[str] = mapped_column(String(30), default="baroai-csv-v1")
    encrypted_snapshot: Mapped[str | None] = mapped_column(Text)
    encrypted_audit: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExportDownload(IdentityTimestamps, Base):
    __tablename__ = "export_downloads"
    export_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("export_records.id", ondelete="CASCADE")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
