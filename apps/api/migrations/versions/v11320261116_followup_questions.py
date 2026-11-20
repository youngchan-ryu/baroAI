"""Persist one AI follow-up bundle and its bounded untrusted evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v11320261116"
down_revision = "v11220261116"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tickets", sa.Column("ai_followup_data", postgresql.JSONB(), nullable=True))
    op.add_column(
        "tickets",
        sa.Column(
            "followup_analysis_evidence",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column("tickets", "followup_analysis_evidence")
    op.drop_column("tickets", "ai_followup_data")
