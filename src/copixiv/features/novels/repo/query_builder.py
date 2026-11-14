"""Query builder for Novel list, ID, bounded-count, and existence queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, literal_column, select
from sqlalchemy import select as _select
from sqlalchemy import tuple_ as _tuple
from sqlalchemy.orm import Session

from copixiv.core.exceptions import ValidationError
from copixiv.core.services import QuerySpec
from copixiv.db import constants as C
from copixiv.db import models
from copixiv.features.novels.search import keyword_condition


def time_axis_col(model):
    """「按时间」排序键 = ``abs(id)``（docs/LIBRARY_DESIGN.md §3.1）。

    pixiv 行的 ``id`` 就是作品号（发布序）；本地行的 ``id`` 是**负的**时间坐标
    （``-(上传时刻的作品号位置)``，见 ``features/library/local_axis.py``）。
    负号只是「本地」这个来源的标记，时间在**模**上 —— 所以直接把 ``id`` 当时间
    排，本地书会整体被钉在数轴负端（DESC 恒最后 / ASC 恒最前），位置与上传时间
    无关。取绝对值才回到同一根时间轴。

    契约：**凡把 id 当时间用的排序，都要走这个表达式**。反例（故意不换）：
    ``shuffle, id`` 的随机浏览次级键、``check_epub`` 的 ``id > cursor`` 巡检游标
    —— 它们不是时间排序，用裸 id 才是对的。
    """
    return func.abs(model.id)


def blocked_tags_excluded(names):
    """Build a ``NOT (tags && ARRAY[...])`` clause excluding blocked novels.

    Returns None for an empty name set so callers can skip the condition
    entirely (zero overhead when nothing is blocked).
    """
    if not names:
        return None
    return ~models.Novel.tags.overlap(list(names))


def _retired_series_subquery():
    """Series ids pixiv no longer serves — excluded from repair candidates.

    A subquery rather than a materialised Python set: the repair task's
    candidate queries already run inside the caller's transaction, and the
    ledger is tiny (a few dozen rows), so letting PostgreSQL do the anti-join
    keeps the "is this series dead?" rule in exactly one place
    (``series_dead_letter.retired_at IS NOT NULL``) instead of being
    re-implemented at every call site.
    """
    return _select(models.SeriesDeadLetter.series_id).where(
        models.SeriesDeadLetter.retired_at.isnot(None)
    )


_INT_FILTER_COLS = frozenset({
    C.COL_ID, C.COL_AUTHOR_ID, C.COL_SERIES_ID,
    C.COL_LIKES, C.COL_VIEWS, C.COL_TEXTS,
    C.COL_SERIES_INDEX, C.COL_HAS_EPUB,
})
_STR_FILTER_COLS = frozenset({
    C.COL_TITLE, C.COL_AUTHOR_NAME, C.COL_SERIES_NAME, C.COL_SOURCE,
})
_FILTERABLE_NOVEL_FIELDS = _INT_FILTER_COLS | _STR_FILTER_COLS
_SORTABLE_NOVEL_FIELDS = _INT_FILTER_COLS | frozenset({
    C.COL_CREATE_TIME, C.COL_TITLE, C.COL_AUTHOR_NAME,
    C.COL_SERIES_NAME, C.ORDER_BY_RANDOM, C.ORDER_BY_NONE,
})


def _parse_query_int(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise ValidationError(f"Invalid integer for {label}: {raw!r}")
    try:
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid integer for {label}: {raw!r}") from exc


def _parse_cursor_val(order_by: str, raw: Any) -> Any:
    if raw is None:
        raise ValidationError(f"Cursor missing non-null value for {order_by}")
    if order_by in (_INT_FILTER_COLS | {"shuffle"}):
        return _parse_query_int(raw, f"cursor.{order_by}")
    if order_by == C.COL_CREATE_TIME:
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError(
                    f"Invalid datetime cursor for {order_by}: {raw!r}"
                ) from exc
        raise ValidationError(f"Invalid datetime cursor for {order_by}: {raw!r}")
    if not isinstance(raw, str):
        raise ValidationError(f"Invalid string cursor for {order_by}: {raw!r}")
    return raw


class NovelQueryBuilder:
    """Builds single-phase Novel list and count queries (PostgreSQL forms).

    Query structure (conceptual)::

        SELECT novel.*, (SELECT is_special_follow FROM author WHERE author_id=novel.author_id)
        FROM novel
        WHERE novel.tags @> ARRAY[...]                 -- tag filters (AND)
          AND NOT (novel.tags && ARRAY[blocked])       -- blocked exclusion
          AND novel.search_lower LIKE lower(:pN) ...   -- keyword (per segment)
          AND [thresholds / author_id / series_id / cursor]
        ORDER BY ...
        LIMIT ...

    ``is_favourite`` is a direct ``novel`` column; ``is_special_follow`` comes
    from ``author``.  Tag filtering uses ``tags @> ARRAY[...]`` (all names
    present = AND); blocked exclusion uses ``NOT (tags && ...)``.  Keyword
    search is a per-segment substring match against the generated column
    ``novel.search_lower`` (``pg_bigm`` GIN index, migration 0007;
    :func:`copixiv.features.novels.search.keyword_condition`) — no join and no
    separate search table.
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
        self.session: Session = repo.session
        self.main_model = models.Novel
        self.params: dict[str, Any] = {}
        self.repo = repo
        self.spec = spec
        self.ids = ids
        self.restrict_ids = restrict_ids
        self.blocked_tag_names = blocked_tag_names

    # ------------------------------------------------------------------
    # Pagination / ordering
    # ------------------------------------------------------------------

    def _apply_cursor(
        self, stmt: Select, cursor: dict | None, order_by: str,
        order_direction: str = "DESC",
    ) -> Select:
        """Apply cursor-based keyset pagination.

        Uses ``<`` for DESC (next page = smaller values) and ``>`` for ASC
        (next page = larger values).  Secondary-sorts on ``id`` to avoid
        skipping or duplicating rows that share the same sort-column value.
        Row-value tuple comparison works directly in PostgreSQL (ROW...).
        """
        if not cursor:
            return stmt
        if not isinstance(cursor, dict):
            raise ValidationError("Invalid cursor format")
        if "id" not in cursor:
            raise ValidationError("Cursor missing 'id'")
        cursor_id = _parse_query_int(cursor["id"], "cursor.id")

        # Precomputed shuffle column for random ordering — seek on index
        if order_by == "random":
            if "shuffle" not in cursor:
                raise ValidationError("Cursor missing 'shuffle' for random ordering")
            last_shuffle = _parse_query_int(cursor["shuffle"], "cursor.shuffle")
            return stmt.where(
                _tuple(self.main_model.shuffle, self.main_model.id)
                > _tuple(last_shuffle, cursor_id)
            )

        if order_by == C.ORDER_BY_NONE or order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by for cursor pagination: {order_by}")

        if order_by == C.COL_ID:
            # 时间轴：游标里存的仍是身份 id，坐标由 ``abs`` 反推 —— 游标形状
            # 因此保持 ``{"id": N}`` 不变（§3.1）。
            col = time_axis_col(self.main_model)
            cursor_val = abs(cursor_id)
        else:
            if order_by not in cursor:
                raise ValidationError(f"Cursor missing '{order_by}'")
            col = getattr(self.main_model, order_by)
            cursor_val = _parse_cursor_val(order_by, cursor[order_by])

        descending = order_direction.upper() == "DESC"
        if descending:
            return stmt.where(
                _tuple(col, self.main_model.id) < _tuple(cursor_val, cursor_id)
            )
        return stmt.where(
            _tuple(col, self.main_model.id) > _tuple(cursor_val, cursor_id)
        )

    def _apply_ordering(
        self, stmt: Select, order_by: str, order_direction: str,
    ) -> Select:
        """Apply ORDER BY clause."""
        # Precomputed shuffle column — walk index, no temp B-Tree.
        if order_by == "random":
            return stmt.order_by(
                self.main_model.shuffle.asc(), self.main_model.id.asc(),
            )
        if order_by == C.ORDER_BY_NONE:
            return stmt
        if order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by field: {order_by}")

        if order_by == C.COL_ID:
            col = time_axis_col(self.main_model)  # 时间轴：负数 id 取模（§3.1）
        else:
            col = getattr(self.main_model, order_by)
        if order_direction.upper() == "DESC":
            return stmt.order_by(col.desc(), self.main_model.id.desc())
        return stmt.order_by(col.asc(), self.main_model.id.asc())

    def _apply_limit(self, stmt: Select, limit: int) -> Select:
        """Apply LIMIT clause."""
        return stmt.limit(limit)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def build(self) -> tuple[Select, Any]:
        """Build the main list query.

        ``restrict_ids`` is deliberately not applied here (the list surface
        narrows via conditions, not via the count-only id restriction).
        """
        main = self._base_select()
        main = self._apply_filters(main, apply_restrict_ids=False)

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
        """Build an ID-only query with the same filters, without limit."""
        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._apply_filters(stmt, ids=self.ids)
        return stmt.order_by(self.main_model.id)

    def build_ids_in_scope(
        self, novel_ids: list[int], blocked_tag_names: frozenset[str],
    ) -> Select:
        """Build an ID query intersecting *novel_ids* with *spec*, minus blocked."""
        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._apply_filters(
            stmt, ids=novel_ids, blocked_tag_names=blocked_tag_names,
        )
        return stmt.order_by(self.main_model.id)

    def build_count(
        self, *, count_blocked: bool = False,
    ) -> Select | None:
        """Build an exact COUNT(*) query with the same filters, without limit."""
        if not self._has_scope_filters():
            return None

        stmt = select(func.count()).select_from(self.main_model)
        return self._apply_filters(stmt, count_blocked=count_blocked)

    def build_count_bounded(
        self, cap: int, *, count_blocked: bool = False,
    ) -> Select:
        """Build a bounded COUNT: ``SELECT count(*) FROM (SELECT 1 … LIMIT cap+1) x``."""
        inner = select(literal_column("1")).select_from(self.main_model)
        inner = self._apply_filters(inner, count_blocked=count_blocked)
        return select(func.count()).select_from(inner.limit(cap + 1).subquery())

    def build_existence(
        self, *, count_blocked: bool = False,
    ) -> Select | None:
        """Build a ``SELECT 1 ... LIMIT 1`` mirror-predicate existence query."""
        if not self._has_scope_filters():
            return None

        stmt = select(literal_column("1")).select_from(self.main_model)
        stmt = self._apply_filters(stmt, count_blocked=count_blocked)
        return stmt.limit(1)

    def _has_scope_filters(self) -> bool:
        """Whether the spec carries any filter that would narrow a scope."""
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)
        return bool(
            tags or keywords or field_filters
            or self.spec.min_like is not None
            or self.spec.min_text is not None
            or self.spec.exclude_ids
            or self.restrict_ids
            or self.blocked_tag_names
        )

    def _apply_filters(
        self,
        stmt: Select,
        *,
        count_blocked: bool = False,
        ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] | None = None,
        apply_restrict_ids: bool = True,
    ) -> Select:
        """Apply the one shared WHERE chain to *stmt*."""
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

        stmt = self._join_field_filter_tables(stmt, field_filters)
        stmt = self._where_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)

        if ids is not None:
            stmt = stmt.where(self.main_model.id.in_(ids))

        if apply_restrict_ids and self.restrict_ids:
            stmt = stmt.where(self.main_model.id.in_(self.restrict_ids))

        blocked = (
            self.blocked_tag_names
            if blocked_tag_names is None
            else blocked_tag_names
        )
        if blocked:
            cond = models.Novel.tags.overlap(list(blocked))
            stmt = stmt.where(cond if count_blocked else ~cond)

        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            stmt = stmt.where(self.main_model.id.not_in(exclude_ids))
        return stmt

    # ------------------------------------------------------------------
    # Internal: SELECT columns
    # ------------------------------------------------------------------

    def _base_select(self) -> Select:
        """Build the SELECT clause with all novel columns + display flags."""
        cols: list = list(self.main_model.__table__.c)
        sf_subq = (
            select(func.coalesce(models.Author.is_special_follow, False))
            .where(models.Author.author_id == self.main_model.author_id)
            .scalar_subquery()
        )
        cols.append(sf_subq.label(C.FIELD_IS_SPECIAL_FOLLOW))
        return select(*cols).select_from(self.main_model)

    # ------------------------------------------------------------------
    # Internal: filter categorisation
    # ------------------------------------------------------------------

    @staticmethod
    def _categorize(conditions) -> tuple[set, set, dict]:
        """Split an ordered condition list into (tags, keywords, field_filters)."""
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
    # Internal: tag filter — tags @> ARRAY[...] (AND semantics)
    # ------------------------------------------------------------------

    def _where_tag_filter(self, stmt: Select, tag_names: set[str]) -> Select:
        """Add tag filter conditions: ``tags @> ARRAY[names]`` (all present)."""
        if not tag_names:
            return stmt
        return stmt.where(self.main_model.tags.contains(list(tag_names)))

    # ------------------------------------------------------------------
    # Internal: keyword filter — search_lower LIKE (pg_bigm GIN)
    # ------------------------------------------------------------------

    def _where_fts_filter(
        self, stmt: Select, keywords: set[str],
    ) -> Select:
        """Add the keyword filter (per-segment substring match)."""
        if not keywords:
            return stmt

        condition = keyword_condition(" ".join(filter(None, keywords)))
        return stmt if condition is None else stmt.where(condition)

    # ------------------------------------------------------------------
    # Internal: field filter tables (favourite, special_follow)
    # ------------------------------------------------------------------

    def _join_field_filter_tables(
        self, stmt: Select, field_filters: dict,
    ) -> Select:
        """Add filters for ``is_favourite`` / ``is_special_follow``."""
        for qtype, _value in field_filters.items():
            if qtype == C.FIELD_IS_FAVOURITE:
                stmt = stmt.where(self.main_model.is_favourite == True)
            elif qtype == C.FIELD_IS_SPECIAL_FOLLOW:
                stmt = stmt.where(
                    self.main_model.author_id.in_(
                        select(models.Author.author_id).where(
                            models.Author.is_special_follow == True
                        )
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
                continue

            if qtype not in _FILTERABLE_NOVEL_FIELDS:
                raise ValidationError(f"Field is not filterable: {qtype}")

            if value == "":
                continue
            model_field = getattr(self.main_model, qtype)
            typed_val = (
                _parse_query_int(value, qtype)
                if qtype in _INT_FILTER_COLS
                else value
            )
            if qtype in (C.COL_AUTHOR_ID, C.COL_SERIES_ID, C.COL_ID):
                stmt = stmt.where(model_field.in_([typed_val]))
            else:
                stmt = stmt.where(model_field == typed_val)
        return stmt

    # ------------------------------------------------------------------
    # Internal: thresholds
    # ------------------------------------------------------------------

    def _where_thresholds(self, stmt: Select) -> Select:
        """Add WHERE conditions for min_like / min_text thresholds."""
        min_like = self.spec.min_like
        min_text = self.spec.min_text
        if min_like is not None and min_like > 0:
            stmt = stmt.where(self.main_model.like >= min_like)
        if min_text is not None and min_text > 0:
            stmt = stmt.where(self.main_model.text >= min_text)
        return stmt
