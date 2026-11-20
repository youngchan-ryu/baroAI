"""Store optionally shared photo coordinates separately from public photo DTOs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v11520261116"
down_revision = "v11420261116"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("uploads", sa.Column("photo_coordinates", postgresql.JSONB(), nullable=True))
    op.create_index("ix_uploads_photo_coordinates_expiry", "uploads", ["delete_after"],
                    postgresql_where=sa.text("photo_coordinates IS NOT NULL"))


def downgrade():
    op.drop_index("ix_uploads_photo_coordinates_expiry", table_name="uploads")
    op.drop_column("uploads", "photo_coordinates")
