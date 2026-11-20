"""Audit explicit service staff grants and invalidate old worker capabilities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b18b20261024"
down_revision = "b18a20261024"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("users", "assignments"):
        op.add_column(
            table, sa.Column("auth_epoch", sa.Integer(), nullable=False, server_default="0")
        )
        op.alter_column(table, "auth_epoch", server_default=None)
    op.create_table(
        "auth_role_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("from_role", sa.String(20), nullable=False),
        sa.Column("to_role", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_uid", sa.Integer(), nullable=False),
    )
    op.create_index("ix_auth_role_changes_user_id", "auth_role_changes", ["user_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM auth_role_changes)")).scalar():
        raise RuntimeError("Staff role audit records must be preserved")
    op.drop_index("ix_auth_role_changes_user_id", table_name="auth_role_changes")
    op.drop_table("auth_role_changes")
    op.drop_column("assignments", "auth_epoch")
    op.drop_column("users", "auth_epoch")
