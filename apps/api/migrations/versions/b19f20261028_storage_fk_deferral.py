"""Allow explicit deferred remapping of storage identity in isolated recovery."""

import sqlalchemy as sa
from alembic import op

revision = "b19f20261028"
down_revision = "b16b20261028"
branch_labels = None
depends_on = None

TABLES = ("storage_objects", "storage_operations")


def _change(was_deferrable, target):
    connection = op.get_bind()
    context = op.get_context()
    schema_valid = connection.execute(
        sa.text(
            "SELECT current_schema() = 'public' "
            "AND to_regclass('alembic_version') = to_regclass('public.alembic_version')"
        )
    ).scalar()
    if context.version_table_schema not in (None, "public") or not schema_valid:
        raise RuntimeError("storage_identity_migration_schema_mismatch")
    for table in TABLES:
        name = table + "_storage_id_fkey"
        valid = connection.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
                "JOIN pg_class src ON src.oid=c.conrelid "
                "JOIN pg_class dst ON dst.oid=c.confrelid "
                "WHERE c.conrelid=to_regclass(:source) AND c.conname=:name "
                "AND c.contype='f' AND c.confrelid=to_regclass('public.storage_identity') "
                "AND src.relnamespace=dst.relnamespace "
                "AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute "
                "WHERE attrelid=src.oid AND attname='storage_id')]::smallint[] "
                "AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute "
                "WHERE attrelid=dst.oid AND attname='storage_id')]::smallint[] "
                "AND c.confdeltype='a' AND c.confupdtype='a' AND c.confmatchtype='s' "
                "AND c.convalidated AND c.condeferrable=:deferrable AND NOT c.condeferred)"
            ),
            {"source": "public." + table, "name": name, "deferrable": was_deferrable},
        ).scalar()
        if not valid:
            raise RuntimeError("storage_identity_fk_mismatch")
    for table in TABLES:
        name = table + "_storage_id_fkey"
        op.execute(f'ALTER TABLE public."{table}" ALTER CONSTRAINT "{name}" {target}')


def upgrade():
    _change(False, "DEFERRABLE INITIALLY IMMEDIATE")


def downgrade():
    _change(True, "NOT DEFERRABLE")
