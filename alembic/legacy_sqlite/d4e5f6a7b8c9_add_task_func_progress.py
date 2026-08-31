"""add_task_func_progress

Revision ID: d4e5f6a7b8c9
Revises: a51c9e7d2b4f
Create Date: 2026-08-27 13:00:00.000000

Add ``task_func`` and ``progress`` to ``task_history`` and a partial unique
index that enforces the task dedup guard at the DB layer.

``task_func`` holds the *registered function name* (``scheduled_tasks.task``
/ ``TaskSpec.name``) while ``name`` keeps the *display name* (the scheduled
row's UI label or the manual function name).  The unique index
``ux_task_history_running`` only covers rows still pending/running, so:

* two pending/running history rows for the same underlying function are
  rejected (``sqlalchemy.exc.IntegrityError`` → ``TaskAlreadyRunningError``),
  even when they carry different display names; and
* a completed row ceases to constrain re-enqueueing the same function.

This replaces the two in-process/query guards (``_running_names`` +
``has_pending_or_running``) with a single DB constraint (K1).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'a51c9e7d2b4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pragmatic try/pass like the existing SQLite migrations: the columns
    # may already exist (model-driven creation on a fresh install).  Old
    # rows keep NULL in both new columns.
    try:
        op.execute("ALTER TABLE task_history ADD COLUMN task_func TEXT")
    except Exception:
        pass
    try:
        op.execute("ALTER TABLE task_history ADD COLUMN progress TEXT")
    except Exception:
        pass
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_history_running "
        "ON task_history (task_func) "
        "WHERE status IN ('pending', 'running')"
    )


def downgrade() -> None:
    # Drop the constraint.  The two added columns are left in place: SQLite
    # has no DROP COLUMN before 3.35 with batch mode, so reverting the
    # additive columns is a pragmatic no-op (matching the existing
    # migrations' approach).
    op.execute("DROP INDEX IF EXISTS ux_task_history_running")
