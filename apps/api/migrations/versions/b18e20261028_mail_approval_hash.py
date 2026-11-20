"""Preserve an approval fingerprint for newly created mail intents.

Old jobs cannot prove which snapshot was approved when they were created.
Their null fingerprint deliberately prevents sending; explicit recovery creates a new intent.
"""

import sqlalchemy as sa
from alembic import op

revision = "b18e20261028"
down_revision = "c30a20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mail_jobs", sa.Column("approval_hash", sa.String(64), nullable=True))


def downgrade():
    if op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM mail_jobs WHERE approval_hash IS NOT NULL)")
    ).scalar():
        raise RuntimeError("mail_approval_evidence_exists")
    op.drop_column("mail_jobs", "approval_hash")
