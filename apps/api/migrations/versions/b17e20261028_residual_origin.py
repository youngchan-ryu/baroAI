"""Record explicit residual origin without guessing existing data."""

import sqlalchemy as sa
from alembic import op

revision = "b17e20261028"
down_revision = "b17d20261028"
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
        raise RuntimeError("service_residual_origin_migration_schema_mismatch")


def require_empty():
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE public.service_plan_residuals IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM public.service_plan_residuals)")
    ).scalar():
        raise RuntimeError("service_residual_origin_evidence_present")


def upgrade():
    require_public_schema()
    require_empty()
    op.add_column(
        "service_plan_residuals",
        sa.Column("origin_kind", sa.String(20), nullable=False),
        schema="public",
    )
    op.create_check_constraint(
        "service_residual_origin_kind",
        "service_plan_residuals",
        "origin_kind IN ('initial_input','insertion_urgent')",
        schema="public",
    )


def downgrade():
    require_public_schema()
    require_empty()
    op.drop_constraint(
        "service_residual_origin_kind", "service_plan_residuals", type_="check", schema="public"
    )
    op.drop_column("service_plan_residuals", "origin_kind", schema="public")
