"""Constrain dataset digest and bind active identity to its activation audit."""

from alembic import op

revision = "b16b20261028"
down_revision = "b16a20261028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_check_constraint(
        "planning_dataset_activation_sha256",
        "planning_dataset_activations",
        "sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "planning_dataset_active_sha256", "planning_dataset_active", "sha256 ~ '^[0-9a-f]{64}$'"
    )
    columns = ["namespace", "revision", "sha256", "dataset_id", "version"]
    op.create_unique_constraint(
        "planning_dataset_activation_identity", "planning_dataset_activations", columns
    )
    op.create_foreign_key(
        "planning_dataset_active_activation_fk",
        "planning_dataset_active",
        "planning_dataset_activations",
        columns,
        columns,
    )


def downgrade():
    op.drop_constraint(
        "planning_dataset_active_activation_fk", "planning_dataset_active", type_="foreignkey"
    )
    op.drop_constraint(
        "planning_dataset_activation_identity", "planning_dataset_activations", type_="unique"
    )
    op.drop_constraint("planning_dataset_active_sha256", "planning_dataset_active", type_="check")
    op.drop_constraint(
        "planning_dataset_activation_sha256", "planning_dataset_activations", type_="check"
    )
