"""rebuild novel_fts as char-gram

Revision ID: 69cbf0dfa207
Revises: d4e5f6a7b8c9
Create Date: 2026-08-28 21:05:42.850235

Replace the ``novel_fts`` FTS5 virtual table with the char-gram (character
unigram) definition: a standalone table (no ``content`` clause) whose four
columns match production and whose tokeniser is ``unicode61``.

``novel_fts`` is a DERIVED index — the rows it stores are produced by
:func:`copixiv.features.novels.fts.gram_tokenize` from the ``novel`` table,
so DROP + CREATE carries NO data risk.  The table is (re)filled by the
startup self-heal in ``app._rebuild_fts_if_needed`` (via
``FTSManager.needs_rebuild()``) or by the ``rebuild_fts`` maintenance task;
this migration only resets the schema so a legacy porter/jieba or
external-content table never survives an upgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '69cbf0dfa207'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rebuild ``novel_fts`` as a standalone char-gram FTS5 table."""
    # Derived index — drop the legacy shape (porter/jieba or
    # external-content) and create the char-gram standalone table.  Content
    # is populated by the startup self-heal rebuild or the rebuild_fts task.
    op.execute("DROP TABLE IF EXISTS novel_fts")
    op.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS novel_fts USING fts5(
            title, author_name, series_name, tags,
            tokenize='unicode61'
        )
    """)


def downgrade() -> None:
    """Drop the char-gram index (derived data — rebuilt on next startup)."""
    op.execute("DROP TABLE IF EXISTS novel_fts")
