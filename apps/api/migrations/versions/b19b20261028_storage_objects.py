"""Track managed photo object intents independently of business publication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b19b20261028"
down_revision = "b19a20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_objects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "storage_id",
            UUID(as_uuid=True),
            sa.ForeignKey("storage_identity.storage_id"),
            nullable=False,
        ),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("object_key", sa.String(100), nullable=False, unique=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','ready','deleting','deleted')", name="storage_object_state"
        ),
        sa.CheckConstraint("kind IN ('original','variant')", name="storage_object_kind"),
        sa.CheckConstraint("size >= 0", name="storage_object_size"),
    )
    op.create_index("ix_storage_objects_storage_id", "storage_objects", ["storage_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM storage_objects)")).scalar():
        raise RuntimeError("storage_object_evidence_exists")
    op.drop_index("ix_storage_objects_storage_id", table_name="storage_objects")
    op.drop_table("storage_objects")
