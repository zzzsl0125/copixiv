"""Simplify task_history: normalize result, convert arguments to JSONB, drop ix_task_history_active_func.

Revision ID: 0002_simplify_task_history
Revises: 0001_baseline
Create Date: 2026-11-21 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_simplify_task_history"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE task_history
        SET result = CASE
            WHEN left(btrim(result #>> '{}'), 1) = '{' THEN (result #>> '{}')::jsonb
            ELSE jsonb_build_object('summary', result #>> '{}')
        END
        WHERE result IS NOT NULL AND jsonb_typeof(result) <> 'object'
        """
    )
    op.execute(
        """
        ALTER TABLE task_history
        ALTER COLUMN arguments TYPE JSONB
        USING CASE
            WHEN arguments IS NULL OR btrim(arguments) = '' THEN NULL
            ELSE arguments::jsonb
        END
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_task_history_active_func")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX ix_task_history_active_func ON task_history (task_func) "
        "WHERE status IN ('pending', 'running')"
    )
    op.execute(
        """
        ALTER TABLE task_history
        ALTER COLUMN arguments TYPE TEXT
        USING CASE
            WHEN arguments IS NULL THEN NULL
            ELSE arguments::text
        END
        """
    )
