"""Store only unconfirmed photo location candidate codes and catalog revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v11420261116"
down_revision = "v11320261116"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("uploads", sa.Column("location_signal", postgresql.JSONB(), nullable=True))

def downgrade():
    op.drop_column("uploads", "location_signal")
