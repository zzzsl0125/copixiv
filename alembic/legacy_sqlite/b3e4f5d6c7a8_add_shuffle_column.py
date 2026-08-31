"""add_shuffle_column

Revision ID: b3e4f5d6c7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-06 12:00:00.000000

Add shuffle column to novel table for fast random-order keyset pagination.
The column holds a precomputed random integer (0 .. 2^31-1) so that
random ordering can use a covering index seek instead of computing a
hash expression over all rows.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'b3e4f5d6c7a8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Column may already exist (e.g. from model-driven table creation).
    # Use a pragmatic approach: try to add, pass if it's already there.
    try:
        op.execute("ALTER TABLE novel ADD COLUMN shuffle INTEGER DEFAULT 0")
    except Exception:
        pass  # column already exists — safe to continue
    # Assign random values to any rows that still have the default 0
    # (SQLite's random() returns -2^63..2^63, abs() keeps us in range).
    op.execute(
        "UPDATE novel SET shuffle = abs(random()) % 2147483647 WHERE shuffle = 0"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_novel_shuffle ON novel (shuffle)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_novel_shuffle")
    # SQLite doesn't support DROP COLUMN directly, but Alembic handles it
    # via batch mode if configured.  For pragmatism this is left as a no-op —
    # downgrading past this revision requires recreating the table.
    pass
