r"""``novel_search`` derived-table manager — character-unigram (char-gram) search.

The SQLite-era ``FTS5`` virtual table is gone.  Keyword search now runs
against the **application-maintained ``novel_search`` derived table**:
a plain ``(novel_id, search_text)`` row whose ``search_text`` is the
char-gram text computed by :func:`gram_tokenize` over
``title + author_name + series_name + tags``.  The GIN index on
``to_tsvector('simple', search_text)`` (created by the Alembic baseline)
answers phrase queries.  ``search_text`` is recomputed in the write path
(the repository calls this manager inside the same transaction) and there is
a periodic full-rebuild fallback for any drift.

Because ``novel_search`` is an ordinary table with a ``FOREIGN KEY ... ON
DELETE CASCADE`` to ``novel``, deleting a novel removes its search row
automatically — no manual FTS delete is needed.  Keyword filter construction
lives in ``repo._build_fts_query_string``; this module owns the *index
maintenance* side (the single source of truth for ``search_text``).

The query side uses ``to_tsvector('simple', search_text) @@
to_tsquery('simple', '<gram phrase>')`` — the ``simple`` tokeniser treats the
``龖`` placeholder (for non-alphanumerics) and CJK chars as word characters,
so 1/2/multi-character phrases match exactly (spike_pg_report §4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text as _text, select, delete as _delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models

if TYPE_CHECKING:
    pass

from copixiv.log import logger


# The placeholder character substituted for any non-alphanumeric character
# (punctuation etc.) on BOTH the index side and the query side.  This is
# U+9F96 (a CJK unified ideograph), chosen because it is a token character
# for the ``simple`` tokeniser, resides in the same Unicode block as CJK
# text, and is expected to appear ~0 times in the corpus (so it never
# collides with real content).  Index and query MUST use the same
# placeholder — see :func:`gram_tokenize`.
_GRAM_PLACEHOLDER = "龖"


def gram_tokenize(text: str) -> str:
    """Convert *text* into character-unigram token text for char-gram search.

    This is the single source of truth shared by the index side
    (``novel_search`` insertion via :func:`build_search_text`) and the query
    side (``repo._build_fts_query_string``).  Both sides MUST call this exact
    function and produce identical strings — any divergence between them
    silently breaks every keyword search (R1 regression), so keep them in
    lockstep.

    Rules (applied per character, then joined by a single space):
    * whitespace characters are skipped (they carry no meaning as tokens);
    * ``ch.isalpha() or ch.isnumeric()`` keeps the character unchanged
      (the original case is preserved);
    * every other character maps to the placeholder ``龖``.

    Examples::

        gram_tokenize("普通文本") == "普 通 文 本"
        gram_tokenize("R-18")     == "R 龖 1 8"

    ``simple`` folds case on both the index and the query side, so case
    differences between the stored text and the query never affect matching
    (``TS`` and ``ts`` match each other).  The empty string (or text made
    entirely of whitespace) maps to ``""``, preserving the "empty in, empty
    out" contract.
    """
    if not text:
        return ""
    chars: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if ch.isalpha() or ch.isnumeric():
            chars.append(ch)
        else:
            chars.append(_GRAM_PLACEHOLDER)
    return " ".join(chars)


def build_search_text(
    title: str | None,
    author_name: str | None,
    series_name: str | None,
    tags: list[str],
) -> str:
    """Build the ``novel_search.search_text`` for one novel.

    This is the **single source of truth** for the derived ``search_text``:
    both the one-time SQLite→PG migration (``scripts/migrate_sqlite_to_pg.py``)
    and the runtime write path (``FTSManager``) call exactly this function, so
    the migrated rows and the incrementally-maintained rows are always built
    identically.

    Semantics: char-gram each of ``title``/``author_name``/``series_name`` and
    the space-joined tag names, then join the non-empty parts with a single
    space.  Whitespace/cross-field boundaries collapse to a single space in the
    stored token stream (the ``simple`` tokeniser splits each char into its own
    token regardless), so within-field phrases (the common case) match exactly.
    Empty parts are dropped, so an empty title never leaves a stray leading
    space.
    """
    parts: list[str] = []
    for text in (title, author_name, series_name, " ".join(tags or [])):
        if text:
            parts.append(gram_tokenize(text))
    return " ".join(parts)


class FTSManager:
    """Maintains the application-maintained ``novel_search`` derived table.

    ``novel_search`` is NOT a full-text virtual table anymore — it is a plain
    table the repository keeps in sync (same transaction as the ``novel``
    write).  This manager provides the incremental upsert/delete helpers and
    the full-rebuild fallback, plus health checks.
    """

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    def update_novel_fts_index(self, novel_ids: list[int]) -> None:
        """Recompute ``search_text`` for the given novel IDs (upsert).

        Runs an ``INSERT ... ON CONFLICT (novel_id) DO UPDATE`` inside the
        caller's transaction so the search row and the novel row are always
        consistent.  No-op for empty input.
        """
        if not novel_ids:
            return

        novels = self.session.execute(
            select(
                models.Novel.id,
                models.Novel.title,
                models.Novel.author_name,
                models.Novel.series_name,
                models.Novel.tags,
            )
            .where(models.Novel.id.in_(novel_ids))
        ).all()

        for nid, title, author, series, tags in novels:
            search_text = build_search_text(title, author, series, tags or [])
            self.session.execute(
                pg_insert(models.NovelSearch)
                .values(novel_id=nid, search_text=search_text)
                .on_conflict_do_update(
                    index_elements=["novel_id"],
                    set_={"search_text": search_text},
                )
            )

    def delete_novel_fts(self, novel_id: int) -> None:
        """Remove one ``novel_search`` row.

        Normally unnecessary (the FK ``ON DELETE CASCADE`` removes it when the
        novel is deleted) — kept as an explicit helper for callers that delete
        a search row without touching ``novel``.
        """
        self.session.execute(
            _delete(models.NovelSearch).where(
                models.NovelSearch.novel_id == novel_id
            )
        )

    def delete_novel_fts_many(self, novel_ids: list[int]) -> None:
        """Remove many ``novel_search`` rows in one statement."""
        if not novel_ids:
            return
        self.session.execute(
            _delete(models.NovelSearch).where(
                models.NovelSearch.novel_id.in_(novel_ids)
            )
        )

    # ------------------------------------------------------------------
    # Full rebuild (fallback)
    # ------------------------------------------------------------------

    def batch_rebuild_fts(self, batch_size: int = 500) -> int:
        """Rebuild the entire ``novel_search`` derived table from ``novel``.

        ``TRUNCATE`` then bulk-inserts ``search_text`` for every novel computed
        via :func:`build_search_text`.  Returns the number of novels indexed.
        Uses a plain Python-side gram (the ``simple`` tokeniser run inside
        PostgreSQL can't reproduce the ``龖`` placeholder logic), so rows are
        built in the application and inserted in bulk.
        """
        self.session.execute(_text("TRUNCATE novel_search"))
        novels = self.session.execute(
            select(
                models.Novel.id,
                models.Novel.title,
                models.Novel.author_name,
                models.Novel.series_name,
                models.Novel.tags,
            )
        ).all()
        rows = [
            {
                "novel_id": nid,
                "search_text": build_search_text(title, author, series, tags or []),
            }
            for nid, title, author, series, tags in novels
        ]
        if rows:
            self.session.execute(pg_insert(models.NovelSearch), rows)
        logger.info(f"novel_search full rebuild complete — {len(rows)} novels indexed.")
        return len(rows)

    def needs_rebuild(self) -> bool:
        """Return True when ``novel_search`` is missing or stale.

        Compares ``count(novel_search)`` to ``count(novel)`` — the derived
        table must have exactly one row per novel.  Kept as an instance method
        (the app.startup self-heal path calls ``FTSManager(session).needs_rebuild()``).
        """
        ns_count = self.session.execute(
            select(func.count()).select_from(models.NovelSearch)
        ).scalar() or 0
        novel_count = self.session.execute(
            select(func.count()).select_from(models.Novel)
        ).scalar() or 0
        return ns_count != novel_count

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def check_fts_health(self) -> dict:
        """Check ``novel_search`` health.

        The GIN index is created at migration time and always exists, so
        ``fts_table_exists`` is always True.  Reports entry/novel counts and
        any orphan (search row without a novel — should be 0 given the FK) /
        missing (novel without a search row) entries.
        """
        result: dict = {
            "fts_table_exists": True,
            "is_healthy": False,
            "novel_count": 0,
            "fts_entry_count": 0,
            "orphan_entries": 0,
            "missing_entries": 0,
            "error": None,
        }

        try:
            fts_count = self.session.execute(
                select(func.count()).select_from(models.NovelSearch)
            ).scalar() or 0
            novel_count = self.session.execute(
                select(func.count()).select_from(models.Novel)
            ).scalar() or 0
            result["fts_entry_count"] = fts_count
            result["novel_count"] = novel_count

            orphan_count = self.session.execute(
                _text(
                    "SELECT count(*) FROM novel_search ns "
                    "WHERE NOT EXISTS (SELECT 1 FROM novel n WHERE n.id = ns.novel_id)"
                )
            ).scalar() or 0
            missing_count = self.session.execute(
                _text(
                    "SELECT count(*) FROM novel n "
                    "WHERE NOT EXISTS (SELECT 1 FROM novel_search ns WHERE ns.novel_id = n.id)"
                )
            ).scalar() or 0
            result["orphan_entries"] = orphan_count
            result["missing_entries"] = missing_count
            result["is_healthy"] = orphan_count == 0 and missing_count == 0
        except Exception as e:  # pragma: no cover - defensive
            result["error"] = str(e)
            result["is_healthy"] = False

        return result
