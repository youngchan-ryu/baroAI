"""Add pending location snapshots without retroactively confirming legacy tickets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b11a20261023"
down_revision = "a31d02b9c147"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("location_data", "routing_data"):
        op.add_column(
            "tickets",
            sa.Column(
                name, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
        )
    op.add_column(
        "tickets",
        sa.Column(
            "location_history",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    for name in ("location_version", "location_answer_count"):
        op.add_column("tickets", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    for name in (
        "location_data",
        "routing_data",
        "location_history",
        "location_version",
        "location_answer_count",
    ):
        op.alter_column("tickets", name, server_default=None)


def downgrade():
    for name in (
        "location_data",
        "routing_data",
        "location_history",
        "location_version",
        "location_answer_count",
    ):
        op.drop_column("tickets", name)
