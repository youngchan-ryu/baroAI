"""Add active planning dataset generation and immutable activation audit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b16a20261028"
down_revision = "b19e20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "planning_dataset_active",
        sa.Column("namespace", sa.String(60), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(100), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.CheckConstraint("revision >= 1", name="planning_dataset_active_revision"),
    )
    op.create_table(
        "planning_dataset_activations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("namespace", sa.String(60), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(100), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_uid", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("namespace", "revision", name="planning_dataset_activation_unique"),
        sa.CheckConstraint("revision >= 1", name="planning_dataset_activation_revision"),
    )


def downgrade():
    op.drop_table("planning_dataset_activations")
    op.drop_table("planning_dataset_active")
