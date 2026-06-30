"""Novel query builder — single-phase, filter-driven queries for SQLite.

Key design decisions (v2 rewrite):
- Single-phase: no nested "filtered_ids subquery → main query" pattern.
  The old two-phase approach caused ``USE TEMP B-TREE FOR ORDER BY`` on every
  request because SQLite lost ordering across the subquery boundary.
- Filter-driven with WHERE-IN: tag and FTS filters produce independent
  subqueries of novel IDs, used via ``WHERE novel.id IN (...)``.  This lets
  SQLite use covering indexes (ix_novel_like, idx_novel_author_likes, etc.)
  for ORDER BY + LIMIT because the outer scan stays on the novel table.
- JOIN for small tables: favourite/special_follow filters use INNER JOIN
  since those tables are tiny (67 and 58 rows respectively).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    select, func, case, Select, text as _text, literal_column,
)
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C


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

        return " AND ".join(
            f'"{t}"' if " " in t else t for t in tokens
        )

    # ------------------------------------------------------------------
    # Hash-based pseudo-random sort key (deterministic, seed-driven)
    # ------------------------------------------------------------------
    # Uses a multiplicative hash so that "random" ordering is repeatable
    # across pages: same seed → same global order.  The formula is
    #   ((id * A + seed * B) & MASK) % MOD
    # with large primes to produce good dispersion without overflow.
    _RAND_A: int = 393555900037
    _RAND_B: int = 1728364729
    _RAND_MASK: int = 0x7FFFFFFFFFFFFFFF   # clear sign bit (63-bit)
    _RAND_MOD: int = 9223372036854775783   # a large prime

    def _hash_sort_key(self, seed: int):
        """Return a SQLAlchemy expression for the deterministic sort key."""
        inner = (
            self.main_model.id * self._RAND_A
            + seed * self._RAND_B
        )
        masked = inner.op("&")(self._RAND_MASK)
        return masked.op("%")(self._RAND_MOD)

    def _apply_cursor(
        self, stmt: Select, cursor: dict | None, order_by: str,
    ) -> Select:
        """Apply cursor-based keyset pagination."""
        if not cursor:
            return stmt

        # Pseudo-random ordering — compare hash sort keys
        if order_by == "random" and "random" in cursor and "id" in cursor:
            seed = cursor.get("random_seed", 0)
            sort_key = self._hash_sort_key(seed)
            last_hash = cursor["random"]
            last_id = cursor["id"]
            return stmt.where(
                (sort_key > last_hash)
                | ((sort_key == last_hash) & (self.main_model.id > last_id))
            )

        col = getattr(self.main_model, order_by, None)
        if col is not None:
            stmt = stmt.where(col < cursor[order_by])
        return stmt

    def _apply_ordering(
        self, stmt: Select, order_by: str, order_direction: str,
    ) -> Select:
        """Apply ORDER BY clause."""
        # Pseudo-random ordering — sort by hash(id, seed), then id
        if order_by == "random":
            cursor = self.params.get("cursor") or {}
            seed = cursor.get("random_seed", 0)
            sort_key = self._hash_sort_key(seed)
            return stmt.order_by(sort_key.asc(), self.main_model.id.asc())

        col = getattr(self.main_model, order_by, None)
        if col is not None:
            if order_direction.upper() == "DESC":
                return stmt.order_by(col.desc())
            else:
                return stmt.order_by(col.asc())
        return stmt

    def _apply_limit(self, stmt: Select, limit: int) -> Select:
        """Apply LIMIT clause."""
        return stmt.limit(limit)


class NovelQueryBuilder(BaseQueryBuilder):
    """Builds single-phase Novel list and count queries.

    Query structure (conceptual)::

        SELECT novel.*, CASE ... is_favourite, CASE ... is_special_follow
        FROM novel
        LEFT JOIN favourite        ON novel.id = favourite.novel_id
        LEFT JOIN special_follow   ON novel.author_id = sf.author_id
        [JOIN favourite            ON ...]   -- if filtering by favourite
        [JOIN special_follow       ON ...]   -- if filtering by sf
        WHERE novel.id IN (<tag_id_subquery>)        -- if tags
          AND novel.id IN (<fts_id_subquery>)         -- if keyword
          AND [thresholds / author_id / series_id / cursor]
        ORDER BY ...
        LIMIT ...

    The WHERE-IN pattern for tags and FTS is critical: because the outer
    query scans the novel table via a covering index (e.g. ix_novel_like),
    SQLite can use the index ordering to satisfy ORDER BY + LIMIT without
    a temporary B-Tree.  The subqueries are independent (no outer reference),
    so they are materialised once, not correlated per row.

    Count query uses the same filter structure, drops ORDER BY / LIMIT.
    """

    def __init__(self, repo, **params):
        super().__init__(repo.session, models.Novel)
        self.repo = repo
        self.params = params

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def build(self) -> tuple[Select, dict]:
        """Build the main list query."""
        queries = self.params.get("queries") or {}
        tags, keywords, field_filters = self._categorize(queries)

        # Skip display-flag JOINs when the query already filters by them
        skip_fav = C.FIELD_IS_FAVOURITE in field_filters
        skip_sf = C.FIELD_IS_SPECIAL_FOLLOW in field_filters
        main = self._base_select(
            skip_favourite_join=skip_fav,
            skip_special_follow_join=skip_sf,
        )

        # Filter JOINs for favourite / special_follow (WHERE-IN subqueries)
        main = self._join_field_filter_tables(main, field_filters)

        # WHERE-IN filters for tags and FTS (independent subqueries)
        main = self._where_tag_filter(main, tags)
        main = self._where_fts_filter(main, keywords)

        # WHERE conditions on novel columns
        main = self._where_field_filters(main, field_filters)
        main = self._where_thresholds(main)

        # Pagination, ordering, limit — applied last so indexes can serve ORDER BY
        main = self._apply_cursor(
            main, self.params.get("cursor"), self.params["order_by"],
        )
        main = self._apply_ordering(
            main, self.params["order_by"], self.params["order_direction"],
        )
        main = self._apply_limit(main, self.params["per_page"])

        return main, self.params

    def build_count(self) -> Select | None:
        """Build a COUNT(*) query with the same filters, without limit.

        Returns None when there are no filters (caller can use a cheap
        ``SELECT COUNT(*) FROM novel``).
        """
        queries = self.params.get("queries") or {}
        tags, keywords, field_filters = self._categorize(queries)

        has_filters = bool(
            tags or keywords or field_filters
            or self.params.get("min_like") is not None
            or self.params.get("min_text") is not None
        )
        if not has_filters:
            return None

        stmt = select(func.count()).select_from(self.main_model)
        stmt = self._join_field_filter_tables(stmt, field_filters)
        stmt = self._where_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)
        return stmt

    # ------------------------------------------------------------------
    # Internal: SELECT columns
    # ------------------------------------------------------------------

    def _base_select(
        self,
        skip_favourite_join: bool = False,
        skip_special_follow_join: bool = False,
    ) -> Select:
        """Build the SELECT clause with all novel columns + display flags.

        When the query already filters by *is_favourite* or
        *is_special_follow*, the corresponding OUTER JOIN can be skipped
        because the flag value is statically known (1).
        """
        cols: list = list(self.main_model.__table__.c)

        if skip_favourite_join:
            cols.append(literal_column("1").label(C.FIELD_IS_FAVOURITE))
        else:
            cols.append(
                case(
                    (models.Favourite.novel_id != None, 1), else_=0,
                ).label(C.FIELD_IS_FAVOURITE),
            )

        if skip_special_follow_join:
            cols.append(literal_column("1").label(C.FIELD_IS_SPECIAL_FOLLOW))
        else:
            cols.append(
                case(
                    (models.SpecialFollow.author_id != None, 1), else_=0,
                ).label(C.FIELD_IS_SPECIAL_FOLLOW),
            )

        stmt = select(*cols).select_from(self.main_model)

        if not skip_favourite_join:
            stmt = stmt.outerjoin(
                models.Favourite,
                self.main_model.id == models.Favourite.novel_id,
            )
        if not skip_special_follow_join:
            stmt = stmt.outerjoin(
                models.SpecialFollow,
                self.main_model.author_id == models.SpecialFollow.author_id,
            )

        return stmt

    # ------------------------------------------------------------------
    # Internal: filter categorisation
    # ------------------------------------------------------------------

    @staticmethod
    def _categorize(queries: dict) -> tuple[set, set, dict]:
        """Split query dict into (tags, keywords, field_filters)."""
        tags: set[str] = set()
        keywords: set[str] = set()
        field_filters: dict[str, str] = {}
        for value, qtype in queries.items():
            if not isinstance(value, str) or value.strip() == "":
                continue
            if qtype == C.FIELD_TAGS:
                tags.add(value)
            elif qtype == C.FIELD_KEYWORD:
                keywords.add(value)
            else:
                field_filters[qtype] = value
        return tags, keywords, field_filters

    # ------------------------------------------------------------------
    # Internal: tag filter — WHERE novel.id IN (independent subquery)
    # ------------------------------------------------------------------

    def _where_tag_filter(self, stmt: Select, tag_names: set[str]) -> Select:
        """Add ``WHERE novel.id IN (tag_subquery)`` for each tag.

        Uses independent (non-correlated) subqueries so SQLite can still
        use covering indexes on the novel table for ORDER BY + LIMIT.

        Single tag: ``WHERE novel.id IN (SELECT novel_id FROM novel_tag
        JOIN tag WHERE tag.name = 'X')``

        Multiple tags: one IN subquery per tag (intersection via chained
        WHERE-IN clauses).  INTERSECT is also viable but WHERE-IN chains
        are simpler and let SQLite choose the best index strategy.
        """
        if not tag_names:
            return stmt

        for tag_name in tag_names:
            tag_ids_subq = (
                select(models.NovelTag.novel_id)
                .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
                .where(models.Tag.name == tag_name)
            )
            stmt = stmt.where(self.main_model.id.in_(tag_ids_subq))
        return stmt

    # ------------------------------------------------------------------
    # Internal: FTS / keyword filter — WHERE novel.id IN (fts subquery)
    # ------------------------------------------------------------------

    def _where_fts_filter(
        self, stmt: Select, keywords: set[str],
    ) -> Select:
        """Add ``WHERE novel.id IN (SELECT rowid FROM novel_fts WHERE
        novel_fts MATCH 'query')``.

        Uses raw SQL for the FTS virtual table because it may not be
        registered in SQLAlchemy's MetaData (created via raw DDL).

        The subquery is independent — does NOT reference the outer
        ``novel.id`` — so SQLite materialises it once.
        """
        if not keywords:
            return stmt

        keyword_string = " ".join(filter(None, keywords))
        if not keyword_string.strip():
            return stmt

        fts_query = self._build_fts_query_string(keyword_string)
        self._fts_query = fts_query

        # Check that the FTS virtual table exists in the database
        try:
            self.session.execute(
                _text(f"SELECT 1 FROM {C.TABLE_NOVEL_FTS} LIMIT 0")
            )
        except Exception:
            return stmt

        # Build: WHERE novel.id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH '...')
        # Using raw text because novel_fts is a virtual table that may not
        # be in SQLAlchemy MetaData, and its columns (rowid) aren't ORM-mapped.
        inner = (
            select(literal_column("rowid"))
            .select_from(_text(C.TABLE_NOVEL_FTS))
            .where(
                _text(f"{C.TABLE_NOVEL_FTS} MATCH '{fts_query}'")
            )
        )
        return stmt.where(self.main_model.id.in_(inner))

    # ------------------------------------------------------------------
    # Internal: field filter tables (favourite, special_follow)
    # ------------------------------------------------------------------

    def _join_field_filter_tables(
        self, stmt: Select, field_filters: dict,
    ) -> Select:
        """Add filters for favourite / special_follow.

        These CANNOT use INNER JOIN because ``_base_select()`` already
        LEFT JOINs the same tables for the display flags (is_favourite /
        is_special_follow CASE expressions).  A second JOIN on the same
        table would produce an ambiguous column reference.

        Instead we use independent ``WHERE id IN (SELECT ...)`` subqueries
        which are cheap since these tables are tiny (67 / 58 rows).
        """
        for qtype, _value in field_filters.items():
            if qtype == C.FIELD_IS_FAVOURITE:
                stmt = stmt.where(
                    self.main_model.id.in_(
                        select(models.Favourite.novel_id)
                    )
                )
            elif qtype == C.FIELD_IS_SPECIAL_FOLLOW:
                stmt = stmt.where(
                    self.main_model.author_id.in_(
                        select(models.SpecialFollow.author_id)
                    )
                )
        return stmt

    # ------------------------------------------------------------------
    # Internal: field filter WHERE conditions
    # ------------------------------------------------------------------

    def _where_field_filters(
        self, stmt: Select, field_filters: dict,
    ) -> Select:
        """Add WHERE conditions for column-based filters."""
        for qtype, value in field_filters.items():
            self.repo._validate_query_field(qtype)

            if qtype in (C.FIELD_IS_FAVOURITE, C.FIELD_IS_SPECIAL_FOLLOW):
                # Handled by _join_field_filter_tables above
                continue

            if value and qtype in self.repo.VALID_NOVEL_FIELDS:
                model_field = getattr(self.main_model, qtype)
                if qtype in (C.COL_AUTHOR_ID, C.COL_SERIES_ID, C.COL_ID):
                    stmt = stmt.where(model_field.in_([value]))
                else:
                    stmt = stmt.where(model_field == value)
        return stmt

    # ------------------------------------------------------------------
    # Internal: thresholds
    # ------------------------------------------------------------------

    def _where_thresholds(self, stmt: Select) -> Select:
        """Add WHERE conditions for min_like / min_text thresholds."""
        if self.params.get("min_like") is not None:
            stmt = stmt.where(
                func.coalesce(self.main_model.like, 0)
                >= self.params["min_like"]
            )
        if self.params.get("min_text") is not None:
            stmt = stmt.where(
                self.main_model.text >= self.params["min_text"]
            )
        return stmt
