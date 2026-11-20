"""Explicit stopped visits are not resolved tickets or inferred destination arrivals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b17b20261024"
down_revision = "b17a20261024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assignments", sa.Column("stopped_at", sa.DateTime(timezone=True)))
    op.add_column(
        "assignments", sa.Column("stopped_by", UUID(as_uuid=True), sa.ForeignKey("users.id"))
    )
    op.add_column("assignments", sa.Column("stop_reason", sa.Text()))


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM assignments WHERE stopped_at IS NOT NULL)"))
        .scalar()
    ):
        raise RuntimeError("Stopped visit audit must be preserved before downgrade")
    for column in ("stop_reason", "stopped_by", "stopped_at"):
        op.drop_column("assignments", column)
