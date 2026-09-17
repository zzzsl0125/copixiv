"""novel.source / novel.source_key — 「我的上传」（本地 TXT）的来源列

Revision ID: 0005_novel_source
Revises: 0004_tag_refs_linear
Create Date: 2026-09-20 00:00:00.000000

``docs/LIBRARY_DESIGN.md`` §2 verbatim::

    ALTER TABLE novel ADD COLUMN source     TEXT NOT NULL DEFAULT 'pixiv';
    ALTER TABLE novel ADD COLUMN source_key TEXT;
    CREATE INDEX        ix_novel_source     ON novel (source);
    CREATE UNIQUE INDEX ux_novel_source_key ON novel (source, source_key)
        WHERE source_key IS NOT NULL;

Why a column and not a tag: tags are user assets — ``tag_alias`` +
``apply_alias_retroactively`` can merge 「本地」 into another tag and rewrite
the whole library, which would empty the shelf.  The source is a structural
fact and belongs to the row.

**Zero backfill**: the 240 k existing rows are Pixiv rows, so the column
default ``'pixiv'`` is the correct value for all of them (PostgreSQL 11+
stores the default in the catalogue — no table rewrite, no UPDATE).

``source_key`` is the fingerprint of a local book's normalised body (sha256)
— both the dedup key and the future merge anchor.  Pixiv rows keep NULL,
which is why the unique index is **partial** (``WHERE source_key IS NOT
NULL``): without the predicate, all NULL-bearing Pixiv rows would collide…
they would not (NULLs are distinct in a unique index), but the partial form
keeps the index small — only local rows carry a key.

The third object is ``novel_local_id_seq``, the local-book id allocator
(§3.1: ``id = -(10**9) + n``, n from 1, newer → larger).  ``START WITH
-999999999`` is exactly ``n = 1``; ``INCREMENT BY 1`` keeps the ordering the
shelf's 「时间」 button relies on (``order by Novel.id``).  ``MINVALUE`` must
be spelled out — PostgreSQL defaults an ascending sequence's minimum to 1,
so the bare ``START WITH -999999999`` from the request is rejected
(``START value (-999999999) cannot be less than MINVALUE (1)``).

Why a sequence and not ``SELECT max(id)``: ids are user-visible (the DSL
``id:`` filter, the export template's ``{id}``), so an id must never be
reused.  ``max(id)`` falls back after the newest local row is deleted, which
would silently point every old reference at a different book; a sequence
never goes backwards (``nextval`` is non-transactional, so even a rolled-back
upload consumes its number — gaps are the price of never repeating).

``setval`` re-anchors the sequence for the downgrade → upgrade path: the
downgrade deliberately keeps local rows, so a plain ``START WITH`` would hand
the next upload an id that already exists (primary-key collision).  The
re-anchor is one backwards index scan (``WHERE id < 0``) and a no-op on an
empty database.

Downgrade drops the two indexes, the two columns and the sequence.  It
deliberately does **not** delete the local rows: dropping the columns makes
them indistinguishable from Pixiv rows (negative id, files under
``download/local/``), which is a data decision, not a schema rollback —
SQLite-era restores are handled by ``pg_dump``, not by this migration.
"""

from typing import Union, Sequence

from alembic import op

revision: str = "0005_novel_source"
down_revision: Union[str, Sequence[str], None] = "0004_tag_refs_linear"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE novel ADD COLUMN source TEXT NOT NULL DEFAULT 'pixiv'"
    )
    op.execute("ALTER TABLE novel ADD COLUMN source_key TEXT")
    op.execute("CREATE INDEX ix_novel_source ON novel (source)")
    op.execute(
        "CREATE UNIQUE INDEX ux_novel_source_key ON novel (source, source_key) "
        "WHERE source_key IS NOT NULL"
    )
    # Local-book id allocator (§3.1).  ``MINVALUE`` is mandatory: PostgreSQL
    # defaults an ascending sequence's minimum to 1, so ``START WITH
    # -999999999`` alone is rejected ("START value (-999999999) cannot be less
    # than MINVALUE (1)").  ``setval`` below re-anchors the cursor so that a
    # downgrade → upgrade cycle cannot hand out an id that a surviving local
    # row already holds.
    op.execute(
        "CREATE SEQUENCE novel_local_id_seq "
        "MINVALUE -999999999 START WITH -999999999 INCREMENT BY 1"
    )
    # No local rows → park the cursor on the anchor (is_called=false, so the
    # first ``nextval`` IS -999999999 = -(10**9) + 1); otherwise continue from
    # the largest existing local id.
    op.execute(
        """
        SELECT setval('novel_local_id_seq', COALESCE(v, -999999999), v IS NOT NULL)
        FROM (SELECT max(id) AS v FROM novel WHERE id < 0) AS anchor
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_novel_source_key")
    op.execute("DROP INDEX IF EXISTS ix_novel_source")
    op.execute("ALTER TABLE novel DROP COLUMN IF EXISTS source_key")
    op.execute("ALTER TABLE novel DROP COLUMN IF EXISTS source")
    op.execute("DROP SEQUENCE IF EXISTS novel_local_id_seq")
