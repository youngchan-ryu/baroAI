"""Record staff account lifecycle events without credentials."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v11120261116"
down_revision = "v11020261114"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "staff_account_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("staff_credentials.id"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint(
            "action IN ('applied','approved','rejected','revoked')", name="staff_event_action"
        ),
    )


def downgrade():
    op.drop_table("staff_account_events")
