"""Persist managed staging inode before writing bytes."""

import sqlalchemy as sa
from alembic import op

revision = "b19c20261028"
down_revision = "b19b20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("storage_objects", sa.Column("stage_device", sa.BigInteger(), nullable=True))
    op.add_column("storage_objects", sa.Column("stage_inode", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "storage_object_stage_identity",
        "storage_objects",
        "(stage_device IS NULL AND stage_inode IS NULL) OR "
        "(stage_device IS NOT NULL AND stage_inode IS NOT NULL AND stage_inode > 0)",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM storage_objects WHERE stage_inode IS NOT NULL)")
        )
        .scalar()
    ):
        raise RuntimeError("storage_stage_evidence_exists")
    op.drop_constraint("storage_object_stage_identity", "storage_objects", type_="check")
    op.drop_column("storage_objects", "stage_inode")
    op.drop_column("storage_objects", "stage_device")
