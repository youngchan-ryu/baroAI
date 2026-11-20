"""Public intake transactional limits and operator pause."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v10920261109"
down_revision = "v10820261108"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "public_intake_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id = 1", name="public_intake_singleton"),
    )
    op.create_table(
        "public_limit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("subject", sa.String(64), nullable=False),
        sa.CheckConstraint("kind IN ('guest','ticket')", name="public_limit_kind"),
    )
    op.create_index(
        "ix_public_limit_event_lookup", "public_limit_events", ["kind", "subject", "created_at"]
    )


def downgrade():
    op.drop_index("ix_public_limit_event_lookup", table_name="public_limit_events")
    op.drop_table("public_limit_events")
    op.drop_table("public_intake_control")
