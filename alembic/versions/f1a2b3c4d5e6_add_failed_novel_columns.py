"""add_failed_novel_columns

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-08-19 21:00:00.000000

Add title / last_failed_at to failed_novel so the failure ledger can be
rendered as a first-class "download failures" management view (sidebar
entry): users need a human-readable title and a failure time to decide
what to do with each record.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pragmatic try/pass like the existing SQLite migrations: the columns
    # may already exist (model-driven creation on a fresh install).
    try:
        op.execute("ALTER TABLE failed_novel ADD COLUMN title TEXT")
    except Exception:
        pass
    try:
        op.execute("ALTER TABLE failed_novel ADD COLUMN last_failed_at TEXT")
    except Exception:
        pass
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_failed_novel_last_failed_at "
        "ON failed_novel (last_failed_at)"
    )


def downgrade() -> None:
    # SQLite has no DROP COLUMN before 3.35 with batch mode; pragmatic no-op,
    # matching the existing migrations' approach.
    pass
