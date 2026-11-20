"""Preserve the single recovery child of each mail intent."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b18f20261028"
down_revision = "b18e20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mail_jobs", sa.Column("recovery_parent_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_mail_recovery_parent",
        "mail_jobs",
        "mail_jobs",
        ["recovery_parent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("mail_recovery_parent", "mail_jobs", ["recovery_parent_id"])


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM mail_jobs WHERE recovery_parent_id IS NOT NULL)")
        )
        .scalar()
    ):
        raise RuntimeError("mail_recovery_evidence_exists")
    op.drop_constraint("mail_recovery_parent", "mail_jobs", type_="unique")
    op.drop_constraint("fk_mail_recovery_parent", "mail_jobs", type_="foreignkey")
    op.drop_column("mail_jobs", "recovery_parent_id")
