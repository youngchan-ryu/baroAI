"""Encrypted neutral CSV snapshots and separate download audit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b18d20261025"
down_revision = "b18c20261025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "export_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("creator_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("namespace", sa.String(50), nullable=False),
        sa.Column("request_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(30), nullable=False),
        sa.Column("encrypted_snapshot", sa.Text()),
        sa.Column("encrypted_audit", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("creator_id", "namespace", "request_key", name="export_request_key"),
        sa.CheckConstraint("row_count BETWEEN 1 AND 50", name="export_row_limit"),
    )
    op.create_table(
        "export_downloads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "export_id",
            UUID(as_uuid=True),
            sa.ForeignKey("export_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM export_records)")).scalar():
        raise RuntimeError("CSV generation audit must be preserved before downgrade")
    op.drop_table("export_downloads")
    op.drop_table("export_records")
