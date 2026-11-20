"""Add operation publication evidence, accepting the earlier b19d variant safely."""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b19e20261028"
down_revision = "b19d20261028"
branch_labels = None
depends_on = None

CONSTRAINT = "storage_operation_publication"
FOREIGN_KEY = "storage_operations_published_upload_id_fkey"
EXPRESSION = (
    "(state = 'published' AND published_upload_id IS NOT NULL) OR "
    "(state != 'published' AND published_upload_id IS NULL)"
)


def _normal(expression):
    expression = expression.lower().replace("::text", "").replace("<>", "!=")
    return re.sub(r"[\s()]", "", expression)


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns("storage_operations")}
    column = columns.get("published_upload_id")
    if column is None:
        op.add_column("storage_operations", sa.Column("published_upload_id", UUID(as_uuid=True)))
        op.create_foreign_key(
            FOREIGN_KEY, "storage_operations", "uploads", ["published_upload_id"], ["id"]
        )
    else:
        if not isinstance(column["type"], UUID) or not column["nullable"]:
            raise RuntimeError("storage_operation_publication_column_mismatch")
        keys = inspector.get_foreign_keys("storage_operations")
        if not any(
            key["name"] == FOREIGN_KEY
            and key["constrained_columns"] == ["published_upload_id"]
            and key["referred_table"] == "uploads"
            and key["referred_columns"] == ["id"]
            for key in keys
        ):
            raise RuntimeError("storage_operation_publication_foreign_key_mismatch")
    # Inspector summaries omit validation status, and an unqualified target
    # table name can resolve to a different schema on the search path.
    fk_safe = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
            "JOIN pg_class source ON source.oid = c.conrelid "
            "JOIN pg_class target ON target.oid = c.confrelid "
            "WHERE c.conrelid = to_regclass('storage_operations') "
            "AND c.conname = :name AND c.contype = 'f' "
            "AND c.confrelid = to_regclass('uploads') "
            "AND source.relnamespace = target.relnamespace "
            "AND c.confdeltype = 'a' AND c.confupdtype = 'a' "
            "AND c.confmatchtype = 's' AND NOT c.condeferrable "
            "AND NOT c.condeferred AND c.convalidated)"
        ),
        {"name": FOREIGN_KEY},
    ).scalar()
    if not fk_safe:
        raise RuntimeError("storage_operation_publication_foreign_key_mismatch")
    inspector = sa.inspect(connection)
    checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("storage_operations")
    }
    if CONSTRAINT in checks:
        valid = connection.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conrelid = to_regclass('storage_operations') "
                "AND conname = :name AND contype = 'c' AND convalidated)"
            ),
            {"name": CONSTRAINT},
        ).scalar()
        if not valid or _normal(checks[CONSTRAINT]) != _normal(EXPRESSION):
            raise RuntimeError("storage_operation_publication_constraint_mismatch")
    else:
        op.create_check_constraint(CONSTRAINT, "storage_operations", EXPRESSION)


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM storage_operations "
                "WHERE published_upload_id IS NOT NULL OR state = 'published')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("storage_operation_publication_evidence_exists")
    op.drop_constraint(CONSTRAINT, "storage_operations", type_="check")
    op.drop_constraint(FOREIGN_KEY, "storage_operations", type_="foreignkey")
    op.drop_column("storage_operations", "published_upload_id")
