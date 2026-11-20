"""Minimal users, tickets and auditable events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def identity():
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade():
    op.create_table(
        "users",
        *identity(),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.CheckConstraint(
            "role IN ('reporter','dispatcher','worker','admin','system')", name="user_role"
        ),
    )
    op.create_table(
        "tickets",
        *identity(),
        sa.Column("reporter_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('created','needs_info','received','confirmed','assigned','in_progress',"
            "'completed','transferred','merged','rejected','cancelled','reopened')",
            name="ticket_status",
        ),
    )
    op.create_index("ix_tickets_reporter_id", "tickets", ["reporter_id"])
    op.create_table(
        "ticket_events",
        *identity(),
        sa.Column("ticket_id", UUID(as_uuid=True), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("from_status", sa.String(20)),
        sa.Column("to_status", sa.String(20)),
    )
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])


def downgrade():
    op.drop_table("ticket_events")
    op.drop_table("tickets")
    op.drop_table("users")
