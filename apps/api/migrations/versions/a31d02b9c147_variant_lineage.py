"""Use explicit successful image lineage rather than transaction timestamps."""

import sqlalchemy as sa
from alembic import op

revision = "a31d02b9c147"
down_revision = "9f76e4514b88"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("uploads", sa.Column("last_successful_variant_id", sa.UUID(), nullable=True))
    # Only current ready is authoritative. Never infer a historical failed lineage by time.
    op.execute("""
        UPDATE uploads AS u SET last_successful_variant_id = v.id
        FROM image_variants AS v
        WHERE u.current_variant_id = v.id AND v.upload_id = u.id AND v.status = 'ready'
    """)


def downgrade():
    op.drop_column("uploads", "last_successful_variant_id")
