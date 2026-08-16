r"""FTS5 full-text search manager — jieba-powered indexing.

Features:
- Idempotent rebuild with ``CREATE VIRTUAL TABLE IF NOT EXISTS``
- Incremental and batch FTS updates
- FTS health check via ``INSERT INTO ..._fts(..._fts) VALUES('rebuild')``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text as _text, select, delete as _delete, func, bindparam
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C

if TYPE_CHECKING:
    pass

from copixiv.app.logger import logger


class FTSManager:
    """Manages the novel_fts virtual table with jieba tokenisation."""

    def __init__(self, session: Session):
        self.session = session
        self._jieba = None

    @property
    def jieba(self):
        if self._jieba is None:
            import jieba
            self._jieba = jieba
        return self._jieba

    @staticmethod
    def warm_up() -> None:
        """Pre-load jieba's dict so the first keyword search isn't slow.

        jieba loads its 4.95 MB default dict and builds the prefix trie on
        the first ``cut()`` call, not on import (import alone is ~37 ms;
        the real ~700 ms cost lands on the first ``cut()``). Triggering it
        here shifts that cost to startup instead of the first user search.
        """
        try:
            import jieba
            list(jieba.cut("预热", HMM=True))
        except ImportError:
            pass

    # ------------------------------------------------------------------
    # Rebuild (idempotent)
    # ------------------------------------------------------------------

    # The FTS table is a *standalone* FTS5 table (no external-content
    # clause).  The index stores jieba-tokenised text that deliberately
    # differs from the raw ``novel`` rows, so a content-synchronised
    # table would make FTS5's 'delete' command unable to match rows.
    # Column set matches the table as it exists in production
    # (``title, author_name, series_name, tags``) so
    # ``CREATE ... IF NOT EXISTS`` never tries to recreate it with a
    # different shape.
    _CREATE_SQL = (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {C.TABLE_NOVEL_FTS} USING fts5("
        f"  {C.COL_TITLE}, {C.COL_AUTHOR_NAME}, {C.COL_SERIES_NAME}, tags,"
        f"  tokenize='porter unicode61'"
        f")"
    )

    def rebuild_novel_fts(self) -> None:
        """Create FTS5 virtual table if missing, then rebuild from novels.

        Idempotent: drops the virtual table first, then recreates it via
        ``CREATE VIRTUAL TABLE``, so it works regardless of whether the
        table already exists and what shape a legacy table has.
        """
        # DROP/CREATE are DDL and auto-commit in SQLite regardless; the
        # INSERTs below stay inside the caller's transaction.  Callers run
        # inside db_write() + uow.begin() (see the rebuild_fts task), which
        # commits on exit — FTSManager never commits itself.
        self.session.execute(
            _text(f"DROP TABLE IF EXISTS {C.TABLE_NOVEL_FTS}")
        )
        self.session.execute(_text(self._CREATE_SQL))
        self._batch_insert_all_novels()
        logger.info("FTS5 index rebuilt from scratch.")

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    def update_novel_fts_index(self, novel_ids: list[int]) -> None:
        """Update FTS entries for the given novel IDs.

        No-op if FTS table missing or *novel_ids* is empty.  Deletes
        the affected rowids (plain DELETE — the standalone novel_fts
        table has no external-content clause) then batch re-inserts
        all affected rows.
        """
        if not novel_ids:
            return

        if not self._fts_table_exists():
            return

        # Remove the existing entries first.  novel_fts is a standalone
        # FTS5 table, so a plain rowid DELETE is safe and exact (the
        # FTS5 'delete' command would require the exact tokenised
        # values stored in the index).  Without this, re-inserting the
        # same rowid below would fail with an IntegrityError.
        self.session.execute(
            _text(
                f"DELETE FROM {C.TABLE_NOVEL_FTS} WHERE rowid IN :nids"
            ).bindparams(bindparam("nids", expanding=True)),
            {"nids": novel_ids},
        )

        novels = self.session.execute(
            select(
                models.Novel.id,
                models.Novel.title,
                models.Novel.author_name,
                models.Novel.series_name,
                func.group_concat(models.Tag.name, " ").label("tags"),
            )
            .outerjoin(models.NovelTag, models.Novel.id == models.NovelTag.novel_id)
            .outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .where(models.Novel.id.in_(novel_ids))
            .group_by(models.Novel.id)
        ).all()

        if novels:
            self._batch_insert_fts_entries([
                (nid, title or "", author or "", series_name or "", tags or "")
                for nid, title, author, series_name, tags in novels
            ])

    def delete_novel_fts(self, novel_id: int) -> None:
        """Remove an FTS entry for a deleted novel.

        Plain rowid DELETE — safe on the standalone novel_fts table
        and a no-op when the row doesn't exist.  Skips silently when the
        FTS table itself is missing (unlike the sibling update path, this
        used to raise ``no such table`` and roll back the whole delete).
        """
        if not self._fts_table_exists():
            return
        self.session.execute(
            _text(f"DELETE FROM {C.TABLE_NOVEL_FTS} WHERE rowid = :id"),
            {"id": novel_id},
        )

    def delete_novel_fts_many(self, novel_ids: list[int]) -> None:
        """Remove FTS entries for many deleted novels in one statement.

        Same semantics as :meth:`delete_novel_fts` — a plain rowid DELETE
        with an expanding bind parameter, a no-op for empty input or a
        missing FTS table.
        """
        if not novel_ids or not self._fts_table_exists():
            return
        self.session.execute(
            _text(f"DELETE FROM {C.TABLE_NOVEL_FTS} WHERE rowid IN :nids")
            .bindparams(bindparam("nids", expanding=True)),
            {"nids": novel_ids},
        )

    # ------------------------------------------------------------------
    # Batch operations (Phase 2)
    # ------------------------------------------------------------------

    def batch_rebuild_fts(self, batch_size: int = 500) -> int:
        """Rebuild the entire FTS index in batches, avoiding per-row upserts.

        Drops and recreates the table (shape always matches
        ``_CREATE_SQL``), then bulk-inserts every novel.
        Returns the number of novels indexed.
        """
        # Recreate the table so its shape always matches _CREATE_SQL
        # (a legacy table may have a different definition), then bulk
        # re-insert.  'delete-all' is not used: it only works on
        # contentless / external-content FTS5 tables.
        # DDL auto-commits in SQLite; the INSERTs below are committed by
        # the caller's transaction (FTSManager never commits itself).
        self.session.execute(
            _text(f"DROP TABLE IF EXISTS {C.TABLE_NOVEL_FTS}")
        )
        self.session.execute(_text(self._CREATE_SQL))
        count = self._batch_insert_all_novels()
        logger.info(f"FTS5 batch rebuild complete — {count} novels indexed.")
        return count

    # ------------------------------------------------------------------
    # Health check (Phase 2)
    # ------------------------------------------------------------------

    def check_fts_health(self) -> dict:
        """Check FTS5 index health.

        Uses ``INSERT INTO ..._fts(..._fts) VALUES('rebuild')`` to detect
        corruption.  Returns a dict with health status and details.
        """
        result = {
            "fts_table_exists": False,
            "is_healthy": False,
            "novel_count": 0,
            "fts_entry_count": 0,
            "error": None,
        }

        if not self._fts_table_exists():
            result["error"] = "FTS virtual table does not exist"
            return result

        result["fts_table_exists"] = True

        # Count entries
        try:
            fts_count = self.session.execute(
                _text(f"SELECT COUNT(*) FROM {C.TABLE_NOVEL_FTS}")
            ).scalar()
            result["fts_entry_count"] = fts_count or 0

            novel_count = self.session.execute(
                select(func.count()).select_from(models.Novel)
            ).scalar()
            result["novel_count"] = novel_count or 0
        except Exception as e:
            result["error"] = f"Count query failed: {e}"
            return result

        # Corruption check: INSERT INTO ..._fts(..._fts) VALUES('rebuild')
        # If the index is corrupt, this will raise an error.
        # A SAVEPOINT (begin_nested) scopes the rollback to the probe row
        # only — a plain session.rollback() here would also discard any
        # unrelated pending writes in the caller's transaction.
        try:
            with self.session.begin_nested():
                self.session.execute(
                    _text(
                        f"INSERT INTO {C.TABLE_NOVEL_FTS}({C.TABLE_NOVEL_FTS}) "
                        f"VALUES('rebuild')"
                    )
                )
        except Exception as e:
            result["error"] = f"Integrity check failed: {e}"
            return result

        # Check for orphan entries (FTS rows pointing to nonexistent novels)
        orphan_count = self.session.execute(_text(
            f"SELECT COUNT(*) FROM {C.TABLE_NOVEL_FTS} "
            f"WHERE rowid NOT IN (SELECT {C.COL_ID} FROM {C.TABLE_NOVEL})"
        )).scalar()
        result["orphan_entries"] = orphan_count or 0

        # Check for missing entries (novels not in FTS)
        missing_count = self.session.execute(_text(
            f"SELECT COUNT(*) FROM {C.TABLE_NOVEL} "
            f"WHERE {C.COL_ID} NOT IN (SELECT rowid FROM {C.TABLE_NOVEL_FTS})"
        )).scalar()
        result["missing_entries"] = missing_count or 0

        result["is_healthy"] = (
            result["error"] is None
            and (orphan_count or 0) == 0
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fts_table_exists(self) -> bool:
        """Return True if the FTS5 virtual table exists."""
        fts_t = models.Base.metadata.tables.get(C.TABLE_NOVEL_FTS)
        if fts_t is not None:
            return True
        # Also check directly — the table may exist in DB but not in MetaData
        try:
            self.session.execute(_text(f"SELECT 1 FROM {C.TABLE_NOVEL_FTS} LIMIT 0"))
            return True
        except Exception:
            return False

    def _tokenize(self, text: str) -> str:
        """Tokenize *text* using jieba, adding spaces between CJK chars."""
        if not text:
            return ""
        tokens = list(self.jieba.cut(text, HMM=True))
        return " ".join(t for t in tokens if t.strip())

    def _batch_insert_fts_entries(
        self, rows: list[tuple[int, str, str, str]],
    ) -> None:
        """Insert multiple FTS entries in a single batch.

        Tokenizes each row individually (jieba can't run inside SQLite) but
        issues a single multi-row INSERT for efficiency.

        Uses raw SQL instead of ``Base.metadata.tables``: the ``novel_fts``
        virtual table is created via raw DDL and is deliberately NOT part
        of the ORM metadata, so ``metadata.tables.get()`` would always
        return None and the insert would be silently skipped (the FTS
        index would stay empty and keyword search would match nothing).
        """
        if not rows:
            return
        values = [
            {
                "id": nid,
                "title": self._tokenize(title),
                "author_name": self._tokenize(author),
                "series_name": self._tokenize(series),
                "tags": self._tokenize(tags),
            }
            for nid, title, author, series, tags in rows
        ]
        stmt = (
            f"INSERT INTO {C.TABLE_NOVEL_FTS}"
            f"(rowid, {C.COL_TITLE}, {C.COL_AUTHOR_NAME}, {C.COL_SERIES_NAME}, tags) "
            f"VALUES (:id, :title, :author_name, :series_name, :tags)"
        )
        self.session.execute(_text(stmt), values)

    def _batch_insert_all_novels(self) -> int:
        """Insert all novels from the novel table into FTS in one bulk operation.

        Uses raw SQL for efficiency — each row's text columns are tokenized
        individually because jieba can't run inside SQLite.
        """
        novels = self.session.execute(
            select(
                models.Novel.id,
                models.Novel.title,
                models.Novel.author_name,
                models.Novel.series_name,
                func.group_concat(models.Tag.name, " ").label("tags"),
            )
            .outerjoin(models.NovelTag, models.Novel.id == models.NovelTag.novel_id)
            .outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .group_by(models.Novel.id)
        ).all()

        if not novels:
            return 0

        self._batch_insert_fts_entries([
            (nid, title or "", author or "", series_name or "", tags or "")
            for nid, title, author, series_name, tags in novels
        ])

        return len(novels)
