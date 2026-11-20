"""Staff display name audit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c30a20261028"
down_revision = "b18d20261025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "staff_name_changes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("before_name", sa.String(100), nullable=False),
        sa.Column("after_name", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_uid", sa.Integer(), nullable=False),
    )
    op.create_index("ix_staff_name_changes_user_id", "staff_name_changes", ["user_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM staff_name_changes)")).scalar():
        raise RuntimeError("staff_name_audit_exists")
    op.drop_index("ix_staff_name_changes_user_id", table_name="staff_name_changes")
    op.drop_table("staff_name_changes")
