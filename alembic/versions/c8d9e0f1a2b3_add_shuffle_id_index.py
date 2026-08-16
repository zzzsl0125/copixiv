"""add_shuffle_id_index

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-16 13:18:00.000000

Add composite index ix_novel_shuffle_id on novel(shuffle, id) so
ORDER BY shuffle, id (random browse load-more cursor) is served straight
from the index — the id tiebreak no longer needs a TEMP B-TREE
("USE TEMP B-TREE FOR RIGHT PART OF ORDER BY").

NOTE: this revision was applied to the production database before its
source file was briefly deleted; it has been restored because Alembic
revisions that exist in the database must never disappear from the code.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the shuffle + id index."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_novel_shuffle_id "
        "ON novel (shuffle, id)"
    )


def downgrade() -> None:
    """Drop the shuffle + id index."""
    op.execute("DROP INDEX IF EXISTS ix_novel_shuffle_id")
