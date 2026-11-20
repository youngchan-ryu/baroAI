"""Bind public staff sessions to individually revocable invites."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v10820261108"
down_revision = "v10720261103"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "public_staff_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_sessions.id"),
            unique=True,
        ),
        sa.CheckConstraint("role IN ('dispatcher','worker')", name="public_staff_invite_role"),
    )
    op.create_index("ix_public_staff_invites_user_id", "public_staff_invites", ["user_id"])


def downgrade():
    op.drop_index("ix_public_staff_invites_user_id", table_name="public_staff_invites")
    op.drop_table("public_staff_invites")
