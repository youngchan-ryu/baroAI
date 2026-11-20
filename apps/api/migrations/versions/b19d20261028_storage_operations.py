"""Record managed recovery operations without adopting existing objects."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "b19d20261028"
down_revision = "b19c20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_operations",
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
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_role", sa.String(20), nullable=False),
        sa.Column("auth_epoch", sa.Integer(), nullable=False),
        sa.Column("assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignments.id")),
        sa.Column("upload_id", UUID(as_uuid=True), sa.ForeignKey("uploads.id")),
        sa.Column("current_variant_id", UUID(as_uuid=True), sa.ForeignKey("image_variants.id")),
        sa.Column("previous_variant_id", UUID(as_uuid=True), sa.ForeignKey("image_variants.id")),
        sa.Column("last_variant_id", UUID(as_uuid=True), sa.ForeignKey("image_variants.id")),
        sa.Column("previous_sha256", sa.String(64)),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("purpose", sa.String(10), nullable=False),
        sa.Column("scope", sa.String(160), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("mask_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_data", JSONB),
        sa.UniqueConstraint("scope", "key_hash", name="storage_operation_scope_key"),
        sa.CheckConstraint("kind IN ('upload','reprocess')", name="storage_operation_kind"),
        sa.CheckConstraint(
            "state IN ('active','published','abandoned')", name="storage_operation_state"
        ),
        sa.CheckConstraint("purpose IN ('ticket','report')", name="storage_operation_purpose"),
    )
    op.add_column("storage_objects", sa.Column("operation_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "storage_objects_operation_id_fkey",
        "storage_objects",
        "storage_operations",
        ["operation_id"],
        ["id"],
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM storage_operations)")).scalar():
        raise RuntimeError("storage_operation_evidence_exists")
    op.drop_constraint("storage_objects_operation_id_fkey", "storage_objects", type_="foreignkey")
    op.drop_column("storage_objects", "operation_id")
    op.drop_table("storage_operations")
