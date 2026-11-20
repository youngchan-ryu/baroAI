"""Immutable report confirmation and approved per-report screen replies."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b13b20261023"
down_revision = "b13a20261023"
branch_labels = None
depends_on = None


def upgrade():
    for name, default in (("revision", "1"),):
        op.add_column(
            "completion_reports",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
    op.add_column(
        "completion_reports",
        sa.Column(
            "analysis_status", sa.String(30), nullable=False, server_default="worker_confirmed"
        ),
    )
    for name in ("draft_data", "confirmed_data", "approved_snapshot"):
        op.add_column(
            "completion_reports",
            sa.Column(name, pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )
    op.add_column("completion_reports", sa.Column("worker_confirmed_revision", sa.Integer()))
    op.add_column("completion_reports", sa.Column("confirmed_result", sa.String(20)))
    op.add_column("completion_reports", sa.Column("reply_text", sa.Text()))
    op.add_column(
        "completion_reports", sa.Column("approved_by", pg.UUID(), sa.ForeignKey("users.id"))
    )
    op.add_column("completion_reports", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column(
        "completion_reports",
        sa.Column("legacy_migrated", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.get_bind().exec_driver_sql("""UPDATE completion_reports
        SET worker_confirmed_revision=1, confirmed_result=result,
        confirmed_data='{"actions":[],"materials_used":[],"residual_issue":"","revisit_needed":false,"photo_comparison":"not_performed","source":"legacy"}'::jsonb""")
    op.execute("""UPDATE completion_reports SET approved_snapshot=jsonb_build_object(
        'revision',1,'confirmed_result',result,'legacy_migrated',true), reply_text=CASE result
        WHEN 'completed' THEN '담당자가 작업 완료 보고를 확인했어요.'
        WHEN 'temporary' THEN '임시 조치 보고를 확인했어요. 추가 확인이 필요해요.'
        WHEN 'revisit' THEN '재방문 필요 보고를 확인했어요. 방문 일정은 정해지지 않았어요.'
        ELSE '작업 보고를 확인했어요. 해결 여부는 추가 확인이 필요해요.' END WHERE reviewed=true""")
    for name in (
        "revision",
        "analysis_status",
        "draft_data",
        "confirmed_data",
        "approved_snapshot",
        "legacy_migrated",
    ):
        op.alter_column("completion_reports", name, server_default=None)
    op.add_column(
        "processing_jobs",
        sa.Column(
            "report_id", pg.UUID(), sa.ForeignKey("completion_reports.id", ondelete="CASCADE")
        ),
    )
    op.create_index("ix_processing_jobs_report_id", "processing_jobs", ["report_id"])
    op.drop_constraint("processing_job_revision", "processing_jobs", type_="unique")
    op.create_index(
        "processing_ticket_revision",
        "processing_jobs",
        ["ticket_id", "kind", "input_revision"],
        unique=True,
        postgresql_where=sa.text("report_id IS NULL"),
    )
    op.create_index(
        "processing_report_revision",
        "processing_jobs",
        ["report_id", "kind", "input_revision"],
        unique=True,
        postgresql_where=sa.text("report_id IS NOT NULL"),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM processing_jobs WHERE report_id IS NOT NULL)")
    ):
        raise RuntimeError("rollback_requires_preserving_report_jobs")
    op.drop_index("processing_report_revision", "processing_jobs")
    op.drop_index("processing_ticket_revision", "processing_jobs")
    op.create_unique_constraint(
        "processing_job_revision", "processing_jobs", ["ticket_id", "kind", "input_revision"]
    )
    op.drop_index("ix_processing_jobs_report_id", "processing_jobs")
    op.drop_column("processing_jobs", "report_id")
    for name in (
        "revision",
        "analysis_status",
        "draft_data",
        "confirmed_data",
        "approved_snapshot",
        "worker_confirmed_revision",
        "confirmed_result",
        "reply_text",
        "approved_by",
        "approved_at",
        "legacy_migrated",
    ):
        op.drop_column("completion_reports", name)
