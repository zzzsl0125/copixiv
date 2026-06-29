r"""FTS5 full-text search manager — jieba-powered indexing.

Features:
- Idempotent rebuild with ``CREATE VIRTUAL TABLE IF NOT EXISTS``
- Incremental and batch FTS updates
- FTS health check via ``INSERT INTO ..._fts(..._fts) VALUES('rebuild')``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text as _text, select, delete as _delete
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C

if TYPE_CHECKING:
    pass

logger = logging.getLogger("copixiv.fts")


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
        """Pre-import jieba so the first search isn't slow."""
        try:
            import jieba  # noqa: F401
        except ImportError:
            pass

    # ------------------------------------------------------------------
    # Rebuild (idempotent)
    # ------------------------------------------------------------------

    def rebuild_novel_fts(self) -> None:
        """Create FTS5 virtual table if missing, then rebuild from novels.

        Idempotent: uses ``CREATE VIRTUAL TABLE IF NOT EXISTS`` so it
        doesn't fail when the FTS table already exists.

        Uses ``INSERT INTO ..._fts(..._fts) VALUES('delete-all')`` to
        clear the index safely — avoids the "database disk image is
        malformed" error that can occur when DELETE is used on a
        content-synchronized FTS5 table.
        """
        self.session.execute(_text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {C.TABLE_NOVEL_FTS} USING fts5("
            f"  {C.COL_TITLE}, {C.COL_AUTHOR_NAME}, {C.COL_SERIES_NAME},"
            f"  content='{C.TABLE_NOVEL}', content_rowid='{C.COL_ID}'"
            f")"
        ))
        # Clear all existing FTS index entries safely
        self.session.execute(
            _text(
                f"INSERT INTO {C.TABLE_NOVEL_FTS}({C.TABLE_NOVEL_FTS}) "
                f"VALUES('delete-all')"
            )
        )
        self._batch_insert_all_novels()
        self.session.commit()
        logger.info("FTS5 index rebuilt from scratch.")

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    def update_novel_fts_index(self, novel_ids: list[int]) -> None:
        """Update FTS entries for the given novel IDs.

        No-op if FTS table missing or *novel_ids* is empty.  Uses
        FTS5 delete command (safe for content-synced tables) then
        re-inserts for each affected row.
        """
        if not novel_ids:
            return

        if not self._fts_table_exists():
            return

        # Use FTS5 'delete' command — avoids corruption on content-synced tables
        for nid in novel_ids:
            try:
                self.session.execute(
                    _text(
                        f"INSERT INTO {C.TABLE_NOVEL_FTS}"
                        f"({C.TABLE_NOVEL_FTS}) VALUES('delete', :id)"
                    ),
                    {"id": nid},
                )
            except Exception:
                # Row may not exist in FTS index yet — that's fine
                pass

        novels = self.session.execute(
            select(
                models.Novel.id,
                models.Novel.title,
                models.Novel.author_name,
                models.Novel.series_name,
            ).where(models.Novel.id.in_(novel_ids))
        ).all()

        for nid, title, author, series_name in novels:
            self._insert_fts_entry(
                nid, title or "", author or "", series_name or ""
            )

    def delete_novel_fts(self, novel_id: int) -> None:
        """Remove an FTS entry for a deleted novel.

        Uses FTS5 'delete' command — safe for content-synced tables.
        """
        try:
            self.session.execute(
                _text(
                    f"INSERT INTO {C.TABLE_NOVEL_FTS}"
                    f"({C.TABLE_NOVEL_FTS}) VALUES('delete', :id)"
                ),
                {"id": novel_id},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Batch operations (Phase 2)
    # ------------------------------------------------------------------

    def batch_rebuild_fts(self, batch_size: int = 500) -> int:
        """Rebuild the entire FTS index in batches, avoiding per-row upserts.

        Uses ``INSERT INTO ..._fts SELECT ... FROM novel`` for bulk transfer.
        Returns the number of novels indexed.
        """
        if not self._fts_table_exists():
            self.session.execute(_text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {C.TABLE_NOVEL_FTS} USING fts5("
                f"  {C.COL_TITLE}, {C.COL_AUTHOR_NAME}, {C.COL_SERIES_NAME},"
                f"  content='{C.TABLE_NOVEL}', content_rowid='{C.COL_ID}'"
                f")"
            ))

        # Clear existing entries safely (DELETE on content-synced FTS can corrupt)
        self.session.execute(
            _text(
                f"INSERT INTO {C.TABLE_NOVEL_FTS}({C.TABLE_NOVEL_FTS}) "
                f"VALUES('delete-all')"
            )
        )
        count = self._batch_insert_all_novels()
        self.session.commit()
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
                select(models.Novel).with_only_columns(
                    models.Novel.id
                )
            ).scalars().all()
            result["novel_count"] = len(novel_count)
        except Exception as e:
            result["error"] = f"Count query failed: {e}"
            return result

        # Corruption check: INSERT INTO ..._fts(..._fts) VALUES('rebuild')
        # If the index is corrupt, this will raise an error.
        try:
            self.session.execute(
                _text(
                    f"INSERT INTO {C.TABLE_NOVEL_FTS}({C.TABLE_NOVEL_FTS}) "
                    f"VALUES('rebuild')"
                )
            )
            # Remove the test row
            self.session.rollback()
        except Exception as e:
            result["error"] = f"Integrity check failed: {e}"
            self.session.rollback()
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

    def _insert_fts_entry(
        self, novel_id: int, title: str, author: str, series: str
    ) -> None:
        fts_t = models.Base.metadata.tables.get(C.TABLE_NOVEL_FTS)
        if fts_t is None:
            return
        self.session.execute(
            fts_t.insert().values(
                id=novel_id,
                title=self._tokenize(title),
                author_name=self._tokenize(author),
                series_name=self._tokenize(series),
            )
        )

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
            )
        ).all()

        if not novels:
            return 0

        for nid, title, author, series_name in novels:
            self._insert_fts_entry(
                nid, title or "", author or "", series_name or ""
            )

        return len(novels)
