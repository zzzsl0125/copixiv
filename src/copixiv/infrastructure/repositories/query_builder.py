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
    select, func, case, Select, text as _text, literal_column, exists as _exists,
    table, column, Integer, tuple_ as _tuple,
)
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C

# ------------------------------------------------------------------
# FTS availability cache — checked once per process lifetime.
# The FTS virtual table is created at database init and persists, so
# there is no need to probe it on every single keyword query.
# ------------------------------------------------------------------
_fts_available: bool | None = None


# Lightweight table reference for the FTS5 virtual table so SQLAlchemy's
# ORM compile state can handle subqueries that select from it (TextClause
# lacks a .selectable attribute and causes AttributeError).
_fts_table = table(
    C.TABLE_NOVEL_FTS,
    column("rowid", Integer),
)

# FTS5 query-language reserved words — kept out of MATCH queries because
# a bare reserved word is parsed as an operator (e.g. ``AND OR`` is a
# syntax error), turning user input into a 500 response.
_FTS5_RESERVED: frozenset[str] = frozenset({
    "and", "or", "not", "near",
})

# Adaptive filter thresholds (benchmarked on the real 232k-novel database).
# For list queries, low-selectivity tag/keyword filters are faster as
# ``WHERE id IN (...)``, while high-selectivity filters are faster as
# correlated ``EXISTS`` (which can stop early along the ORDER BY index).
# These thresholds sit at the measured crossover point.
_ADAPTIVE_TAG_THRESHOLD = 3000
_ADAPTIVE_KEYWORD_THRESHOLD = 15000


def reset_fts_cache() -> None:
    """Reset the FTS availability cache (call after FTS index rebuild)."""
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

        # Tag and FTS filters — adaptive for list queries: rare filters
        # use WHERE-IN, popular filters use EXISTS so SQLite can walk the
        # covering index for ORDER BY + LIMIT without a temporary B-Tree.
        main = self._where_tag_filter(main, tags, use_exists=True)
        main = self._where_fts_filter(main, keywords, use_exists=True)

        # WHERE conditions on novel columns
        main = self._where_field_filters(main, field_filters)
        main = self._where_thresholds(main)

        # Pagination, ordering, limit — applied last so indexes can serve ORDER BY
        main = self._apply_cursor(
            main, self.params.get("cursor"), self.params["order_by"],
            self.params.get("order_direction", "DESC"),
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
        # COUNT queries: special_follow uses IN instead of EXISTS (avoids
        # a full novel scan), and tags use JOINs instead of a large IN list.
        stmt = self._join_field_filter_tables(
            stmt, field_filters, use_exists_for_special_follow=False,
        )
        stmt = self._join_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords, use_exists=False)
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

        Tags are now loaded in batch by the repository after the main query
        via ``_batch_load_tags`` — no per-row correlated subquery.
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
    # Internal: tag filter — WHERE-IN (count) or EXISTS (list)
    # ------------------------------------------------------------------

    def _get_tag_reference_counts(
        self, tag_names: set[str],
    ) -> dict[str, int]:
        """Return ``{tag_name: reference_count}`` for the given tag names."""
        if not tag_names:
            return {}
        rows = self.session.execute(
            select(models.Tag.name, models.Tag.reference_count)
            .where(models.Tag.name.in_(tag_names))
        ).all()
        return {name: count for name, count in rows}

    def _where_tag_filter(
        self, stmt: Select, tag_names: set[str], use_exists: bool = False,
        adaptive: bool = True,
    ) -> Select:
        """Add tag filter conditions.

        For list queries (``use_exists=True``) the strategy is adaptive:
        rare tags use ``WHERE id IN (...)``, popular tags use correlated
        ``EXISTS``.  The threshold is based on ``tag.reference_count``,
        which is maintained incrementally and is cheap to read.

        * ``reference_count < _ADAPTIVE_TAG_THRESHOLD`` → IN
        * otherwise → EXISTS

        For count/filter-only queries (``use_exists=False``) the method
        keeps the ``WHERE id IN (...)`` form (the count path now prefers
        ``_join_tag_filter`` for large result sets).
        """
        if not tag_names:
            return stmt

        ref_counts = (
            self._get_tag_reference_counts(tag_names)
            if use_exists and adaptive else {}
        )

        for tag_name in tag_names:
            use_in = False
            if use_exists:
                if adaptive:
                    ref = ref_counts.get(tag_name)
                    # Missing tag means zero occurrences → treat as rare/IN.
                    use_in = ref is None or ref < _ADAPTIVE_TAG_THRESHOLD
                # else: adaptive=False keeps legacy EXISTS behaviour.

            if use_exists and not use_in:
                exists_subq = _exists(
                    select(literal_column("1"))
                    .select_from(models.NovelTag)
                    .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
                    .where(
                        models.NovelTag.novel_id == self.main_model.id,
                        models.Tag.name == tag_name,
                    )
                )
                stmt = stmt.where(exists_subq)
            else:
                tag_ids_subq = (
                    select(models.NovelTag.novel_id)
                    .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
                    .where(models.Tag.name == tag_name)
                )
                stmt = stmt.where(self.main_model.id.in_(tag_ids_subq))
        return stmt

    def _join_tag_filter(
        self, stmt: Select, tag_names: set[str],
    ) -> Select:
        """Add tag filters as INNER JOINs (used by COUNT queries).

        A direct join avoids materialising a large ``id IN (SELECT ...)``
        list; for very popular tags this is faster and uses less temp space
        (measured 249 ms → 171 ms for R-18 on 232k novels).
        """
        for idx, tag_name in enumerate(tag_names, start=1):
            nt_alias = models.NovelTag.__table__.alias(f"nt_{idx}")
            t_alias = models.Tag.__table__.alias(f"t_{idx}")
            stmt = stmt.join(
                nt_alias,
                self.main_model.id == nt_alias.c.novel_id,
            )
            stmt = stmt.join(
                t_alias,
                nt_alias.c.tag_id == t_alias.c.id,
            )
            stmt = stmt.where(t_alias.c.name == tag_name)
        return stmt

    # ------------------------------------------------------------------
    # Internal: FTS / keyword filter — WHERE-IN (count) or EXISTS (list)
    # ------------------------------------------------------------------

    def _count_fts_matches(self, fts_query: str) -> int | None:
        """Return the number of FTS rows matching *fts_query*.

        This is a pure FTS5 count (no join to novel), which is very fast
        even for large vocabularies (measured ~0–20 ms on 232k rows).
        """
        try:
            return self.session.execute(
                _text(
                    f"SELECT count(*) FROM {C.TABLE_NOVEL_FTS} "
                    f"WHERE {C.TABLE_NOVEL_FTS} MATCH :fts_query"
                ).bindparams(fts_query=fts_query)
            ).scalar() or 0
        except Exception:
            return None

    def _where_fts_filter(
        self, stmt: Select, keywords: set[str], use_exists: bool = False,
        adaptive: bool = True,
    ) -> Select:
        """Add FTS keyword filter.

        For list queries (``use_exists=True``) the strategy is adaptive:
        a cheap pure-FTS count decides between ``WHERE id IN (...)`` for
        low-selectivity keywords and correlated ``EXISTS`` for popular
        keywords.

        * FTS match count < _ADAPTIVE_KEYWORD_THRESHOLD → IN
        * otherwise → EXISTS

        Pass ``adaptive=False`` to force the legacy EXISTS behaviour.
        """
        if not keywords:
            return stmt

        keyword_string = " ".join(filter(None, keywords))
        if not keyword_string.strip():
            return stmt

        fts_query = self._build_fts_query_string(keyword_string)
        self._fts_query = fts_query

        # Check that the FTS virtual table exists in the database.
        # Result is cached at module level — only probes DB once per process.
        if not _check_fts_available(self.session):
            return stmt

        if use_exists and adaptive:
            fts_count = self._count_fts_matches(fts_query)
            if fts_count is not None and fts_count < _ADAPTIVE_KEYWORD_THRESHOLD:
                use_exists = False

        if use_exists:
            # Use _fts_table (a sqlalchemy.table() reference) instead of
            # _text() for the FROM clause — _text() creates a TextClause
            # which lacks .selectable and crashes the ORM compile state.
            exists_subq = _exists(
                select(literal_column("1"))
                .select_from(_fts_table)
                .where(
                    _text(f"{C.TABLE_NOVEL_FTS} MATCH :fts_query")
                    .bindparams(fts_query=fts_query),
                    _fts_table.c.rowid == self.main_model.id,
                )
            )
            stmt = stmt.where(exists_subq)
        else:
            inner = (
                select(literal_column("rowid"))
                .select_from(_text(C.TABLE_NOVEL_FTS))
                .where(
                    _text(f"{C.TABLE_NOVEL_FTS} MATCH :fts_query")
                    .bindparams(fts_query=fts_query)
                )
            )
            stmt = stmt.where(self.main_model.id.in_(inner))
        return stmt

    # ------------------------------------------------------------------
    # Internal: field filter tables (favourite, special_follow)
    # ------------------------------------------------------------------

    def _join_field_filter_tables(
        self, stmt: Select, field_filters: dict,
        use_exists_for_special_follow: bool = True,
    ) -> Select:
        """Add filters for favourite / special_follow.

        These CANNOT use INNER JOIN because ``_base_select()`` already
        LEFT JOINs the same tables for the display flags (is_favourite /
        is_special_follow CASE expressions).  A second JOIN on the same
        table would produce an ambiguous column reference.

        *is_favourite* filters by novel PK, which is always efficient.

        *is_special_follow* uses EXISTS for list queries (same pattern as
        tag/FTS filters) so SQLite can walk the PK index for ORDER BY +
        LIMIT instead of building a TEMP B-TREE over all matching rows
        (benchmarked: 1.7 ms vs 4.5 ms for the WHERE-IN approach).
        For COUNT queries there is no LIMIT, so EXISTS forces a full scan;
        the caller passes ``use_exists_for_special_follow=False`` to use
        ``author_id IN (SELECT ...)`` instead (measured 187 ms → 3 ms).
        """
        for qtype, _value in field_filters.items():
            if qtype == C.FIELD_IS_FAVOURITE:
                stmt = stmt.where(
                    self.main_model.id.in_(
                        select(models.Favourite.novel_id)
                    )
                )
            elif qtype == C.FIELD_IS_SPECIAL_FOLLOW:
                if use_exists_for_special_follow:
                    stmt = stmt.where(
                        _exists(
                            select(literal_column("1"))
                            .select_from(models.SpecialFollow)
                            .where(
                                models.SpecialFollow.author_id
                                == self.main_model.author_id,
                            )
                        )
                    )
                else:
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
        """Add WHERE conditions for min_like / min_text thresholds.

        Values of 0 are treated as "no threshold": the frontend uses 0 to
        mean 不限, and generating ``like >= 0`` prevents SQLite from using
        the more specific ``(author_id, id)`` / ``(series_id, id)``
        indexes for ORDER BY id.

        Uses bare column comparisons (no COALESCE) so SQLite can do a
        direct index range scan.  The ``novel.like`` column has zero NULL
        values in this dataset, and ``NULL >= 500`` evaluates to NULL
        (falsy) in any case, so COALESCE is unnecessary.
        """
        min_like = self.params.get("min_like")
        min_text = self.params.get("min_text")
        if min_like is not None and min_like > 0:
            stmt = stmt.where(
                self.main_model.like >= min_like
            )
        if min_text is not None and min_text > 0:
            stmt = stmt.where(
                self.main_model.text >= min_text
            )
        return stmt
