"""Record explicit isolated storage provisioning intent."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b19a20261028"
down_revision = "b18f20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_identity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("storage_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("operation_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("namespace", sa.String(60), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("device", sa.BigInteger(), nullable=False),
        sa.Column("inode", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="storage_identity_singleton"),
        sa.CheckConstraint("state IN ('pending','active')", name="storage_identity_state"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM storage_identity)")).scalar():
        raise RuntimeError("storage_identity_exists")
    op.drop_table("storage_identity")
