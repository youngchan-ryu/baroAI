"""Separate visits from resolution and preserve multi-generation dispatch membership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "b17a20261024"
down_revision = "b16b20261024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dispatch_plans",
        sa.Column("parent_plan_id", UUID(as_uuid=True), sa.ForeignKey("dispatch_plans.id")),
    )
    op.add_column(
        "dispatch_plans", sa.Column("kind", sa.String(20), nullable=False, server_default="initial")
    )
    op.add_column(
        "dispatch_plans", sa.Column("insertion_data", JSONB(), nullable=False, server_default="{}")
    )
    for column in ("kind", "insertion_data"):
        op.alter_column("dispatch_plans", column, server_default=None)
    op.add_column(
        "assignments",
        sa.Column("active_plan_id", UUID(as_uuid=True), sa.ForeignKey("dispatch_plans.id")),
    )
    op.add_column(
        "assignments",
        sa.Column("predecessor_id", UUID(as_uuid=True), sa.ForeignKey("assignments.id")),
    )
    op.add_column(
        "assignments",
        sa.Column("execution_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "assignments", sa.Column("remaining_data", JSONB(), nullable=False, server_default="{}")
    )
    for column in ("execution_revision", "remaining_data"):
        op.alter_column("assignments", column, server_default=None)
    for column in ("departed_at", "started_at", "visit_ended_at"):
        op.add_column("assignments", sa.Column(column, sa.DateTime(timezone=True)))
    op.add_column("assignments", sa.Column("execution_location_code", sa.String(100)))
    op.execute("UPDATE assignments SET active_plan_id=plan_id")
    # Preserve unknown legacy execution timestamps; no invented travel/start/end times.
    op.create_table(
        "plan_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "plan_id", UUID(as_uuid=True), sa.ForeignKey("dispatch_plans.id"), nullable=False
        ),
        sa.Column(
            "assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignments.id"), nullable=False
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("original_assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignments.id")),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timing_data", JSONB(), nullable=False),
        sa.UniqueConstraint("plan_id", "assignment_id"),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM plan_memberships) "
                "OR EXISTS(SELECT 1 FROM assignments WHERE execution_revision>0) "
                "OR EXISTS(SELECT 1 FROM dispatch_plans WHERE kind='insertion')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Execution/dispatch audit must be preserved before downgrade")
    op.drop_table("plan_memberships")
    for column in (
        "execution_location_code",
        "visit_ended_at",
        "started_at",
        "departed_at",
        "remaining_data",
        "execution_revision",
        "predecessor_id",
        "active_plan_id",
    ):
        op.drop_column("assignments", column)
    for column in ("insertion_data", "kind", "parent_plan_id"):
        op.drop_column("dispatch_plans", column)
