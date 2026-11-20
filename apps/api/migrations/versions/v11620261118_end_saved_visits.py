"""Release active visits that already have a saved, unconfirmed worker report."""

from alembic import op

revision = "v11620261118"
down_revision = "v11520261116"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        WITH saved AS (
            SELECT DISTINCT ON (a.id) a.id, a.ticket_id, a.worker_id, r.created_at,
                   t.location_data -> 'selected' ->> 'code' AS location_code
            FROM assignments a
            JOIN tickets t ON t.id = a.ticket_id
            JOIN users u ON u.id = a.worker_id
            JOIN completion_reports r ON r.assignment_id = a.id AND r.ticket_id = t.id
            WHERE t.status = 'in_progress' AND u.role = 'worker' AND u.auth_epoch = a.auth_epoch
              AND NOT a.revoked AND a.stopped_at IS NULL AND a.visit_ended_at IS NULL
              AND r.created_at >= COALESCE(a.started_at, a.created_at)
              AND NOT r.reviewed AND r.worker_confirmed_revision IS NULL
              AND r.supersedes_report_id IS NULL AND r.superseded_at IS NULL
              AND r.analysis_status <> 'dismissed'
            ORDER BY a.id, r.created_at, r.id
        ), ended AS (
            UPDATE assignments a
            SET visit_ended_at = s.created_at, execution_revision = a.execution_revision + 1,
                execution_location_code = COALESCE(a.execution_location_code, s.location_code),
                remaining_data = '{}'::jsonb
            FROM saved s WHERE a.id = s.id
            RETURNING s.ticket_id, s.worker_id, s.created_at
        )
        INSERT INTO ticket_events
            (id, ticket_id, actor_id, event_type, reason, created_at)
        SELECT gen_random_uuid(), ticket_id, worker_id, 'visit_ended',
               '저장된 작업 보고로 현장 방문을 종료했어요', created_at
        FROM ended
        """)


def downgrade():
    # A later visit can depend on this ended visit. Keep the execution and audit history.
    pass
