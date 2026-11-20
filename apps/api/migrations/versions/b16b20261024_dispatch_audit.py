"""Preserve explicit cancellation and snapshot capture time, including empty proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b16b20261024"
down_revision = "b16a20261024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dispatch_plans",
        sa.Column("cancellation_data", JSONB(), nullable=False, server_default="{}"),
    )
    op.alter_column("dispatch_plans", "cancellation_data", server_default=None)
    op.add_column("dispatch_plans", sa.Column("captured_at", sa.DateTime(timezone=True)))
    # No old proposal was approved under this new contract; expire legacy captures safely.
    op.execute(
        "UPDATE dispatch_plans SET captured_at=created_at, expires_at=LEAST(expires_at,now())"
    )
    op.alter_column("dispatch_plans", "captured_at", nullable=False)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM dispatch_plans)")).scalar():
        raise RuntimeError("Dispatch audit records must be preserved before downgrade")
    op.drop_column("dispatch_plans", "captured_at")
    op.drop_column("dispatch_plans", "cancellation_data")
