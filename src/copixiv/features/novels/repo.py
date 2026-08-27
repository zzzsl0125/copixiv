"""Novel data layer — read/write repos, facade, query builder, and series repo.

Merged into one module from the split ``novel_read.py`` / ``novel_write.py``
/ ``novel.py`` (read/write facade) / ``query_builder.py`` /
``query_builder_base.py`` / ``series.py`` (docs/SIMPLIFY_PLAN.md §3 S3,
§5 S1-4a).  ``FTSManager`` moves separately to
``copixiv.features.novels.fts``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import (
    select, select as _select,
    func, case, Select, update, delete as _delete,
    text, text as _text,
    table, column, Integer, literal_column, exists as _exists, tuple_ as _tuple,
)
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copixiv.db import models
from copixiv.db import constants as C
from copixiv.core.models import Novel, EpubStatus
from copixiv.core.services import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    resolve_active,
)
from copixiv.core.services import QuerySpec
from copixiv.db.base import (
    BaseRepository,
    model_to_dict,
    update_summary,
)
from copixiv.db.data_version import current_epoch
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.features.novels.fts import FTSManager

# =========================================================================
# Query builder — base (FTS availability cache, pagination/ordering)
# =========================================================================


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


# =========================================================================
# Query builder — single-phase Novel list/count builder
# =========================================================================

# """Novel query builder — single-phase, filter-driven queries for SQLite.
#
# Key design decisions (v2 rewrite):
# - Single-phase: no nested "filtered_ids subquery → main query" pattern.
#   The old two-phase approach caused ``USE TEMP B-TREE FOR ORDER BY`` on every
#   request because SQLite lost ordering across the subquery boundary.
# - Filter-driven with WHERE-IN: tag and FTS filters produce independent
#   subqueries of novel IDs, used via ``WHERE novel.id IN (...)``.  This lets
#   SQLite use covering indexes (ix_novel_like, idx_novel_author_likes, etc.)
#   for ORDER BY + LIMIT because the outer scan stays on the novel table.
# - JOIN for small tables: favourite/special_follow filters use INNER JOIN
#   since those tables are tiny (67 and 58 rows respectively).
# """


# Lightweight table reference for the FTS5 virtual table so SQLAlchemy's
# ORM compile state can handle subqueries that select from it (TextClause
# lacks a .selectable attribute and causes AttributeError).
_fts_table = table(
    C.TABLE_NOVEL_FTS,
    column("rowid", Integer),
)

# Adaptive filter thresholds (benchmarked on the real 232k-novel database).
# For list queries, low-selectivity tag/keyword filters are faster as
# ``WHERE id IN (...)``, while high-selectivity filters are faster as
# correlated ``EXISTS`` (which can stop early along the ORDER BY index).
# These thresholds sit at the measured crossover point.
_ADAPTIVE_TAG_THRESHOLD = 3000
_ADAPTIVE_KEYWORD_THRESHOLD = 15000

# Blocked-tag exclusion: below this many blocked novels the count is
# computed by restricting on the blocked-id list (PK lookups, ~18ms on
# 232k rows); above it the correlated NOT EXISTS form is faster because
# it short-circuits on the first match (~150ms for a 92%-coverage tag).
_BLOCKED_COUNT_THRESHOLD = 30000


def blocked_tags_not_exists(names):
    """Build a ``NOT EXISTS`` clause excluding novels carrying any of *names*.

    Correlated subquery over ``novel_tag JOIN tag`` — keeps the outer
    query's covering-index walk (ORDER BY + LIMIT early termination)
    intact.  Returns None for an empty name set so callers can skip the
    condition entirely (zero overhead when nothing is blocked).
    """
    if not names:
        return None
    return ~_exists(
        select(literal_column("1"))
        .select_from(models.NovelTag)
        .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
        .where(
            models.NovelTag.novel_id == models.Novel.id,
            models.Tag.name.in_(names),
        )
    )


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

    def __init__(
        self,
        repo,
        spec: QuerySpec,
        *,
        ids: list[int] | None = None,
        restrict_ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] = frozenset(),
    ):
        super().__init__(repo.session, models.Novel)
        self.repo = repo
        self.spec = spec
        # SQL-only inputs — supplied by the repository, not part of the
        # user-facing QuerySpec (docs/MODULARITY.md §M3).
        self.ids = ids
        self.restrict_ids = restrict_ids
        self.blocked_tag_names = blocked_tag_names

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def build(self) -> tuple[Select, dict]:
        """Build the main list query."""
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

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

        # Blocked-tag exclusion (NOT EXISTS; skipped entirely when empty)
        blocked = self.blocked_tag_names
        if blocked:
            main = main.where(blocked_tags_not_exists(blocked))

        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            main = main.where(self.main_model.id.not_in(exclude_ids))

        # Pagination, ordering, limit — applied last so indexes can serve ORDER BY
        main = self._apply_cursor(
            main, self.spec.cursor, self.spec.order_by,
            self.spec.order_direction,
        )
        main = self._apply_ordering(
            main, self.spec.order_by, self.spec.order_direction,
        )
        main = self._apply_limit(main, self.spec.per_page)

        return main, self.spec

    def build_ids(self) -> Select:
        """Build an ID-only query with the same filters, without limit.

        Used by batch operations to resolve the full matching ID set in a
        single lightweight scan (no display-flag JOINs, no column payload).
        """
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._join_field_filter_tables(
            stmt, field_filters, use_exists_for_special_follow=False,
        )
        stmt = self._join_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords, use_exists=False)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)

        # Optional membership constraint: only return IDs from this set
        # (used by match-ids to intersect the selection with the scope).
        id_set = self.ids
        if id_set:
            stmt = stmt.where(self.main_model.id.in_(id_set))

        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            stmt = stmt.where(self.main_model.id.not_in(exclude_ids))
        return stmt

    def build_count(self) -> Select | None:
        """Build a COUNT(*) query with the same filters, without limit.

        Returns None when there are no filters (caller can use a cheap
        ``SELECT COUNT(*) FROM novel``).
        """
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

        has_filters = bool(
            tags or keywords or field_filters
            or self.spec.min_like is not None
            or self.spec.min_text is not None
            or self.spec.exclude_ids
            or self.restrict_ids
            or self.blocked_tag_names
        )
        if not has_filters:
            return None

        stmt = select(func.count()).select_from(self.main_model)
        # COUNT queries: special_follow uses IN instead of EXISTS (avoids
        # a full novel scan).  Tags: JOIN when there are no thresholds
        # (faster for popular tags — 171ms vs 249ms), but EXISTS when
        # thresholds are active so SQLite drives from the small
        # threshold-filtered index instead of the large tag membership set
        # (measured: R-18 + 500/3000 = 202ms → 77ms).
        has_thresholds = (
            (self.spec.min_like is not None and self.spec.min_like > 0)
            or (self.spec.min_text is not None and self.spec.min_text > 0)
        )
        stmt = self._join_field_filter_tables(
            stmt, field_filters, use_exists_for_special_follow=False,
        )
        if tags and has_thresholds:
            stmt = self._where_tag_filter(stmt, tags, use_exists=True)
        else:
            stmt = self._join_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords, use_exists=False)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)

        # Blocked-tag exclusion: restrict to / exclude from a set of ids.
        restrict_ids = self.restrict_ids
        if restrict_ids:
            stmt = stmt.where(self.main_model.id.in_(restrict_ids))
        blocked = self.blocked_tag_names
        if blocked:
            stmt = stmt.where(blocked_tags_not_exists(blocked))

        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            stmt = stmt.where(self.main_model.id.not_in(exclude_ids))
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
    def _categorize(conditions) -> tuple[set, set, dict]:
        """Split an ordered condition list into (tags, keywords, field_filters).

        - ``tags`` / ``keywords`` accumulate (AND semantics — every value
          becomes its own WHERE branch downstream).
        - Scalar field filters keep the LAST value per type: under AND
          semantics two different values for the same column are
          contradictory, and the ordered list makes the winner
          deterministic.
        """
        tags: set[str] = set()
        keywords: set[str] = set()
        field_filters: dict[str, str] = {}
        for qtype, value in conditions:
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
        min_like = self.spec.min_like
        min_text = self.spec.min_text
        if min_like is not None and min_like > 0:
            stmt = stmt.where(
                self.main_model.like >= min_like
            )
        if min_text is not None and min_text > 0:
            stmt = stmt.where(
                self.main_model.text >= min_text
            )
        return stmt


# =========================================================================
# Read repository (queries, listing, scopes, blocked-tag exclusion)
# =========================================================================


# ---------------------------------------------------------------------------
# Count-result cache (process-wide, epoch-validated)
#
# Count queries are expensive on popular tags (186-222 ms for R-18) but
# change only when the novel set is mutated (ingest / delete / tag edit /
# blocked-tag change / favourite toggle).  Writes are sparse relative to
# reads, and the count is already consumed fire-and-forget by the frontend
# (ExclusionBar / BatchBar), so caching gives near-exact freshness at a
# fraction of the cost.
#
# The cache is keyed on a normalized signature of everything that affects
# the count: conditions, thresholds, and the effective blocked-tag set.
# Entries with a non-empty ``exclude_ids`` are not cached (that path is
# batch-scoped and rarely repeated with the same id list).
#
# Freshness is by version, not time: each cached value records the data
# epoch (``current_epoch()``) at write time and is returned only while
# that epoch is still current.  Every committed transaction bumps the
# epoch (see ``copixiv.db.data_version``), which invalidates the whole
# cache in one shot — no per-mutation invalidation, no TTL drift.
# ---------------------------------------------------------------------------
_count_cache: dict[tuple, tuple[int, int]] = {}


def _novel_from_orm(obj) -> Novel:
    """Convert an ORM row to the domain :class:`Novel` model.

    Pydantic coerces the DB int columns (``has_epub`` → EpubStatus,
    display flags → bool); transient fields keep their defaults.
    """
    return Novel(**{c.name: getattr(obj, c.name) for c in obj.__table__.columns})


class SQLAlchemyNovelReadRepository(BaseRepository):
    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.UPDATABLE_NOVEL_FIELDS = list(
            self.VALID_NOVEL_FIELDS - {C.COL_ID, C.COL_INDEX}
        )
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }


    async def get_by_id(self, novel_id: int) -> Novel | None:
        novel = self.session.get(models.Novel, novel_id)
        return _novel_from_orm(novel) if novel else None


    async def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    # ---- blocked-tag exclusion helpers -------------------------------------
    #
    # The *decision* lives in core/services.py (§M4): the
    # repository only supplies the raw setting value and blocked names.

    def _exclusion_active(self, explicit: bool | None) -> bool:
        """Resolve whether blocked-tag exclusion applies to this query.

        *explicit* (from the API ``exclude_blocked`` param) wins when
        given; otherwise the global runtime setting applies (default on
        when the settings row is missing) — decided by the domain policy
        :func:`copixiv.core.services.resolve_active`.
        """
        row = self.session.execute(
            select(models.Setting).where(
                models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY
            )
        ).scalar_one_or_none()
        return resolve_active(
            explicit, row.value if row is not None else None,
        )


    def _blocked_tag_names(self) -> frozenset[str]:
        """Names of user-blocked (厌恶) tags; empty set when none."""
        rows = self.session.execute(
            select(models.TagPreference.tag).where(
                models.TagPreference.preference
                == models.TagPreferenceORM.blocked
            )
        ).scalars().all()
        return frozenset(rows)


    def _blocked_novel_ids(self, names: frozenset[str]) -> list[int]:
        """All novel IDs carrying any blocked tag (index-driven scan)."""
        if not names:
            return []
        return list(self.session.execute(
            select(models.NovelTag.novel_id)
            .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .where(models.Tag.name.in_(names))
        ).scalars().all())


    async def list_blocked_ids(self) -> list[int]:
        """All novel IDs carrying blocked tags; [] when exclusion is off.

        Powers the 「查看被排除」view — the endpoint filters this list
        down to the current search scope.
        """
        if not self._exclusion_active(None):
            return []
        return self._blocked_novel_ids(self._blocked_tag_names())

    # Same value as BATCH_ID_CHUNK_SIZE in batch_operations — kept local
    # so the repository layer doesn't import the use-case module.
    _ID_CHUNK_SIZE = 30_000


    async def sort_novel_ids(
        self,
        novel_ids: list[int],
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
    ) -> list[int]:
        """Return *novel_ids* ordered by a novel column (id / like / text).

        Sort keys are fetched chunked from the novel table and ordered in
        Python — SQLite cannot serve ORDER BY through a large IN-list via
        its indexes.  Unsupported orders (e.g. ``random``) return the
        input order unchanged.  Missing IDs are dropped.  Runs in a worker
        thread (chunked fetches can touch hundreds of thousands of rows).
        """
        return await asyncio.to_thread(
            self._sort_novel_ids_sync, novel_ids, order_by, order_direction,
        )


    def _sort_novel_ids_sync(
        self,
        novel_ids: list[int],
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
    ) -> list[int]:
        if not novel_ids or order_by not in (C.COL_ID, C.COL_LIKES, C.COL_TEXTS):
            return list(novel_ids)

        keys: dict[int, tuple[int, int]] = {}  # id -> (sort_key, id)
        for i in range(0, len(novel_ids), self._ID_CHUNK_SIZE):
            chunk = novel_ids[i:i + self._ID_CHUNK_SIZE]
            rows = self.session.execute(
                select(models.Novel.id, models.Novel.like, models.Novel.text)
                .where(models.Novel.id.in_(chunk))
            ).all()
            for nid, like, text in rows:
                if order_by == C.COL_LIKES:
                    key = like or 0
                elif order_by == C.COL_TEXTS:
                    key = text or 0
                else:
                    key = nid
                keys[nid] = (key, nid)

        reverse = order_direction.upper() == "DESC"
        ordered = [nid for nid, _ in sorted(keys.items(), key=lambda kv: kv[1], reverse=reverse)]
        return ordered


    async def get_novels(self, spec: QuerySpec) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*.

        Heavy query — executes in a worker thread so the event loop is
        never blocked by SQLite work (tag/FTS subqueries, sorting).

        ``spec.exclude_blocked_tags``: None → global setting;
        True/False → override.
        """
        return await asyncio.to_thread(self._get_novels_sync, spec)

    def _get_novels_sync(self, spec: QuerySpec) -> dict:
        # Validate fields
        if spec.order_by:
            self._validate_query_field(spec.order_by)
        self._validate_order_direction(spec.order_direction)
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )

        # Random browsing — use precomputed shuffle column for fast index seek.
        # First page: pick a random starting point in the shuffle space so
        # each visit shows a different slice.  Wrap around if the tail
        # doesn't have enough rows.
        if spec.order_by == "random" and not spec.conditions:
            if not spec.cursor:
                novels = self._get_random_novels_shuffle(
                    spec.per_page, spec.min_like or 0, spec.min_text or 0,
                    blocked_names,
                )
                cursor_out = None
                if novels and len(novels) >= spec.per_page:
                    last = novels[-1]
                    cursor_out = {"shuffle": last.shuffle, "id": last.id}
                return {"cursor": cursor_out, "novels": novels}
            # else: has cursor → fall through to query builder below

        # +1 to detect if there are more pages
        page_spec = spec.model_copy(update={"per_page": spec.per_page + 1})

        builder = NovelQueryBuilder(
            self, page_spec, blocked_tag_names=blocked_names,
        )
        query, _ = builder.build()

        result = self.session.execute(query)
        novels = [Novel(**dict(row._mapping)) for row in result.fetchall()]

        cursor_out = None
        if len(novels) > spec.per_page:
            n = novels.pop()
            if spec.order_by == "random":
                cursor_out = {"shuffle": n.shuffle, "id": n.id}
            else:
                cursor_out = {
                    "id": n.id,
                    spec.order_by: getattr(n, spec.order_by, None),
                }

        # Batch-load tags for all returned novels (replaces per-row subquery)
        if novels:
            novel_ids = [n.id for n in novels]
            tag_map = self._batch_load_tags(novel_ids)
            for novel in novels:
                novel.tags = tag_map.get(novel.id, [])

        return {"novels": novels, "cursor": cursor_out}


    async def count_novels(self, spec: QuerySpec) -> int:
        """Count VISIBLE novels matching *spec* (runs in a worker thread).

        Applies blocked-tag exclusion (unless overridden off) so the
        count matches the list.  ``spec.exclude_blocked_tags``: None →
        global setting; True/False → override.
        """
        return await asyncio.to_thread(self._count_novels_sync, spec)


    def _count_novels_sync(self, spec: QuerySpec) -> int:
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )

        # Cache — skip when exclude_ids is set (batch-scoped, rarely
        # repeated).  blocked_names is part of the key so toggling the
        # exclusion setting or editing blocked tags produces a fresh entry.
        cache_key = None
        if not spec.exclude_ids:
            cache_key = (
                tuple(sorted(spec.conditions)),
                spec.min_like or 0,
                spec.min_text or 0,
                spec.exclude_blocked_tags,
                frozenset(blocked_names),
            )
            hit = _count_cache.get(cache_key)
            if hit is not None:
                cached_epoch, val = hit
                if cached_epoch == current_epoch():
                    return val

        result = self._compute_count(spec, blocked_names)

        if cache_key is not None:
            _count_cache[cache_key] = (current_epoch(), result)
        return result


    def _compute_count(
        self, spec: QuerySpec, blocked_names: frozenset[str],
    ) -> int:
        """The actual count logic, extracted from ``_count_novels_sync``."""
        # No blocked tags — the existing cheap paths unchanged.
        if not blocked_names:
            return self._count_with_spec(spec)

        base_total = self._count_with_spec(spec)
        blocked_ids = self._blocked_novel_ids(blocked_names)
        if not blocked_ids:
            return base_total

        if len(blocked_ids) <= _BLOCKED_COUNT_THRESHOLD:
            # Sparse blocked set: count the blocked∩filters intersection via
            # PK lookups on the blocked-id list (~18ms measured) and subtract.
            excluded = self._count_with_spec(spec, restrict_ids=blocked_ids)
            return base_total - excluded

        # Dense blocked set: correlated NOT EXISTS short-circuits faster
        # (~150-200ms for a 92%-coverage tag vs ~200ms+ for the IN form).
        return self._count_with_spec(spec, blocked_tag_names=blocked_names)


    def _count_with_spec(
        self,
        spec: QuerySpec,
        *,
        restrict_ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] = frozenset(),
    ) -> int:
        """Execute a COUNT built from *spec*; plain COUNT(*) when the
        builder reports no filters (cheap whole-table count)."""
        builder = NovelQueryBuilder(
            self, spec,
            restrict_ids=restrict_ids,
            blocked_tag_names=blocked_tag_names,
        )
        count_stmt = builder.build_count()
        if count_stmt is None:
            result = self.session.execute(
                select(func.count()).select_from(models.Novel)
            )
        else:
            result = self.session.execute(count_stmt)
        return result.scalar()


    async def count_excluded_novels(self, spec: QuerySpec) -> int:
        """Count novels matching *spec* that carry blocked tags.

        Returns 0 when exclusion is off or no tags are blocked.  Powers
        the ``excluded`` field of ``GET /api/novels/count`` so the UI can
        show how many novels were hidden for the current search scope.
        """
        return await asyncio.to_thread(self._count_excluded_novels_sync, spec)


    def _count_excluded_novels_sync(self, spec: QuerySpec) -> int:
        if not self._exclusion_active(spec.exclude_blocked_tags):
            return 0
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = self._blocked_tag_names()
        if not blocked_names:
            return 0
        blocked_ids = self._blocked_novel_ids(blocked_names)
        if not blocked_ids:
            return 0

        if len(blocked_ids) <= _BLOCKED_COUNT_THRESHOLD:
            return self._count_with_spec(spec, restrict_ids=blocked_ids)

        # Dense: total minus visible (both via the builder).
        base_total = self._count_with_spec(spec)
        visible = self._count_with_spec(
            spec, blocked_tag_names=blocked_names,
        )
        return base_total - visible


    async def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated.

        Blocked-tag exclusion is applied as a set difference (much faster
        than per-row NOT EXISTS for full ID scans: ~105ms vs ~634ms).

        Batch operations resolve their scope server-side through this
        lightweight ID-only scan (no column payload, no display-flag JOINs).
        Runs in a worker thread.
        """
        return await asyncio.to_thread(self._list_matching_ids_sync, spec)


    def _list_matching_ids_sync(self, spec: QuerySpec) -> list[int]:
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        builder = NovelQueryBuilder(self, spec)
        stmt = builder.build_ids()
        ids = list(self.session.execute(stmt).scalars())

        return self._apply_blocked_exclusion(ids, spec.exclude_blocked_tags)


    def _apply_blocked_exclusion(
        self, ids: list[int], exclude_blocked_tags: bool | None,
    ) -> list[int]:
        """Subtract blocked-tag novels from *ids* (sorted for determinism)."""
        if not ids or not self._exclusion_active(exclude_blocked_tags):
            return ids
        blocked = set(self._blocked_novel_ids(self._blocked_tag_names()))
        if not blocked:
            return ids
        return sorted(set(ids) - blocked)


    async def filter_ids_in_scope(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        """Return the subset of *novel_ids* matching *spec*.

        Powers the scoped 「清除选择」action — intersect the accumulated
        selection with the current search scope.  Cost is bounded by the
        input ID list, not by the size of the matched set.
        """
        return await asyncio.to_thread(
            self._filter_ids_in_scope_sync, novel_ids, spec,
        )


    def _filter_ids_in_scope_sync(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        if not novel_ids:
            return []
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        builder = NovelQueryBuilder(self, spec, ids=list(novel_ids))
        stmt = builder.build_ids()
        ids = list(self.session.execute(stmt).scalars())

        return self._apply_blocked_exclusion(ids, spec.exclude_blocked_tags)


    async def get_novels_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return full novel models for the given IDs, in the given order.

        Missing IDs are silently dropped.  Tags and display flags are
        batch-loaded exactly like the list-query path.
        """
        return await asyncio.to_thread(self._get_novels_by_ids_sync, novel_ids)


    def _get_novels_by_ids_sync(self, novel_ids: list[int]) -> list[Novel]:
        if not novel_ids:
            return []
        rows = self.session.execute(
            select(models.Novel).where(models.Novel.id.in_(novel_ids))
        ).scalars().all()
        by_id = {n.id: _novel_from_orm(n) for n in rows}

        present_ids = [nid for nid in novel_ids if nid in by_id]
        if present_ids:
            tag_map = self._batch_load_tags(present_ids)
            fav_ids = set(self.session.execute(
                select(models.Favourite.novel_id).where(
                    models.Favourite.novel_id.in_(present_ids)
                )
            ).scalars().all())
            sf_author_ids = set(self.session.execute(
                select(models.SpecialFollow.author_id)
            ).scalars().all())
            for nid in present_ids:
                novel = by_id[nid]
                novel.tags = tag_map.get(nid, [])
                novel.is_favourite = nid in fav_ids
                novel.is_special_follow = novel.author_id in sf_author_ids

        return [by_id[nid] for nid in novel_ids if nid in by_id]


    def _get_random_novels_shuffle(
        self, limit: int, min_likes: int, min_texts: int,
        blocked_tag_names: frozenset[str] = frozenset(),
    ) -> list[dict]:
        """Return *limit* novels in shuffle order, starting from a random offset.

        Uses the precomputed ``shuffle`` column and its index for O(1)
        keyset-style performance.  A random starting threshold is picked so
        each visit shows a different slice; if the tail doesn't have enough
        rows the query wraps around from ``shuffle >= 0``.

        The composite index ``ix_novel_shuffle_like_text`` (shuffle, like, text)
        allows SQLite to evaluate the like/text filters directly from the index
        without main-table lookups for candidate rows that don't pass.

        ``blocked_tag_names`` adds the blocked-tag NOT EXISTS condition to
        both SELECTs — the index seek stays intact; SQLite simply walks on
        past excluded rows until *limit* visible ones are collected.

        Tags, favourite, and special_follow flags are loaded in batch after
        the main query — no per-row correlated subqueries.
        """
        import random as _random

        blocked_clause = blocked_tags_not_exists(blocked_tag_names)

        # Query the max shuffle value so the random start is within range.
        max_shuffle = self.session.scalar(
            select(func.coalesce(func.max(models.Novel.shuffle), 0)),
        ) or 0

        novels: list[dict] = []
        start = _random.randint(0, max_shuffle) if max_shuffle > 0 else 0

        # First attempt: shuffle >= random start — fetch novel entities only,
        # no correlated tags / favourite / sf subqueries.
        rows = self.session.execute(
            select(models.Novel)
            .where(
                models.Novel.like >= min_likes,
                models.Novel.text >= min_texts,
                models.Novel.shuffle >= start,
                *((blocked_clause,) if blocked_clause is not None else ()),
            )
            .order_by(models.Novel.shuffle.asc(), models.Novel.id.asc())
            .limit(limit)
        ).scalars().all()
        for novel in rows:
            novels.append(_novel_from_orm(novel))

        # Wrap around if the tail didn't have enough rows.
        if len(novels) < limit and start > 0:
            remaining = limit - len(novels)
            seen_ids = {n.id for n in novels}
            rows = self.session.execute(
                select(models.Novel)
                .where(
                    models.Novel.like >= min_likes,
                    models.Novel.text >= min_texts,
                    models.Novel.shuffle >= 0,
                    *((blocked_clause,) if blocked_clause is not None else ()),
                )
                .order_by(models.Novel.shuffle.asc(), models.Novel.id.asc())
                .limit(remaining + len(seen_ids))
            ).scalars().all()
            for novel in rows:
                nd = _novel_from_orm(novel)
                if nd.id not in seen_ids:
                    novels.append(nd)
                    if len(novels) >= limit:
                        break

        # ---- batch-load tags, favourite, and special_follow flags ---------
        novel_ids = [n.id for n in novels]
        if novel_ids:
            tag_map = self._batch_load_tags(novel_ids)
            fav_ids = set(self.session.execute(
                select(models.Favourite.novel_id).where(
                    models.Favourite.novel_id.in_(novel_ids)
                )
            ).scalars().all())
            sf_author_ids = set(self.session.execute(
                select(models.SpecialFollow.author_id)
            ).scalars().all())
            for novel in novels:
                novel.tags = tag_map.get(novel.id, [])
                novel.is_favourite = novel.id in fav_ids
                novel.is_special_follow = novel.author_id in sf_author_ids

        return novels

    # ---- batch helpers -------------------------------------------------------


    def _batch_load_tags(self, novel_ids: list[int]) -> dict[int, list[str]]:
        """Return a mapping of novel_id → tag name list for the given IDs.

        Replaces the per-row correlated scalar subquery with a single batch
        query — one round-trip instead of N.  Uses ``|`` as the concat
        separator — safe in practice because Pixiv tag names cannot
        contain it; replace with JSON grouping if that ever changes.
        """
        if not novel_ids:
            return {}
        rows = self.session.execute(
            select(
                models.NovelTag.novel_id,
                func.group_concat(models.Tag.name, '|'),
            )
            .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .where(models.NovelTag.novel_id.in_(novel_ids))
            .group_by(models.NovelTag.novel_id)
        ).all()
        return {
            row[0]: (row[1] or "").split("|") for row in rows
        }

    # ---- helpers -------------------------------------------------------------


    def _validate_query_field(self, field: str) -> None:
        from copixiv.core.exceptions import ValidationError

        if field not in self.VALID_NOVEL_QUERY_FIELDS:
            raise ValidationError(f"Invalid query field: {field}")

    @staticmethod

    def _validate_order_direction(order_direction: str) -> None:
        from copixiv.core.exceptions import ValidationError

        if order_direction.upper() not in ("ASC", "DESC"):
            raise ValidationError(
                f"Invalid order_direction: {order_direction} (expected ASC/DESC)"
            )


# =========================================================================
# Write repository (upserts, deletes, tag/favourite mutations)
# =========================================================================


class SQLAlchemyNovelWriteRepository(BaseRepository):
    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.UPDATABLE_NOVEL_FIELDS = list(
            self.VALID_NOVEL_FIELDS - {C.COL_ID, C.COL_INDEX}
        )
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }


    async def upsert_novels(
        self, novels: list[Novel], force_update: list[str] | None = None
    ) -> int:
        """Insert or update novels, then sync tags and FTS index.

        Accepts :class:`Novel` domain models (canonical write-path object);
        plain dicts are still tolerated for callers that only have row data.

        Heavy write path (alias resolution, batch upsert, tag sync, FTS
        index update) — runs in a worker thread so the event loop is not
        blocked by SQLite write work or busy-timeout waits.
        """
        return await asyncio.to_thread(
            self._upsert_novels_sync, novels, force_update,
        )


    def _upsert_novels_sync(
        self, novels: list[Novel], force_update: list[str] | None = None
    ) -> int:
        """Insert or update novels, then sync tags and FTS index."""
        if not novels:
            return 0

        # Normalize: accept both Novel models and legacy dicts at the edge.
        #
        # Serializer-free conversion: read field values straight from the
        # instance __dict__ instead of model_dump().  pydantic v2's
        # model_dump() routes through __pydantic_serializer__, which
        # pydantic may transiently rebuild — model_rebuild() *deletes and
        # recreates* it and is explicitly "not thread-safe" (see pydantic
        # main.py model_rebuild).  This method runs in a worker thread
        # (asyncio.to_thread), so the rebuild race can leave the serializer
        # as None and model_dump() raises
        # "TypeError: 'None' is not an instance of 'SchemaSerializer'" —
        # the exact failure that took down the 08-19 每日更新/每日排行 cron
        # runs.  Field values are populated at construction (event loop)
        # and never mutated afterwards, so reading __dict__ here is
        # thread-safe and byte-identical to model_dump() for Novel (no
        # private attrs, no computed fields, extra!='allow').
        from pydantic import BaseModel
        novels = [
            dict(n.__dict__) if isinstance(n, BaseModel) else dict(n)
            for n in novels
        ]

        force_update = force_update or []

        # 1. Resolve tag aliases
        novel_tags_map = self._resolve_tag_aliases(novels)

        # 2. Batch-fetch existing novels
        existing_map = self._fetch_existing_novels(novels)

        # 3. Upsert rows
        new_ids, fts_dirty_ids = self._upsert_rows(
            novels, existing_map, force_update,
        )

        # 4. Sync tags
        for nid, tag_list in novel_tags_map.items():
            self.rewrite_tags(nid, set(tag_list))

        # 5. Update FTS index
        fts = FTSManager(self.session)
        fts.update_novel_fts_index(list(set(new_ids + fts_dirty_ids)))

        return len(new_ids)

    # ---- upsert helpers -----------------------------------------------------


    def _resolve_tag_aliases(
        self, novels: list[dict],
    ) -> dict[int, set[str]]:
        """Pop tags from each novel dict and apply alias mapping.

        The canonical key is ``tags`` (``Novel.tags``); legacy callers may
        still pass ``tag`` — both are accepted and popped.
        """
        tag_repo = SQLAlchemyTagRepository(self.session)
        alias_map = tag_repo.get_alias_map_sync()
        novel_tags_map: dict[int, set[str]] = {}
        for n in novels:
            raw_tags = n.pop("tags", None)
            if raw_tags is None:
                raw_tags = n.pop("tag", [])
            mapped_tags = {alias_map.get(t, t) for t in raw_tags}
            nid = n.get("id")
            if nid is not None:
                novel_tags_map[nid] = mapped_tags
        return novel_tags_map


    def _fetch_existing_novels(
        self, novels: list[dict],
    ) -> dict[int, Any]:
        """Return a mapping of novel_id → ORM instance for all IDs in *novels*."""
        all_ids = [int(n["id"]) for n in novels if n.get("id")]
        if not all_ids:
            return {}
        stmt = select(models.Novel).where(models.Novel.id.in_(all_ids))
        return {
            n.id: n
            for n in self.session.execute(stmt).scalars().all()
        }


    def _upsert_rows(
        self,
        novels: list[dict],
        existing_map: dict[int, Any],
        force_update: list[str],
    ) -> tuple[list[int], list[int]]:
        """Insert new or update existing novel rows.

        Returns ``(new_ids, fts_dirty_ids)``.
        """
        update_fields_set = set([
            "like", "view", "title", "text", "caption",
            "series_name", "create_time",
            # Downloaded novels carry a freshly-computed has_epub and must
            # refresh the stored state when the body text changed (e.g.
            # author removed images).  Metadata-only dicts (from
            # build_from_novel_info) do NOT carry this key, so they never
            # touch it — see build_from_novel_info.
            "has_epub",
        ] + force_update)

        new_ids: list[int] = []
        fts_dirty_ids: list[int] = []

        for novel in novels:
            filtered = {
                k: v for k, v in novel.items()
                if k in self.VALID_NOVEL_FIELDS
            }
            # has_epub=None means "don't overwrite" (metadata-only refresh
            # from build_from_novel_info) — drop the key entirely.
            if filtered.get(C.COL_HAS_EPUB) is None:
                filtered.pop(C.COL_HAS_EPUB, None)
            nid = int(novel["id"]) if novel.get("id") is not None else None
            existing = existing_map.get(nid)

            for int_field in ("id", "author_id", "series_id", "series_index"):
                if int_field in filtered and filtered[int_field] is not None:
                    filtered[int_field] = int(filtered[int_field])

            if existing:
                # Detect FTS-relevant changes BEFORE applying them —
                # title/series_name are in *update_fields_set* and get
                # setattr'ed below, so a comparison after that would
                # always see equal values and the FTS index would never
                # be marked dirty on title changes.
                fts_fields = (C.COL_TITLE, C.COL_AUTHOR_NAME, C.COL_SERIES_NAME)
                if nid and any(
                    key in filtered
                    and str(getattr(existing, key, None)) != str(filtered[key])
                    for key in fts_fields
                ):
                    fts_dirty_ids.append(nid)
                for key, value in filtered.items():
                    if (getattr(existing, key, None) is None and value) or key in update_fields_set:
                        setattr(existing, key, value)
            else:
                new_novel = models.Novel(**filtered)
                if "shuffle" not in filtered or not filtered["shuffle"]:
                    import random as _random
                    new_novel.shuffle = _random.randint(0, 2**31 - 1)
                self.session.add(new_novel)
                new_ids.append(novel.get("id"))

        self.session.flush()

        from copixiv.log import logger
        all_ids = [int(n["id"]) for n in novels if n.get("id")]
        logger.info(
            f"upsert_novels: {len(new_ids)} new, {len(fts_dirty_ids)} updated "
            f"(out of {len(novels)} total, {len(all_ids)} IDs queried)"
        )

        return new_ids, fts_dirty_ids


    async def update_field(self, novel_id: int, field: str, value: Any) -> None:
        if field not in self.UPDATABLE_NOVEL_FIELDS:
            raise ValueError(f"Invalid or non-updatable field: {field}")
        novel = self.session.get(models.Novel, novel_id)
        if novel is not None:
            setattr(novel, field, value)


    async def delete(self, novel_id: int) -> None:
        """Delete a novel row, keeping tag reference counts consistent.

        ``rewrite_tags(novel_id, set())`` removes the novel_tag links AND
        decrements the affected tags' ``reference_count`` — without it the
        denormalized counter drifts permanently after every delete.
        """
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return
        self.rewrite_tags(novel_id, set())
        FTSManager(self.session).delete_novel_fts(novel_id)
        self.session.delete(novel)


    async def toggle_favourite(self, novel_id: int) -> None:
        from copixiv.core.exceptions import NotFoundError

        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            raise NotFoundError(f"Novel {novel_id} not found")
        fav = self.session.execute(
            select(models.Favourite).where(
                models.Favourite.novel_id == novel_id
            )
        ).scalar_one_or_none()
        if fav:
            self.session.delete(fav)
        else:
            self.session.add(models.Favourite(novel_id=novel_id))


    async def toggle_special_follow(self, author_id: int) -> None:
        from copixiv.core.exceptions import NotFoundError

        author = self.session.get(models.Author, author_id)
        if author is None:
            raise NotFoundError(f"Author {author_id} not found")
        follow = self.session.execute(
            select(models.SpecialFollow).where(
                models.SpecialFollow.author_id == author_id
            )
        ).scalar_one_or_none()
        if follow:
            self.session.delete(follow)
        else:
            self.session.add(models.SpecialFollow(author_id=author_id))


    async def update_has_epub_status(
        self, novel_ids: list[int], status: EpubStatus
    ) -> None:
        if not novel_ids:
            return
        self.session.execute(
            update(models.Novel)
            .where(models.Novel.id.in_(novel_ids))
            .values(has_epub=status)
        )


    async def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from scratch (runs in a worker thread).

        Returns the number of novels indexed.  Uses the batched rebuild
        path (``FTSManager.batch_rebuild_fts``) — the canonical full
        rebuild for production (maintenance task ``rebuild_fts``).

        Resets the FTS-availability cache: a rebuild that runs while the
        process is alive (e.g. after the table was missing at startup)
        must re-enable keyword filtering immediately, not after restart.
        """
        count = await asyncio.to_thread(
            FTSManager(self.session).batch_rebuild_fts
        )
        reset_fts_cache()
        return count

    # ---- batch operations ----------------------------------------------------


    async def delete_many(self, novel_ids: list[int]) -> list[str]:
        """Delete many novels, keeping tag reference counts and FTS exact.

        Returns the ``path`` of each deleted novel — best-effort file
        cleanup is the caller's job (mirrors :class:`DeleteNovelUseCase`).
        Runs in a worker thread.
        """
        return await asyncio.to_thread(self._delete_many_sync, novel_ids)


    def _delete_many_sync(self, novel_ids: list[int]) -> list[str]:
        if not novel_ids:
            return []
        paths = list(self.session.execute(
            select(models.Novel.path).where(models.Novel.id.in_(novel_ids))
        ).scalars().all())

        # Decrement tag reference counts for every doomed link, then drop
        # the links themselves — per-novel rewrite_tags would be O(N)
        # queries and drift the denormalized counter if not careful.
        link_rows = self.session.execute(
            select(models.NovelTag.tag_id, func.count())
            .where(models.NovelTag.novel_id.in_(novel_ids))
            .group_by(models.NovelTag.tag_id)
        ).all()
        # Benchmarked on the real 232k DB: a per-tag UPDATE loop here costs
        # ~13s fixed per chunk (thousands of distinct tags × 1 statement
        # each).  One temp-table-backed UPDATE collapses it to ~2 statements.
        self._apply_tag_count_deltas({tid: -cnt for tid, cnt in link_rows})
        self.session.execute(
            _delete(models.NovelTag).where(
                models.NovelTag.novel_id.in_(novel_ids)
            )
        )
        self.session.execute(
            _delete(models.Favourite).where(
                models.Favourite.novel_id.in_(novel_ids)
            )
        )
        self.session.execute(
            _delete(models.FailedNovel).where(
                models.FailedNovel.novel_id.in_(novel_ids)
            )
        )
        FTSManager(self.session).delete_novel_fts_many(novel_ids)
        self.session.execute(
            _delete(models.Novel).where(models.Novel.id.in_(novel_ids))
        )
        return [p for p in paths if p]


    async def add_tags_to_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Add *tags* to every listed novel.

        Returns the number of novels that actually received at least one
        new tag.  FTS entries of changed novels are refreshed (tags are
        indexed columns).
        """
        return await asyncio.to_thread(
            self._add_tags_to_novels_sync, novel_ids, tags,
        )


    def _add_tags_to_novels_sync(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        if not novel_ids or not tags:
            return 0

        self.session.execute(
            sqlite_insert(models.Tag)
            .values([{"name": t} for t in tags])
            .on_conflict_do_nothing(index_elements=["name"])
        )
        tag_ids = list(self.session.execute(
            select(models.Tag.id).where(models.Tag.name.in_(tags))
        ).scalars().all())

        existing = set(self.session.execute(
            select(models.NovelTag.novel_id, models.NovelTag.tag_id)
            .where(
                models.NovelTag.novel_id.in_(novel_ids),
                models.NovelTag.tag_id.in_(tag_ids),
            )
        ).all())
        new_pairs = [
            (nid, tid)
            for nid in novel_ids
            for tid in tag_ids
            if (nid, tid) not in existing
        ]
        if not new_pairs:
            return 0

        self.session.execute(
            sqlite_insert(models.NovelTag).values(
                [{"novel_id": nid, "tag_id": tid} for nid, tid in new_pairs]
            )
        )

        per_tag: dict[int, int] = {}
        for _nid, tid in new_pairs:
            per_tag[tid] = per_tag.get(tid, 0) + 1
        self._apply_tag_count_deltas(per_tag)

        changed_ids = sorted({nid for nid, _tid in new_pairs})
        FTSManager(self.session).update_novel_fts_index(changed_ids)
        return len(changed_ids)


    async def remove_tags_from_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Remove *tags* from every listed novel.

        Returns the number of novels that actually lost at least one tag.
        FTS entries of changed novels are refreshed.
        """
        return await asyncio.to_thread(
            self._remove_tags_from_novels_sync, novel_ids, tags,
        )


    def _remove_tags_from_novels_sync(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        if not novel_ids or not tags:
            return 0

        tag_ids = list(self.session.execute(
            select(models.Tag.id).where(models.Tag.name.in_(tags))
        ).scalars().all())
        if not tag_ids:
            return 0

        doomed_pairs = self.session.execute(
            select(models.NovelTag.novel_id, models.NovelTag.tag_id)
            .where(
                models.NovelTag.novel_id.in_(novel_ids),
                models.NovelTag.tag_id.in_(tag_ids),
            )
        ).all()
        if not doomed_pairs:
            return 0

        self.session.execute(
            _delete(models.NovelTag).where(
                models.NovelTag.novel_id.in_(novel_ids),
                models.NovelTag.tag_id.in_(tag_ids),
            )
        )

        per_tag: dict[int, int] = {}
        for _nid, tid in doomed_pairs:
            per_tag[tid] = per_tag.get(tid, 0) + 1
        self._apply_tag_count_deltas({tid: -cnt for tid, cnt in per_tag.items()})

        changed_ids = sorted({nid for nid, _tid in doomed_pairs})
        FTSManager(self.session).update_novel_fts_index(changed_ids)
        return len(changed_ids)


    def _apply_tag_count_deltas(self, deltas: dict[int, int]) -> None:
        """Apply many ``reference_count`` deltas in ~2 statements.

        A per-tag UPDATE loop costs ~1.2ms × distinct-tag-count — the
        measured ~13s fixed overhead of every batch transaction on the
        real 232k DB.  Staging the deltas in a connection-local temp table
        and joining collapses the same work to one UPDATE.
        """
        if not deltas:
            return
        # Lightweight TableClause — a TextClause has no .selectable and
        # crashes the ORM compile state when used as a subquery source.
        deltas_table = table(
            "_tag_count_deltas",
            column("tag_id", Integer),
            column("delta", Integer),
        )
        self.session.execute(
            text(
                "CREATE TEMP TABLE IF NOT EXISTS _tag_count_deltas "
                "(tag_id INTEGER PRIMARY KEY, delta INTEGER)"
            )
        )
        self.session.execute(
            text("DELETE FROM _tag_count_deltas")
        )
        self.session.execute(
            text(
                "INSERT INTO _tag_count_deltas (tag_id, delta) "
                "VALUES (:tag_id, :delta)"
            ),
            [{"tag_id": tid, "delta": d} for tid, d in deltas.items()],
        )
        self.session.execute(
            update(models.Tag)
            .where(models.Tag.id.in_(select(deltas_table.c.tag_id)))
            .values(
                reference_count=models.Tag.reference_count
                + select(deltas_table.c.delta)
                .where(deltas_table.c.tag_id == models.Tag.id)
                .scalar_subquery()
            )
        )

    # ---- tags ----------------------------------------------------------------


    def rewrite_tags(self, novel_id: int, new_tags: set[str]) -> None:
        """Replace a novel's tag set, keeping ``tag.reference_count`` exact.

        Handles the empty-set case through the same decrement path — the
        old early return deleted the links without decrementing the
        counter, so every novel delete permanently inflated the counts.
        """
        existing = set(self.session.execute(
            select(models.Tag.name)
            .join(models.NovelTag, models.Tag.id == models.NovelTag.tag_id)
            .where(models.NovelTag.novel_id == novel_id)
        ).scalars().all())

        to_add = new_tags - existing
        to_remove = existing - new_tags

        if to_remove:
            tag_ids_stmt = select(models.Tag.id).where(
                models.Tag.name.in_(to_remove)
            )
            self.session.execute(
                _delete(models.NovelTag).where(
                    models.NovelTag.novel_id == novel_id,
                    models.NovelTag.tag_id.in_(tag_ids_stmt),
                )
            )
            self._update_tag_ref_count(to_remove, -1)

        if to_add:
            self._add_tags(novel_id, to_add)


    def _add_tags(self, novel_id: int, tags: set[str]) -> None:
        self.session.execute(
            sqlite_insert(models.Tag)
            .values([{"name": t} for t in tags])
            .on_conflict_do_nothing(index_elements=["name"])
        )
        tag_ids = self.session.execute(
            select(models.Tag.id).where(models.Tag.name.in_(tags))
        ).scalars().all()
        if tag_ids:
            self.session.bulk_insert_mappings(
                models.NovelTag,
                [{"novel_id": novel_id, "tag_id": tid} for tid in tag_ids],
            )
        self._update_tag_ref_count(tags, 1)


    def _update_tag_ref_count(self, tags: set[str], delta: int) -> None:
        if not tags:
            return
        self.session.execute(
            update(models.Tag)
            .where(models.Tag.name.in_(tags))
            .values(reference_count=models.Tag.reference_count + delta)
        )

    # ---- random selection ----------------------------------------------------


# =========================================================================
# Facade — read + write combined repository
# =========================================================================


class SQLAlchemyNovelRepository(
    SQLAlchemyNovelReadRepository,
    SQLAlchemyNovelWriteRepository,
):
    """Facade: read + write halves of the novel repository."""


# =========================================================================
# Series repository
# =========================================================================


class SQLAlchemySeriesRepository(BaseRepository):
    """Repository for series CRUD and statistics."""

    def __init__(self, session: Session):
        super().__init__(session)

    def ensure_exists(self, series_ids: set[int]) -> None:
        """INSERT OR IGNORE placeholder rows so FK constraints are satisfied."""
        if not series_ids:
            return
        for sid in series_ids:
            self.session.execute(
                sqlite_insert(models.Series)
                .values(series_id=sid)
                .on_conflict_do_nothing()
            )
        self.session.flush()

    async def get_by_id(self, series_id: int) -> dict | None:
        series = self.session.get(models.Series, series_id)
        if series is None:
            return None
        return model_to_dict(series)

    async def update_summary(self, series_ids: set[int] | None = None) -> None:
        """Recalculate series aggregates (runs in a worker thread)."""
        await asyncio.to_thread(self._update_summary_sync, series_ids)

    def _update_summary_sync(self, series_ids: set[int] | None = None) -> None:
        update_summary(
            self.session, models.Series, C.COL_SERIES_ID, series_ids,
            extra_columns=[
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME),
            ],
        )

    async def get_empty_series_ids(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Series.series_id)
            .where(models.Series.series_name.is_(None))
            .distinct()
        ).scalars().all())

    async def series_with_empty_index(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Novel.series_id)
            .where(
                models.Novel.series_id.isnot(None),
                models.Novel.series_index.is_(None),
            )
            .distinct()
        ).scalars().all())

