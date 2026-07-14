"""add_shuffle_like_text_index

Revision ID: 9ea7477570f8
Revises: b3e4f5d6c7a8
Create Date: 2026-07-14 11:00:00.000000

Replace ix_novel_shuffle with composite ix_novel_shuffle_like_text on
novel(shuffle, like, text).  The new index is a strict superset — it still
serves ORDER BY shuffle queries but additionally allows SQLite to evaluate
like/text filters directly from the index without main-table lookups (回表).

Before: SEARCH novel USING INDEX ix_novel_shuffle (shuffle>?)
        → table lookup for every candidate row to check like/text

After:  SEARCH novel USING COVERING INDEX ix_novel_shuffle_like_text (shuffle>?)
        → like/text evaluated from index columns directly
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = '9ea7477570f8'
down_revision: Union[str, Sequence[str], None] = 'b3e4f5d6c7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace ix_novel_shuffle with the composite superset index."""
    # Create the new composite index first, then drop the old one.
    # The new index covers shuffle + like + text, making the old
    # shuffle-only index redundant.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_novel_shuffle_like_text "
        "ON novel (shuffle, like, text)"
    )
    op.execute("DROP INDEX IF EXISTS ix_novel_shuffle")


def downgrade() -> None:
    """Restore the original shuffle-only index."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_novel_shuffle ON novel (shuffle)"
    )
    op.execute("DROP INDEX IF EXISTS ix_novel_shuffle_like_text")
