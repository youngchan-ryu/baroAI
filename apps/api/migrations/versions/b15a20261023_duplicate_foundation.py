"""Add reversible merge relationships and version-scoped candidate dismissals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b15a20261023"
down_revision = "b13b20261023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tickets",
        sa.Column("linked_safety_floor", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "tickets",
        sa.Column(
            "linked_safety_requires_review", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("tickets", "linked_safety_floor", server_default=None)
    op.alter_column("tickets", "linked_safety_requires_review", server_default=None)
    op.create_table(
        "ticket_merges",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("previous_status", sa.String(20), nullable=False),
        sa.Column("approved_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("target_version", sa.String(64), nullable=False),
        sa.Column("safety_snapshot", pg.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("subscribed", sa.Boolean(), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.Text()),
        sa.CheckConstraint("source_id <> target_id", name="ticket_merge_distinct"),
        sa.CheckConstraint("revision >= 1", name="ticket_merge_revision"),
        sa.CheckConstraint(
            "previous_status IN ('received','confirmed','assigned','in_progress')",
            name="ticket_merge_previous_status",
        ),
    )
    op.create_index("ix_ticket_merges_source_id", "ticket_merges", ["source_id"])
    op.create_index("ix_ticket_merges_target_id", "ticket_merges", ["target_id"])
    op.create_index(
        "ticket_merge_active_source",
        "ticket_merges",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
    )
    op.create_table(
        "duplicate_dismissals",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("target_version", sa.String(64), nullable=False),
        sa.Column("actor_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("source_id <> target_id", name="duplicate_dismissal_distinct"),
        sa.UniqueConstraint(
            "source_id",
            "target_id",
            "source_version",
            "target_version",
            name="duplicate_dismissal_versions",
        ),
    )
    op.create_index("ix_duplicate_dismissals_source_id", "duplicate_dismissals", ["source_id"])
    op.create_index("ix_duplicate_dismissals_target_id", "duplicate_dismissals", ["target_id"])


def downgrade():
    # Removing merge or safety evidence requires a separately reviewed rollback strategy.
    bind = op.get_bind()
    has_history = bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM ticket_merges) "
            "OR EXISTS(SELECT 1 FROM duplicate_dismissals)"
        )
    )
    has_safety = bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM tickets WHERE linked_safety_floor < 3 "
            "OR linked_safety_requires_review)"
        )
    )
    if has_history or has_safety:
        raise RuntimeError("rollback_requires_preserving_merge_audit_and_safety")
    op.drop_table("duplicate_dismissals")
    op.drop_table("ticket_merges")
    op.drop_column("tickets", "linked_safety_requires_review")
    op.drop_column("tickets", "linked_safety_floor")
