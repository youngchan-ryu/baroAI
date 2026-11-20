"""Store dispatcher information requests and persistent safety evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v11220261116"
down_revision = "v11120261116"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tickets",
        sa.Column(
            "information_safety_evidence",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "tickets",
        sa.Column(
            "information_mail_allowed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.create_table(
        "information_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("question", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("mail_intent_failed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open','answered','cancelled')", name="information_request_status"
        ),
    )
    op.create_index("ix_information_requests_ticket_id", "information_requests", ["ticket_id"])
    op.create_index(
        "information_request_one_open",
        "information_requests",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.add_column("uploads", sa.Column("information_request_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_upload_information_request",
        "uploads",
        "information_requests",
        ["information_request_id"],
        ["id"],
    )
    op.add_column(
        "mail_jobs",
        sa.Column("information_request_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_mail_information_request",
        "mail_jobs",
        "information_requests",
        ["information_request_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade():
    op.drop_constraint("fk_mail_information_request", "mail_jobs", type_="foreignkey")
    op.drop_column("mail_jobs", "information_request_id")
    op.drop_constraint("fk_upload_information_request", "uploads", type_="foreignkey")
    op.drop_column("uploads", "information_request_id")
    op.drop_index("information_request_one_open", table_name="information_requests")
    op.drop_index("ix_information_requests_ticket_id", table_name="information_requests")
    op.drop_table("information_requests")
    op.drop_column("tickets", "information_mail_allowed")
    op.drop_column("tickets", "information_safety_evidence")
