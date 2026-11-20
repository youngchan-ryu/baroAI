"""Add immutable school catalog, datasets, profiles, and historical snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v10620261103"
down_revision = "v10320261103"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    for table in ("tickets", "assignments"):
        op.add_column(table, sa.Column("catalog_revision", sa.BigInteger(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "catalog_snapshot", jsonb, nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
        )
    op.execute(
        "UPDATE tickets SET catalog_snapshot = jsonb_build_object("
        "'category_code', category_code, 'location_text', location_text, 'legacy', true)"
    )
    op.execute(
        "UPDATE assignments a SET catalog_snapshot = jsonb_build_object("
        "'category_code', t.category_code, 'location_text', t.location_text, "
        "'worker_name', u.display_name, 'legacy', true) FROM tickets t, users u "
        "WHERE a.ticket_id = t.id AND a.worker_id = u.id"
    )
    for table in ("tickets", "assignments"):
        op.alter_column(table, "catalog_snapshot", server_default=None)
    op.create_table(
        "school_catalog_imports",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("actor_id", uuid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("errors", jsonb, nullable=False),
        sa.Column("changes", jsonb, nullable=False),
        sa.Column("candidate", jsonb, nullable=False),
    )
    op.create_table(
        "school_planning_datasets",
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("canonical_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(100), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("evidence", jsonb, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "school_catalog_versions",
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("import_id", uuid, sa.ForeignKey("school_catalog_imports.id"), nullable=False),
        sa.Column("actor_id", uuid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("content", jsonb, nullable=False),
        sa.Column("dataset_revision", sa.BigInteger(), nullable=True),
        sa.Column("dataset_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "school_catalog_active",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "catalog_revision",
            sa.BigInteger(),
            sa.ForeignKey("school_catalog_versions.revision"),
            nullable=False,
        ),
        sa.Column(
            "dataset_revision",
            sa.BigInteger(),
            sa.ForeignKey("school_planning_datasets.revision"),
            nullable=True,
        ),
        sa.Column("dataset_hash", sa.String(64), nullable=True),
        sa.CheckConstraint("id = 1", name="school_catalog_active_singleton"),
    )
    op.create_table(
        "worker_profiles",
        sa.Column("worker_id", uuid, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("department_code", sa.String(30), nullable=True),
        sa.Column("field_codes", jsonb, nullable=False),
        sa.Column("catalog_revision", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "worker_profile_changes",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("worker_id", uuid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_id", uuid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before", jsonb, nullable=False),
        sa.Column("after", jsonb, nullable=False),
    )


def downgrade():
    for table in (
        "worker_profile_changes",
        "worker_profiles",
        "school_catalog_active",
        "school_catalog_versions",
        "school_planning_datasets",
        "school_catalog_imports",
    ):
        op.drop_table(table)
    for table in ("assignments", "tickets"):
        op.drop_column(table, "catalog_snapshot")
        op.drop_column(table, "catalog_revision")
