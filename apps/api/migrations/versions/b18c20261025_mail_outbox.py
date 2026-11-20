"""Encrypted mail intents and attempt audit; no historical mail backfill."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b18c20261025"
down_revision = "b17b20261024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mail_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("namespace", sa.String(50), nullable=False),
        sa.Column("delivery", sa.String(16), nullable=False),
        sa.Column("template_version", sa.String(30), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("encrypted_payload", sa.Text()),
        sa.Column("challenge_key", sa.String(64)),
        sa.Column("challenge_nonce", sa.String(64)),
        sa.Column("ticket_id", UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE")),
        sa.Column(
            "report_id",
            UUID(as_uuid=True),
            sa.ForeignKey("completion_reports.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "recipient_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")
        ),
        sa.Column(
            "merge_id", UUID(as_uuid=True), sa.ForeignKey("ticket_merges.id", ondelete="CASCADE")
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(50)),
        sa.CheckConstraint(
            "state IN ('queued','claimed','sending','retry_wait','local_saved',"
            "'accepted','unknown','failed','expired','superseded')",
            name="mail_state",
        ),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 3", name="mail_attempt_limit"),
        sa.UniqueConstraint("dedupe_key", name="mail_dedupe"),
    )
    op.create_index("mail_due", "mail_jobs", ["state", "next_attempt_at"])
    op.create_table(
        "mail_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mail_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("lease_token", UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(50)),
        sa.UniqueConstraint("job_id", "attempt_no", name="mail_attempt_number"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM mail_jobs)")).scalar():
        raise RuntimeError("Mail delivery audit must be preserved before downgrade")
    op.drop_table("mail_attempts")
    op.drop_index("mail_due", table_name="mail_jobs")
    op.drop_table("mail_jobs")
