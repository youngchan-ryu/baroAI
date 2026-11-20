"""Add isolated deployment identity and private email OTP authentication."""

import sqlalchemy as sa
from alembic import op

revision = "b18a20261024"
down_revision = "b15a20261023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("verified_email", sa.String(254), nullable=True))
    op.create_unique_constraint("uq_users_verified_email", "users", ["verified_email"])
    op.create_table(
        "deployment_identity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("namespace", sa.String(60), nullable=False),
    )
    op.create_table(
        "email_challenges",
        sa.Column("address_key", sa.String(64), primary_key=True),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "auth_rate_limits",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM users WHERE verified_email IS NOT NULL) "
            "OR EXISTS(SELECT 1 FROM email_challenges)"
        )
    ).scalar():
        raise RuntimeError("Service authentication records must be preserved")
    op.drop_table("auth_rate_limits")
    op.drop_table("email_challenges")
    op.drop_table("deployment_identity")
    op.drop_constraint("uq_users_verified_email", "users", type_="unique")
    op.drop_column("users", "verified_email")
