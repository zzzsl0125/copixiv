"""Base query builder — FTS availability cache and pagination/ordering helpers.

Shared between concrete builders (currently ``NovelQueryBuilder``).  The
FTS-availability cache lives here so any module (repositories, maintenance
tasks, tests) can reset it through one import without touching the
novel-specific builder.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, text as _text, tuple_ as _tuple
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import constants as C

# ------------------------------------------------------------------
# FTS availability cache — checked once per process lifetime.
# The FTS virtual table is created at database init and persists, so
# there is no need to probe it on every single keyword query.
# ------------------------------------------------------------------
_fts_available: bool | None = None

# FTS5 query-language reserved words — kept out of MATCH queries because
# a bare reserved word is parsed as an operator (e.g. ``AND OR`` is a
# syntax error), turning user input into a 500 response.
_FTS5_RESERVED: frozenset[str] = frozenset({
    "and", "or", "not", "near",
})


def reset_fts_cache() -> None:
    """Reset the FTS availability cache (call after FTS index rebuild).

    ``rebuild_fts`` can run while the process is alive (maintenance task) —
    without this reset, a process that started with the FTS table missing
    would keep skipping keyword filters even after the rebuild created it.
    """
    global _fts_available
    _fts_available = None


def _check_fts_available(session: Session) -> bool:
    """Return True if the FTS virtual table exists in the database.

    The result is cached at module level — the check runs at most once
    per process lifetime (unless ``reset_fts_cache()`` is called).
    """
    global _fts_available
    if _fts_available is not None:
        return _fts_available
    try:
        session.execute(
            _text(f"SELECT 1 FROM {C.TABLE_NOVEL_FTS} LIMIT 0")
        )
        _fts_available = True
    except Exception:
        _fts_available = False
    return _fts_available


class BaseQueryBuilder:
    """Shared helpers for pagination, ordering, and FTS query building."""

    def __init__(self, session: Session, main_model: type):
        self.session = session
        self.main_model = main_model
        self.params: dict[str, Any] = {}
        self._fts_query: str | None = None

    @property
    def fts_query(self) -> str | None:
        return self._fts_query

    @staticmethod
    def _build_fts_query_string(keyword_string: str) -> str:
        """Convert a user keyword string into an FTS5-safe AND query.

        Tokenises through jieba so that the search query matches the same
        tokens that were indexed (the FTS index was built with jieba too).
        Tokens containing spaces are wrapped in double-quotes for phrase
        matching; single tokens are left bare.  Tokens that are pure
        punctuation / FTS5 operators are dropped to avoid syntax errors.
        """
        if not keyword_string.strip():
            return ""

        # Tokenise the same way the index was built (see FTSManager._tokenize)
        try:
            import jieba
            raw_tokens = list(jieba.cut(keyword_string, HMM=True))
            # Only keep tokens that contain at least one alphanumeric / CJK
            # character — pure-punctuation tokens (like "-") are invalid in
            # FTS5 MATCH queries.
            tokens = [
                t.strip() for t in raw_tokens
                if t.strip() and any(ch.isalnum() or ord(ch) > 127 for ch in t)
            ]
        except ImportError:
            tokens = [t for t in keyword_string.split() if any(ch.isalnum() for ch in t)]

        if not tokens:
            return ""

        # The MATCH query is passed as a bound parameter (see
        # ``_where_fts_filter``), so no SQL-string escaping is needed here.
        # This step only keeps the query valid as an FTS5 expression:
        # tokens containing spaces are wrapped in double-quotes to form a
        # phrase; a bare single-quote inside a token would otherwise start
        # an unterminated FTS5 string literal (syntax error), so such
        # quotes are stripped; and tokens that are FTS5 reserved words
        # (AND/OR/NOT/NEAR) are dropped — left bare they'd be parsed as
        # operators and could turn the whole query into a syntax error.
        tokens = [
            t for t in tokens
            if t.strip().lower() not in _FTS5_RESERVED
        ]
        if not tokens:
            return ""

        return " AND ".join(
            f'"{t}"' if " " in t else t.replace("'", "") for t in tokens
        )

    def _apply_cursor(
        self, stmt: Select, cursor: dict | None, order_by: str,
        order_direction: str = "DESC",
    ) -> Select:
        """Apply cursor-based keyset pagination.

        Uses ``<`` for DESC (next page = smaller values) and ``>`` for ASC
        (next page = larger values).  Secondary-sorts on ``id`` to avoid
        skipping or duplicating rows that share the same sort-column value.
        """
        if not cursor:
            return stmt

        # Precomputed shuffle column for random ordering — seek on index
        if order_by == "random" and "shuffle" in cursor and "id" in cursor:
            last_shuffle = cursor["shuffle"]
            last_id = cursor["id"]
            return stmt.where(
                _tuple(self.main_model.shuffle, self.main_model.id)
                > _tuple(last_shuffle, last_id)
            )

        col = getattr(self.main_model, order_by, None)
        if col is not None:
            # Tiebreaker: when multiple rows share the same sort-column
            # value, secondary-sort by id so no row is skipped or
            # duplicated across pages.
            #
            # Use row-value tuple comparison (col, id) < (cursor_val, cursor_id)
            # instead of (col < cursor_val) OR (col = cursor_val AND id < cursor_id).
            # The tuple form lets SQLite use a single index range scan on a
            # composite (col, id) index — the OR form forces a UNION of two
            # separate seeks, which is dramatically slower on page 2 (the first
            # page with a cursor) because it cannot terminate early after LIMIT
            # rows and must exhaust both OR branches.
            descending = order_direction.upper() == "DESC"
            cursor_val = cursor[order_by]
            cursor_id = cursor["id"]
            if descending:
                stmt = stmt.where(
                    _tuple(col, self.main_model.id) < _tuple(cursor_val, cursor_id)
                )
            else:
                stmt = stmt.where(
                    _tuple(col, self.main_model.id) > _tuple(cursor_val, cursor_id)
                )
        return stmt

    def _apply_ordering(
        self, stmt: Select, order_by: str, order_direction: str,
    ) -> Select:
        """Apply ORDER BY clause."""
        # Precomputed shuffle column — walk index, no temp B-Tree.
        if order_by == "random":
            return stmt.order_by(
                self.main_model.shuffle.asc(), self.main_model.id.asc(),
            )

        col = getattr(self.main_model, order_by, None)
        if col is not None:
            if order_direction.upper() == "DESC":
                return stmt.order_by(col.desc(), self.main_model.id.desc())
            else:
                return stmt.order_by(col.asc(), self.main_model.id.asc())
        return stmt

    def _apply_limit(self, stmt: Select, limit: int) -> Select:
        """Apply LIMIT clause."""
        return stmt.limit(limit)
