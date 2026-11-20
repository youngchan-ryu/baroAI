"""Durable ticket analysis outbox and attempts; no legacy ticket reprocessing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b13a20261023"
down_revision = "b11a20261023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tickets", sa.Column("analysis_revision", sa.Integer(), nullable=False, server_default="0")
    )
    op.alter_column("tickets", "analysis_revision", server_default=None)
    op.alter_column("ticket_events", "actor_id", nullable=True)
    op.create_table(
        "processing_jobs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "ticket_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", pg.UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(50)),
        sa.UniqueConstraint("ticket_id", "kind", "input_revision", name="processing_job_revision"),
        sa.CheckConstraint(
            "state IN ('queued','running','retry_wait','succeeded','failed','superseded')",
            name="processing_job_state",
        ),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 3", name="processing_job_attempt_count"),
    )
    op.create_index("ix_processing_jobs_ticket_id", "processing_jobs", ["ticket_id"])
    op.create_index(
        "ix_processing_jobs_due",
        "processing_jobs",
        ["state", "next_attempt_at", "lease_expires_at"],
    )
    op.create_table(
        "processing_attempts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "job_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("processing_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("metadata_json", pg.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("job_id", "attempt_no", name="processing_attempt_number"),
    )
    op.create_index("ix_processing_attempts_job_id", "processing_attempts", ["job_id"])


def downgrade():
    # Rollback cannot erase system audit events or attribute them to a human.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM ticket_events WHERE actor_id IS NULL)")
    ):
        raise RuntimeError("rollback_requires_preserving_system_audit_events")
    op.drop_table("processing_attempts")
    op.drop_table("processing_jobs")
    op.alter_column("ticket_events", "actor_id", nullable=False)
    op.drop_column("tickets", "analysis_revision")
