"""Add internal service insertion execution evidence; public insertion stays disabled."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "b17c20261028"
down_revision = "b19f20261028"
branch_labels = None
depends_on = None


def require_public_schema():
    connection = op.get_bind()
    valid = connection.execute(
        sa.text(
            "SELECT current_schema() = 'public' "
            "AND to_regclass('alembic_version') = to_regclass('public.alembic_version') "
            "AND to_regclass('public.alembic_version') IS NOT NULL"
        )
    ).scalar()
    if op.get_context().version_table_schema not in (None, "public") or not valid:
        raise RuntimeError("service_execution_migration_schema_mismatch")


def upgrade():
    require_public_schema()
    op.create_unique_constraint(
        "assignment_id_worker_unique", "assignments", ["id", "worker_id"], schema="public"
    )
    op.create_table(
        "service_plan_execution_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "plan_id", UUID(as_uuid=True), sa.ForeignKey("public.dispatch_plans.id"), nullable=False
        ),
        sa.Column("assignment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", UUID(as_uuid=True), nullable=False),
        sa.Column("execution_order", sa.Integer(), nullable=False),
        sa.Column("predecessor_assignment_id", UUID(as_uuid=True)),
        sa.Column(
            "parent_membership_id", UUID(as_uuid=True), sa.ForeignKey("public.plan_memberships.id")
        ),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("origin_data", JSONB(), nullable=False),
        sa.UniqueConstraint(
            "plan_id", "assignment_id", name="service_entry_plan_assignment_unique"
        ),
        sa.UniqueConstraint(
            "plan_id", "worker_id", "execution_order", name="service_entry_path_unique"
        ),
        sa.UniqueConstraint(
            "plan_id", "worker_id", "assignment_id", name="service_entry_identity_unique"
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id", "worker_id"],
            ["public.assignments.id", "public.assignments.worker_id"],
            name="service_entry_assignment_worker_fk",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "worker_id", "predecessor_assignment_id"],
            [
                "public.service_plan_execution_entries.plan_id",
                "public.service_plan_execution_entries.worker_id",
                "public.service_plan_execution_entries.assignment_id",
            ],
            name="service_entry_predecessor_fk",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("execution_order >= 0", name="service_entry_order_nonnegative"),
        sa.CheckConstraint(
            "classification IN ('history','fixed','route','excluded')", name="service_entry_class"
        ),
        sa.CheckConstraint(
            "predecessor_assignment_id IS NULL OR predecessor_assignment_id <> assignment_id",
            name="service_entry_not_self_predecessor",
        ),
        schema="public",
    )
    op.create_table(
        "service_plan_dispositions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "plan_id", UUID(as_uuid=True), sa.ForeignKey("public.dispatch_plans.id"), nullable=False
        ),
        sa.Column(
            "parent_assignment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.assignments.id"),
            nullable=False,
        ),
        sa.Column(
            "parent_membership_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.plan_memberships.id"),
            nullable=False,
        ),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("replacement_assignment_id", UUID(as_uuid=True)),
        sa.UniqueConstraint(
            "plan_id", "parent_assignment_id", name="service_disposition_parent_unique"
        ),
        sa.CheckConstraint(
            "(classification = 'replaced' AND replacement_assignment_id IS NOT NULL) OR "
            "(classification = 'unassigned' AND replacement_assignment_id IS NULL)",
            name="service_disposition_class",
        ),
        sa.CheckConstraint(
            "replacement_assignment_id IS NULL OR "
            "replacement_assignment_id <> parent_assignment_id",
            name="service_disposition_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "replacement_assignment_id"],
            [
                "public.service_plan_execution_entries.plan_id",
                "public.service_plan_execution_entries.assignment_id",
            ],
            name="service_disposition_replacement_fk",
            deferrable=True,
            initially="DEFERRED",
        ),
        schema="public",
    )


def downgrade():
    require_public_schema()
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE public.service_plan_dispositions, public.service_plan_execution_entries "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    populated = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM public.service_plan_execution_entries) OR "
            "EXISTS (SELECT 1 FROM public.service_plan_dispositions)"
        )
    ).scalar()
    if populated:
        raise RuntimeError("service_execution_downgrade_evidence_present")
    op.drop_table("service_plan_dispositions", schema="public")
    op.drop_table("service_plan_execution_entries", schema="public")
    op.drop_constraint(
        "assignment_id_worker_unique", "assignments", type_="unique", schema="public"
    )
