"""Synthetic dispatch proposal snapshots, schedules, and secret-free credential receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "b16a20261024"
down_revision = "b18b20261024"
branch_labels = None
depends_on = None


def identity():
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "worker_schedules",
        sa.Column("worker_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
    )
    op.create_table(
        "dispatch_plans",
        *identity(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approval_reason", sa.Text()),
        sa.Column("plan_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *[
            sa.Column(n, JSONB(), nullable=False)
            for n in ("input_data", "result_data", "snapshot_data", "request_data", "excluded")
        ],
        sa.Column("input_hash", sa.String(64), nullable=False),
    )
    op.add_column(
        "assignments",
        sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("assignments", "credential_revision", server_default=None)
    op.add_column(
        "assignments", sa.Column("plan_id", UUID(as_uuid=True), sa.ForeignKey("dispatch_plans.id"))
    )
    for n in ("plan_sequence", "planned_arrival_min", "planned_completion_min"):
        op.add_column("assignments", sa.Column(n, sa.Integer()))
    op.create_table(
        "assignment_credential_receipts",
        *identity(),
        sa.Column(
            "assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignments.id"), nullable=False
        ),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("issued_revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("assignment_id", "request_id"),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM dispatch_plans) OR "
                "EXISTS(SELECT 1 FROM assignment_credential_receipts)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Dispatch audit records must be preserved before downgrade")
    op.drop_table("assignment_credential_receipts")
    for n in (
        "planned_completion_min",
        "planned_arrival_min",
        "plan_sequence",
        "plan_id",
        "credential_revision",
    ):
        op.drop_column("assignments", n)
    op.drop_table("dispatch_plans")
    op.drop_table("worker_schedules")
