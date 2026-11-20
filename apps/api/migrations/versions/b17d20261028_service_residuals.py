"""Add append-only service insertion residual evidence; no automatic backfill."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b17d20261028"
down_revision = "b17c20261028"
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
        raise RuntimeError("service_residual_migration_schema_mismatch")


def upgrade():
    require_public_schema()
    op.create_table(
        "service_plan_residuals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "plan_id", UUID(as_uuid=True), sa.ForeignKey("public.dispatch_plans.id"), nullable=False
        ),
        sa.Column(
            "ticket_id", UUID(as_uuid=True), sa.ForeignKey("public.tickets.id"), nullable=False
        ),
        sa.Column(
            "source_plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.dispatch_plans.id"),
            nullable=False,
        ),
        sa.Column("previous_residual_id", UUID(as_uuid=True)),
        sa.Column("release_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_min", sa.Integer(), nullable=False),
        sa.Column(
            "previous_assignment_id", UUID(as_uuid=True), sa.ForeignKey("public.assignments.id")
        ),
        sa.Column(
            "previous_disposition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.service_plan_dispositions.id"),
        ),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("assigned_assignment_id", UUID(as_uuid=True)),
        sa.Column(
            "external_assignment_id", UUID(as_uuid=True), sa.ForeignKey("public.assignments.id")
        ),
        sa.Column("external_execution_revision", sa.Integer()),
        sa.Column("report_id", UUID(as_uuid=True), sa.ForeignKey("public.completion_reports.id")),
        sa.Column("report_revision", sa.Integer()),
        sa.Column("terminal_status", sa.String(20)),
        sa.Column("terminal_updated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "terminal_event_id", UUID(as_uuid=True), sa.ForeignKey("public.ticket_events.id")
        ),
        sa.UniqueConstraint("plan_id", "ticket_id", name="service_residual_plan_ticket_unique"),
        sa.UniqueConstraint("id", "ticket_id", name="service_residual_identity_ticket_unique"),
        sa.ForeignKeyConstraint(
            ["previous_residual_id", "ticket_id"],
            ["public.service_plan_residuals.id", "public.service_plan_residuals.ticket_id"],
            name="service_residual_previous_ticket_fk",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "assigned_assignment_id"],
            [
                "public.service_plan_execution_entries.plan_id",
                "public.service_plan_execution_entries.assignment_id",
            ],
            name="service_residual_assigned_entry_fk",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "previous_residual_id IS NULL OR previous_residual_id <> id",
            name="service_residual_not_self",
        ),
        sa.CheckConstraint(
            "(outcome = 'assigned' AND assigned_assignment_id IS NOT NULL AND "
            "external_assignment_id IS NULL AND external_execution_revision IS NULL "
            "AND report_id IS NULL AND report_revision IS NULL AND terminal_status IS "
            "NULL AND terminal_updated_at IS NULL AND terminal_event_id IS NULL) OR "
            "(outcome = 'external' AND assigned_assignment_id IS NULL AND "
            "external_assignment_id IS NOT NULL AND external_execution_revision IS "
            "NOT NULL AND external_execution_revision >= 0 AND report_id IS NULL AND "
            "report_revision IS NULL AND terminal_status IS NULL AND "
            "terminal_updated_at IS NULL AND terminal_event_id IS NULL) OR (outcome = "
            "'report' AND assigned_assignment_id IS NULL AND external_assignment_id "
            "IS NULL AND external_execution_revision IS NULL AND report_id IS NOT "
            "NULL AND report_revision IS NOT NULL AND report_revision > 0 AND "
            "terminal_status IS NULL AND terminal_updated_at IS NULL AND "
            "terminal_event_id IS NULL) OR (outcome = 'terminal' AND "
            "assigned_assignment_id IS NULL AND external_assignment_id IS NULL AND "
            "external_execution_revision IS NULL AND report_id IS NULL AND "
            "report_revision IS NULL AND terminal_status IS NOT NULL AND "
            "terminal_status IN "
            "('completed','merged','cancelled','rejected','transferred') AND "
            "terminal_updated_at IS NOT NULL AND terminal_event_id IS NOT NULL) OR "
            "(outcome IN ('unassigned','unavailable','planning_data') AND "
            "assigned_assignment_id IS NULL AND external_assignment_id IS NULL AND "
            "external_execution_revision IS NULL AND report_id IS NULL AND "
            "report_revision IS NULL AND terminal_status IS NULL AND "
            "terminal_updated_at IS NULL AND terminal_event_id IS NULL) ",
            name="service_residual_outcome_evidence",
        ),
        sa.CheckConstraint(
            "(previous_assignment_id IS NULL) = (previous_disposition_id IS NULL)",
            name="service_residual_previous_disposition_pair",
        ),
        sa.CheckConstraint("length(reason) > 0", name="service_residual_reason"),
        sa.CheckConstraint(
            "service_min > 0 AND deadline_at >= release_at", name="service_residual_timing"
        ),
        sa.CheckConstraint("version = 1", name="service_residual_version"),
        schema="public",
    )
    op.execute("""
        CREATE FUNCTION public.reject_service_residual_change() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'service_residual_immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER service_residual_immutable BEFORE UPDATE OR DELETE
        ON public.service_plan_residuals FOR EACH ROW
        EXECUTE FUNCTION public.reject_service_residual_change()
    """)


def downgrade():
    require_public_schema()
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE public.service_plan_residuals IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM public.service_plan_residuals)")
    ).scalar():
        raise RuntimeError("service_residual_downgrade_evidence_present")
    op.drop_table("service_plan_residuals", schema="public")
    op.execute("DROP FUNCTION public.reject_service_residual_change()")
