"""Preserve report correction lineage and request metadata."""

import sqlalchemy as sa
from alembic import op

revision = "v10320261103"
down_revision = "b17e20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("completion_reports", sa.Column("supersedes_report_id", sa.Uuid(), nullable=True))
    op.add_column(
        "completion_reports", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "completion_reports",
        sa.Column("correction_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("completion_reports", sa.Column("correction_reason", sa.Text(), nullable=True))
    op.add_column(
        "completion_reports", sa.Column("correction_requested_by", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "report_supersedes_fk",
        "completion_reports",
        "completion_reports",
        ["supersedes_report_id"],
        ["id"],
    )
    op.create_foreign_key(
        "report_correction_actor_fk",
        "completion_reports",
        "users",
        ["correction_requested_by"],
        ["id"],
    )
    op.create_unique_constraint(
        "report_supersedes_unique", "completion_reports", ["supersedes_report_id"]
    )


def downgrade():
    op.drop_constraint("report_supersedes_unique", "completion_reports", type_="unique")
    op.drop_constraint("report_correction_actor_fk", "completion_reports", type_="foreignkey")
    op.drop_constraint("report_supersedes_fk", "completion_reports", type_="foreignkey")
    for name in (
        "correction_requested_by",
        "correction_reason",
        "correction_requested_at",
        "superseded_at",
        "supersedes_report_id",
    ):
        op.drop_column("completion_reports", name)
