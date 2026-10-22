"""Novel data layer — read/write repos, facade, query builder, and series repo.

postgres-migration: the novel_tag/favourite/special_follow join tables are
gone.  Tags live in ``novel.tags text[]`` (+ GIN), ``is_favourite`` is a
``novel`` column, ``is_special_follow`` is an ``author`` column, and keyword
search runs against the ``pg_bigm`` GIN index on the generated column
``novel.search_lower`` (``search_lower LIKE lower(:pN) ...`` — migration 0007;
see ``copixiv.features.novels.search``).
``reference_count`` is maintained by the statement-level ``sync_tag_refs``
trigger (fires only on ``INSERT``/``UPDATE OF tags``/``DELETE``, aggregates
the whole statement via transition tables), deleted
rows cascade to ``failed_novel`` cleanup (explicit — the ledger has no FK),
and ``id = ANY($1)`` / ``tags @>`` / ``NOT (tags && ...)`` replace
the SQLite-era ``IN``/``EXISTS`` adaptive filters and manual DELETE bookkeeping.

Keyword-search query construction lives in ``copixiv.features.novels.search``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    select, select as _select,
    func, or_, Select, update, delete as _delete,
    literal_column, tuple_ as _tuple,
    any_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db import constants as C
from copixiv.core.models import Novel, EpubStatus
from copixiv.core.offload import offload
from copixiv.core.draft import NovelDraft
from copixiv.core.services import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    resolve_active,
    parse_pixiv_time,
)
from copixiv.core.services import QuerySpec
from copixiv.db.base import (
    BaseRepository,
    model_to_dict,
    update_summary,
)
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.features.novels.search import keyword_condition


# =========================================================================
# Query builder — time axis + pagination/ordering
# =========================================================================


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


# =========================================================================
# Query builder — single-phase Novel list/count builder (PostgreSQL)
# =========================================================================


def blocked_tags_excluded(names):
    """Build a ``NOT (tags && ARRAY[...])`` clause excluding blocked novels.

    Returns None for an empty name set so callers can skip the condition
    entirely (zero overhead when nothing is blocked).
    """
    if not names:
        return None
    return ~models.Novel.tags.overlap(list(names))


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
    # Pagination / ordering (this builder has one model, so the helpers
    # live here rather than in a single-subclass base class)
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

        # Precomputed shuffle column for random ordering — seek on index
        if order_by == "random" and "shuffle" in cursor and "id" in cursor:
            last_shuffle = cursor["shuffle"]
            last_id = cursor["id"]
            return stmt.where(
                _tuple(self.main_model.shuffle, self.main_model.id)
                > _tuple(last_shuffle, last_id)
            )

        if order_by == C.COL_ID:
            # 时间轴：游标里存的仍是身份 id，坐标由 ``abs`` 反推 —— 游标形状
            # 因此保持 ``{"id": N}`` 不变（§3.1）。
            col = time_axis_col(self.main_model)
            cursor_val = abs(int(cursor["id"]))
        else:
            col = getattr(self.main_model, order_by, None)
            cursor_val = cursor.get(order_by) if col is not None else None
        if col is not None:
            descending = order_direction.upper() == "DESC"
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

        if order_by == C.COL_ID:
            col = time_axis_col(self.main_model)  # 时间轴：负数 id 取模（§3.1）
        else:
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

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def build(self) -> tuple[Select, dict]:
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
        """Build an ID-only query with the same filters, without limit.

        Ordered by id: callers treat the result as a set (「全选匹配」bulk
        selection), so a deterministic order is not required for correctness —
        but an unordered ID list made the API's output depend on the table's
        physical row order (vacuum/insert churn), which is untestable and
        surprising for clients.  The sort is over the matched id set only.
        """
        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._apply_filters(stmt, ids=self.ids)
        return stmt.order_by(self.main_model.id)

    def build_ids_in_scope(
        self, novel_ids: list[int], blocked_tag_names: frozenset[str],
    ) -> Select:
        """Build an ID query intersecting *novel_ids* with *spec*, minus blocked.

        Ordered by id for the same determinism reason as :meth:`build_ids`
        (the caller's ``match-ids`` response is asserted as a list).
        """
        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._apply_filters(
            stmt, ids=novel_ids, blocked_tag_names=blocked_tag_names,
        )
        return stmt.order_by(self.main_model.id)

    def build_count(
        self, *, count_blocked: bool = False,
    ) -> Select | None:
        """Build an exact COUNT(*) query with the same filters, without limit.

        生产路径不用它（有界计数走 :meth:`build_count_bounded`）；保留是因为
        基准/EXPLAIN 需要「精确 vs 有界」的同谓词对照（见
        ``scripts/bench_pg_query_performance.py``）。Returns None when there
        are no filters (caller can use a cheap ``SELECT COUNT(*) FROM novel``).
        ``count_blocked=True`` counts the novels that *do* carry blocked tags
        (the excluded set) instead of excluding them.
        """
        if not self._has_scope_filters():
            return None

        stmt = select(func.count()).select_from(self.main_model)
        return self._apply_filters(stmt, count_blocked=count_blocked)

    def build_count_bounded(
        self, cap: int, *, count_blocked: bool = False,
    ) -> Select:
        """Build a bounded COUNT: ``SELECT count(*) FROM (SELECT 1 … LIMIT cap+1) x``.

        与 :meth:`build_count` 共享同一套 WHERE（:meth:`_apply_filters`），
        只差两处：

        * **恒返回语句** —— 无过滤 = 全库，同样走有界子查询。PG 凑够 ``cap+1``
          行就停，比精确 ``count(*) FROM novel`` 的全表扫描（24 万行 ~56ms）
          还便宜，并且让「精确/有界」只有一个代码路径。
        * 内层 ``LIMIT cap+1``：取 ``cap+1`` 是为了区分「恰好 cap」与「超过
          cap」—— 外层结果 > cap ⇒ 真实值 > cap（返回值取 cap 并标 truncated，
          见 :meth:`SQLAlchemyNovelReadRepository._count_with_spec`）。
        """
        inner = select(literal_column("1")).select_from(self.main_model)
        inner = self._apply_filters(inner, count_blocked=count_blocked)
        return select(func.count()).select_from(inner.limit(cap + 1).subquery())

    def build_existence(
        self, *, count_blocked: bool = False,
    ) -> Select | None:
        """Build a ``SELECT 1 ... LIMIT 1`` mirror-predicate existence query.

        Shares the exact filter conditions with :meth:`build_count` —
        ``count_blocked=True`` looks for novels that *do* carry blocked tags
        (the excluded set), ``False`` for visible (non-blocked) ones.  The
        caller uses ``execute(stmt).first() is not None`` instead of an
        aggregate COUNT, so PostgreSQL can stop at the first matching row
        (~ms even on big scopes).  Returns None for an unfiltered scope,
        like :meth:`build_count`.
        """
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
        """Apply the one shared WHERE chain to *stmt*.

        The only owner of the filter order: field-filter joins, tag filter,
        keyword filter, field filters, thresholds, then the optional id set,
        ``restrict_ids``, blocked-tag handling, and ``exclude_ids``.  Every
        caller (list / ids / ids-in-scope / count / bounded count / existence)
        goes through here — the four copy-pasted chains had already drifted
        (``build_ids`` ignored ``restrict_ids``, ``build_ids_in_scope`` had no
        ORDER BY).

        Options, not forks:

        * ``count_blocked`` — the blocked-tag clause's positive form
          (``tags && ...``) instead of its negative form, for the "excluded"
          count/existence queries.
        * ``ids`` — ``id IN (ids)`` intersection (``None`` = not applied).
        * ``blocked_tag_names`` — overrides ``self.blocked_tag_names`` when not
          ``None`` (``build_ids_in_scope`` receives the names explicitly).
        * ``apply_restrict_ids`` — whether to narrow to ``self.restrict_ids``
          (the list query intentionally does not).
        """
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
        """Build the SELECT clause with all novel columns + display flags.

        ``is_favourite`` is a ``novel`` column (already in the column set);
        ``is_special_follow`` lives on ``author`` and is read via a scalar
        subquery (the author table is small and per-row indexed by PK).
        Tags are read directly from the ``novel.tags`` column — no batch
        join is needed anymore.
        """
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
        """Add the keyword filter (per-segment substring match).

        The predicate is built by
        :func:`copixiv.features.novels.search.keyword_condition`, which turns
        the keyword into one ``search_lower LIKE lower(:pN)`` per whitespace
        segment (AND-ed) and matches it against the ``pg_bigm`` GIN index on
        the generated column ``novel.search_lower`` (migration 0007).  The
        column is maintained by PostgreSQL from the row itself, so this method
        never has to think about index freshness.

        A keyword that collapses to nothing (e.g. pure punctuation) yields no
        condition — "empty keyword filters nothing", not "matches nothing".
        """
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
        """Add filters for ``is_favourite`` / ``is_special_follow``.

        ``is_favourite`` is a ``novel`` column; ``is_special_follow`` is an
        ``author`` column (filter via ``author_id IN (SELECT author_id FROM
        author WHERE is_special_follow)`` — the author table is small).
        """
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
        min_like = self.spec.min_like
        min_text = self.spec.min_text
        if min_like is not None and min_like > 0:
            stmt = stmt.where(self.main_model.like >= min_like)
        if min_text is not None and min_text > 0:
            stmt = stmt.where(self.main_model.text >= min_text)
        return stmt


# =========================================================================
# Read repository (queries, listing, scopes, blocked-tag exclusion)
# =========================================================================


# 计数上限：`GET /api/novels/count` 只需要区分「到没到上限」，不需要知道 20 万
# 与 1500 万的区别 —— 这个数在前端只有三种用途：判断有没有匹配、判断是否超过
# 同步批量操作上限、显示「≥ N 篇」。所以精确 COUNT 换成有界计数
# ``SELECT count(*) FROM (SELECT 1 FROM novel WHERE <filters> LIMIT cap+1) x``：
# PostgreSQL 凑够 cap+1 行就停，大命中集（R-18 类）从秒级降到百毫秒级，而且
# **快是由构造保证的**，不靠索引恰好合适。
#
# 必须与同步批量操作上限 ``batch_operations.BATCH_MAX_NOVELS`` 同值（超过这个
# 数的计数只表达「超过」）。两处都是字面量而不是 import：``batch_operations``
# 已经 import 本模块，反向 import 会成环；相等关系由
# ``tests/features/test_novel_count_cap.py::test_count_cap_matches_batch_cap``
# 钉死，改单边会红。
COUNT_CAP = 5000


@dataclass(frozen=True)
class NovelCount:
    """有界计数结果：``value`` 加上「它是不是下界」。

    ``truncated=True`` 表示真实匹配数 **大于** ``value``（此时 ``value`` 恒为
    :data:`COUNT_CAP`）——「至少这么多」。忽略该标志拿到的仍是
    ``min(真实值, COUNT_CAP)`` 这个合理数字，而不是 ``cap+1`` 之类的哨兵值，
    所以忘标标志也不会让调用方看到荒谬的数；但要「是否超限」的判断，必须看它。
    """

    value: int
    truncated: bool = False


def _novel_from_orm(obj) -> Novel:
    """Convert an ORM ``Novel`` to the domain :class:`Novel` model.

    ``create_time`` is a ``timestamptz`` (``datetime``) in PostgreSQL and the
    domain model carries it as-is — no conversion.  ``tags`` comes directly
    from the ``novel.tags`` column.
    """
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return Novel(**d)


def _novel_from_mapping(mapping) -> Novel:
    """Convert a row ``RowMapping`` (column-based select) to a domain Novel."""
    d = dict(mapping)
    # ``is_special_follow`` comes from a scalar subquery over ``author``: a
    # novel whose ``author_id`` is NULL (local uploads, migration orphans) or
    # whose author row is gone makes the subquery return *no rows*, and a
    # scalar subquery with no rows is NULL — the ``coalesce`` inside it never
    # runs.  The domain field is a bool, so normalise here (``get_by_id``
    # does the same with ``or False``).
    if C.FIELD_IS_SPECIAL_FOLLOW in d:
        d[C.FIELD_IS_SPECIAL_FOLLOW] = bool(d[C.FIELD_IS_SPECIAL_FOLLOW])
    return Novel(**d)


class SQLAlchemyNovelReadRepository(BaseRepository):
    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }

    async def get_by_id(self, novel_id: int) -> Novel | None:
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return None
        domain = _novel_from_orm(novel)
        domain.is_special_follow = (
            self.session.execute(
                select(models.Author.is_special_follow).where(
                    models.Author.author_id == novel.author_id
                )
            ).scalar() or False
        )
        return domain

    async def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    # ---- blocked-tag exclusion helpers -------------------------------------

    def _exclusion_active(self, explicit: bool | None) -> bool:
        row = self.session.execute(
            select(models.Setting).where(
                models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY
            )
        ).scalar_one_or_none()
        return resolve_active(
            explicit, row.value if row is not None else None,
        )

    def _blocked_tag_names(self) -> frozenset[str]:
        rows = self.session.execute(
            select(models.TagPreference.tag).where(
                models.TagPreference.preference
                == models.TagPreferenceORM.blocked
            )
        ).scalars().all()
        return frozenset(rows)

    def _blocked_novel_ids(self, names: frozenset[str]) -> list[int]:
        """All novel IDs carrying any blocked tag (``tags && ...``)."""
        if not names:
            return []
        return list(self.session.execute(
            select(models.Novel.id).where(
                models.Novel.tags.overlap(list(names))
            )
        ).scalars().all())

    async def list_blocked_ids(self) -> list[int]:
        if not self._exclusion_active(None):
            return []
        return self._blocked_novel_ids(self._blocked_tag_names())

    @offload
    def sort_novel_ids(
        self,
        novel_ids: list[int],
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
    ) -> list[int]:
        """Return *novel_ids* ordered by a novel column (id / like / text).

        Pushes the sort to PostgreSQL: ``WHERE id = ANY($1) ORDER BY <col>``.
        Missing IDs are dropped.  The public coroutine runs it in a worker
        thread (``@offload``).
        """
        if not novel_ids or order_by not in (C.COL_ID, C.COL_LIKES, C.COL_TEXTS):
            return list(novel_ids)

        col = (
            time_axis_col(models.Novel)
            if order_by == C.COL_ID
            else getattr(models.Novel, order_by)
        )
        descending = order_direction.upper() == "DESC"
        stmt = (
            select(models.Novel.id)
            .where(models.Novel.id.in_(novel_ids))
            .order_by(
                col.desc() if descending else col.asc(),
                models.Novel.id.desc() if descending else models.Novel.id.asc(),
            )
        )
        return list(self.session.execute(stmt).scalars())

    @offload
    def get_novels(self, spec: QuerySpec) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*."""
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

        # Random browsing — precomputed shuffle column for fast index seek.
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

        page_spec = spec.model_copy(update={"per_page": spec.per_page + 1})

        builder = NovelQueryBuilder(
            self, page_spec, blocked_tag_names=blocked_names,
        )
        query, _ = builder.build()

        result = self.session.execute(query)
        novels = [_novel_from_mapping(row._mapping) for row in result.fetchall()]

        cursor_out = None
        if len(novels) > spec.per_page:
            # 多取的那一行（per_page+1）只用来判断「还有下一页」，**不能当游标**：
            # 游标的比较是严格 ``<`` / ``>``，拿它当游标会让它自己在下一页被排除
            # ——每翻一页静默丢一本书（2026-09 修，此前 docstring 承诺的
            # 「no missing rows」并没有被断言覆盖）。
            novels.pop()
            last = novels[-1]
            if spec.order_by == "random":
                cursor_out = {"shuffle": last.shuffle, "id": last.id}
            else:
                cursor_out = {
                    "id": last.id,
                    spec.order_by: getattr(last, spec.order_by, None),
                }

        return {"novels": novels, "cursor": cursor_out}

    @offload
    def count_novels(self, spec: QuerySpec) -> NovelCount:
        """Count VISIBLE novels matching *spec* (bounded, worker thread).

        返回 :class:`NovelCount` 而不是裸 int：调用方要能区分「精确值」与
        「≥ COUNT_CAP 的下界」（前者可直接展示，后者只能显示 ``N+``）。
        """
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )
        return self._count_with_spec(spec, blocked_tag_names=blocked_names)

    def _count_with_spec(
        self,
        spec: QuerySpec,
        *,
        restrict_ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] = frozenset(),
        count_blocked: bool = False,
    ) -> NovelCount:
        """有界计数：``min(真实匹配数, COUNT_CAP)`` + 是否被截断。

        单条 SQL，内层多取一行（``LIMIT cap+1``）：外层 > cap 说明真实值超过
        上限，返回值封顶并标 ``truncated``。
        """
        builder = NovelQueryBuilder(
            self, spec,
            restrict_ids=restrict_ids,
            blocked_tag_names=blocked_tag_names,
        )
        stmt = builder.build_count_bounded(
            COUNT_CAP, count_blocked=count_blocked,
        )
        matched = int(self.session.execute(stmt).scalar() or 0)
        if matched > COUNT_CAP:
            return NovelCount(COUNT_CAP, truncated=True)
        return NovelCount(matched)

    @offload
    def count_excluded_novels(self, spec: QuerySpec) -> NovelCount:
        """Count novels matching *spec* that carry blocked tags (同样有界)."""
        if not self._exclusion_active(spec.exclude_blocked_tags):
            return NovelCount(0)
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = self._blocked_tag_names()
        if not blocked_names:
            return NovelCount(0)
        return self._count_with_spec(
            spec, blocked_tag_names=blocked_names, count_blocked=True,
        )

    @offload
    def has_excluded_novels(self, spec: QuerySpec) -> bool:
        """Whether any novel matching *spec* carries blocked tags.

        Mirror-predicate existence query (``SELECT 1 ... LIMIT 1``) instead
        of a full COUNT — powers the ExclusionBar visibility on the first
        page response without an extra request.
        """
        if not self._exclusion_active(spec.exclude_blocked_tags):
            return False
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = self._blocked_tag_names()
        if not blocked_names:
            return False
        builder = NovelQueryBuilder(
            self, spec, blocked_tag_names=blocked_names,
        )
        stmt = builder.build_existence(count_blocked=True)
        if stmt is None:
            return False
        return self.session.execute(stmt).first() is not None

    @offload
    def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated."""
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )
        builder = NovelQueryBuilder(
            self, spec, blocked_tag_names=blocked_names,
        )
        stmt = builder.build_ids()
        return list(self.session.execute(stmt).scalars())

    @offload
    def filter_ids_in_scope(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        """Return the subset of *novel_ids* matching *spec*."""
        if not novel_ids:
            return []
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )
        builder = NovelQueryBuilder(self, spec)
        stmt = builder.build_ids_in_scope(
            novel_ids, blocked_tag_names=blocked_names,
        )
        return list(self.session.execute(stmt).scalars())

    @offload
    def get_novels_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return full novel models for the given IDs, in the given order.

        Missing IDs are silently dropped.
        """
        if not novel_ids:
            return []
        rows = self.session.execute(
            select(models.Novel).where(models.Novel.id.in_(novel_ids))
        ).scalars().all()
        by_id = {n.id: _novel_from_orm(n) for n in rows}

        author_ids = {n.author_id for n in rows if n.author_id}
        if author_ids:
            sf = set(self.session.execute(
                select(models.Author.author_id).where(
                    models.Author.is_special_follow == True,
                    models.Author.author_id.in_(author_ids),
                )
            ).scalars().all())
            for nid, novel in by_id.items():
                novel.is_special_follow = novel.author_id in sf

        return [by_id[nid] for nid in novel_ids if nid in by_id]

    def _get_random_novels_shuffle(
        self, limit: int, min_likes: int, min_texts: int,
        blocked_tag_names: frozenset[str] = frozenset(),
    ) -> list[Novel]:
        """Return *limit* novels in shuffle order, starting from a random offset."""
        import random as _random

        blocked_clause = blocked_tags_excluded(blocked_tag_names)

        max_shuffle = self.session.scalar(
            select(func.coalesce(func.max(models.Novel.shuffle), 0)),
        ) or 0

        novels: list[Novel] = []
        start = _random.randint(0, max_shuffle) if max_shuffle > 0 else 0

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

        # is_special_follow / is_favourite flags.
        novel_ids = [n.id for n in novels]
        author_ids = {n.author_id for n in novels if n.author_id}
        sf: set[int] = set()
        if author_ids:
            sf = set(self.session.execute(
                select(models.Author.author_id).where(
                    models.Author.is_special_follow == True,
                    models.Author.author_id.in_(author_ids),
                )
            ).scalars().all())
        for novel in novels:
            novel.is_special_follow = novel.author_id in sf

        return novels

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

# ``update_metadata_many`` 允许写的列（批量元数据编辑 = 作者 / 系列两组）。
# 白名单是**唯一所有者**：用例只负责把它们拼成 values，仓库层拒绝其余任何列
# （``ValueError``，不是 400 —— 那是"调用方写错了代码"，不是"用户输入不合法"）。
# 它刻意只有五列：``title`` / ``caption`` / ``tags`` / ``create_time`` 各自的
# 批量语义都还没拍板（标签甚至有专门的 set-based 并集/差集路径），别顺手放开。
BATCH_METADATA_COLUMNS = frozenset({
    "author_id", "author_name", "series_id", "series_name", "series_index",
})


class SQLAlchemyNovelWriteRepository(BaseRepository):
    def __init__(self, session: Session):
        super().__init__(session)
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }

    @offload
    def upsert_novels(
        self, novels: list[NovelDraft], force_update: list[str] | None = None
    ) -> int:
        """Insert or update novels and sync their tags."""
        if not novels:
            return 0

        novels = [
            dict(n.__dict__) if hasattr(n, "__dict__") else dict(n)
            for n in novels
        ]

        force_update = force_update or []

        # 0. Ensure author/series placeholder rows satisfy FKs before any
        #    novel insert (PG foreign keys are enforced).
        self._ensure_parent_rows(novels)

        # 1. Resolve tag aliases
        novel_tags_map = self._resolve_tag_aliases(novels)

        # 2. Batch-fetch existing novels
        existing_map = self._fetch_existing_novels(novels)

        # 3. Upsert rows (new rows get their tags inline; existing rows are
        #    updated in step 4 below).
        new_ids = self._upsert_rows(
            novels, existing_map, force_update, novel_tags_map,
        )

        # 4. Merge tags into each novel (popped in _resolve_tag_aliases) and
        #    flush once.  **只增不减**：来源送来的新标签并进去，已有的
        #    （含用户手加的）一律保留 —— 来源删掉一个标签不再意味着库里
        #    也删掉。边界：这只保护新增、不保护删除；用户在编辑器里删掉
        #    "来源也有"的标签，下次刷新会回来，要彻底去掉请用 tag_alias
        #    合并（别名会把来源侧的名字一起改写）。没有新标签时一个字都
        #    不写，避免无谓的 UPDATE 与触发器抖动。搜索索引无需跟进：它是
        #    novel 上的表达式索引，由 PostgreSQL 自己维护。
        for nid, tag_list in novel_tags_map.items():
            novel = existing_map.get(nid)
            if novel is None:
                novel = self.session.get(models.Novel, nid)
            if novel is None:
                continue
            current = set(novel.tags or [])
            if tag_list - current:
                novel.tags = sorted(current | tag_list)
        self.session.flush()

        return len(new_ids)

    def _ensure_parent_rows(self, novels: list[dict]) -> None:
        """Ensure author + series placeholder rows exist before novel writes.

        PostgreSQL enforces FKs (unlike the old SQLite ``PRAGMA
        foreign_keys``-off tolerance), so a first-seen author/series must get a
        placeholder row before the novel that references it is written.
        """
        from copixiv.features.authors.repo import SQLAlchemyAuthorRepository

        author_ids = {n["author_id"] for n in novels if n.get("author_id")}
        series_ids = {n["series_id"] for n in novels if n.get("series_id")}
        if author_ids:
            SQLAlchemyAuthorRepository(self.session).ensure_exists(author_ids)
        if series_ids:
            SQLAlchemySeriesRepository(self.session).ensure_exists(series_ids)

    # ---- upsert helpers -----------------------------------------------------

    def _resolve_tag_aliases(
        self, novels: list[dict],
    ) -> dict[int, set[str]]:
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
        novel_tags_map: dict[int, set[str]] | None = None,
    ) -> list[int]:
        """Insert new or update existing novel rows.

        New rows receive their tag array inline (single insert, no
        separate tag UPDATE); existing rows only ever **gain** tags (step 4
        unions the incoming set into the stored one — see the comment
        there).  Returns the ids of the inserted rows.

        ``update_fields_set`` is the "source-authoritative" whitelist: these
        columns are overwritten on every refresh.  Everything else obeys the
        ``existing is None`` rule on the line below — i.e. **filled once,
        never overwritten** — which is how ``author_name`` / ``series_index``
        / ``create_time`` stay stable (2026-09 拍板：``create_time`` 退出
        无条件覆盖，改由来源补空值）。

        ``has_epub`` stays listed but is only ever truthy when the caller
        actually rewrote the text file this round (the download path sets it
        to ``None`` otherwise → popped above → untouched): the column
        describes the local artifact, so the EPUB producer
        (``update_has_epub_status``) and the reconciler own it.
        """
        update_fields_set = set([
            "like", "view", "title", "text", "caption",
            "series_name",
            "has_epub",
        ] + force_update)

        new_ids: list[int] = []

        for novel in novels:
            filtered = {
                k: v for k, v in novel.items()
                if k in self.VALID_NOVEL_FIELDS
            }
            if filtered.get(C.COL_HAS_EPUB) is None:
                filtered.pop(C.COL_HAS_EPUB, None)
            # create_time is a string from the draft; store as aware UTC datetime.
            if C.COL_CREATE_TIME in filtered and isinstance(
                filtered[C.COL_CREATE_TIME], str
            ):
                filtered[C.COL_CREATE_TIME] = parse_pixiv_time(
                    filtered[C.COL_CREATE_TIME]
                )
            nid = int(novel["id"]) if novel.get("id") is not None else None
            existing = existing_map.get(nid)

            for int_field in ("id", "author_id", "series_id", "series_index"):
                if int_field in filtered and filtered[int_field] is not None:
                    filtered[int_field] = int(filtered[int_field])

            if existing:
                for key, value in filtered.items():
                    if (getattr(existing, key, None) is None and value) or key in update_fields_set:
                        setattr(existing, key, value)
            else:
                if novel_tags_map and nid is not None:
                    normalized_tags = sorted(novel_tags_map.get(nid, []))
                    filtered = {**filtered, "tags": normalized_tags}
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
            f"upsert_novels: {len(new_ids)} new "
            f"(out of {len(novels)} total, {len(all_ids)} IDs queried)"
        )

        return new_ids

    async def delete(self, novel_id: int) -> None:
        """Delete a novel row.

        The ``sync_tag_refs`` trigger decrements ``tag.reference_count`` from
        the deleted row's tags; the search index needs no cleanup (it is an
        expression index over ``novel``).  The ``failed_novel`` ledger
        deliberately has no FK (failures may be recorded for never-persisted
        novels), so its rows for this novel are removed explicitly.
        """
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return
        self.session.execute(
            _delete(models.FailedNovel).where(
                models.FailedNovel.novel_id == novel_id
            )
        )
        self.session.delete(novel)

    async def toggle_favourite(self, novel_id: int) -> None:
        from copixiv.core.exceptions import NotFoundError

        result = self.session.execute(
            update(models.Novel)
            .where(models.Novel.id == novel_id)
            .values(is_favourite=~models.Novel.is_favourite)
            .returning(models.Novel.is_favourite)
        ).scalar_one_or_none()
        if result is None:
            raise NotFoundError(f"Novel {novel_id} not found")

    async def toggle_special_follow(self, author_id: int) -> None:
        from copixiv.core.exceptions import NotFoundError

        result = self.session.execute(
            update(models.Author)
            .where(models.Author.author_id == author_id)
            .values(is_special_follow=~models.Author.is_special_follow)
            .returning(models.Author.is_special_follow)
        ).scalar_one_or_none()
        if result is None:
            raise NotFoundError(f"Author {author_id} not found")

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

    async def update_metadata(self, novel_id: int, values: dict[str, Any]) -> None:
        """单行元数据更新 —— 卡片「编辑」的写路径（docs/NOVEL_EDIT_DESIGN.md §5）。

        刻意与 ``_upsert_rows`` 分开：那条路径带"来源权威"覆盖白名单与"非空者胜"
        规则，是**来源刷新**的语义；用户编辑若混进去，就等于让编辑器继承来源的
        覆盖策略（前置工作 b76e8cd 刚把这两件事拆开）。

        字段白名单与"仅本地书可编辑"这两条业务规则由调用方
        （``UpdateNovelMetadataUseCase``）掌握：仓库层不重复表达业务约束，
        只保证"setattr + flush"这一件事。``tags`` 的 ``tag`` 行与
        ``reference_count`` 由 ``sync_tag_refs`` 语句级触发器维护，无需簿记。
        """
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            # 用例已先行 404；这里是"读到了又被并发删掉"的兜底。
            return
        for field, value in values.items():
            setattr(novel, field, value)
        self.session.flush()

    # ---- batch operations ----------------------------------------------------

    @offload
    def delete_many(self, novel_ids: list[int]) -> list[str]:
        """Delete many novels.

        Returns the ``path`` of each deleted novel (best-effort file cleanup
        is the caller's job).  The ``failed_novel`` ledger is cleaned
        explicitly and the trigger maintains ``reference_count``.
        """
        if not novel_ids:
            return []
        paths = list(self.session.execute(
            select(models.Novel.path).where(models.Novel.id.in_(novel_ids))
        ).scalars().all())
        # failed_novel has no FK by design (failures can be recorded for
        # never-persisted novels) — clean its ledger rows explicitly.
        self.session.execute(
            _delete(models.FailedNovel).where(
                models.FailedNovel.novel_id.in_(novel_ids)
            )
        )
        self.session.execute(
            _delete(models.Novel).where(models.Novel.id.in_(novel_ids))
        )
        return [p for p in paths if p]

    @offload
    def add_tags_to_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Add *tags* to every listed novel.

        The ``sync_tag_refs`` trigger updates ``reference_count``; the search
        index follows automatically (it is an expression index over
        ``novel.tags``).  Returns the number of novels that actually received
        at least one new tag.
        """
        if not novel_ids or not tags:
            return 0
        tag_names = sorted(set(tags))
        self.session.execute(
            pg_insert(models.Tag)
            .values([{"name": t, "reference_count": 0} for t in tag_names])
            .on_conflict_do_nothing(index_elements=["name"])
        )
        # One set-based UPDATE for the whole batch: strip any occurrence of
        # the new tags (dedup) then append them in sorted order.  A single
        # SQL statement means the statement-level tag trigger fires once and
        # aggregates the whole transition table — no per-novel tag row churn.
        expr = models.Novel.tags
        for t in tag_names:
            expr = func.array_append(func.array_remove(expr, t), t)
        changed = list(self.session.execute(
            update(models.Novel)
            .where(
                models.Novel.id.in_(novel_ids),
                ~models.Novel.tags.contains(tag_names),
            )
            .values(tags=expr)
            .returning(models.Novel.id)
        ).scalars().all())
        return len(changed)

    @offload
    def filter_ids_by_source(
        self, novel_ids: list[int], sources: set[str]
    ) -> list[int]:
        """把 id 列表收窄到来源属于 *sources* 的那些（批量编辑只作用于本地书）。

        一条 ``SELECT id FROM novel WHERE id IN (...) AND source IN (...)``：
        ``source`` 那一半走 ``ix_novel_source``、``id`` 那一半走主键，所以代价与
        选择集大小同阶，不读行、不碰任何"来源权威"的写规则。

        ``novel_ids`` 为空直接返回 ``[]``（空选择集里没有"属于某来源"的行）；
        ``sources`` 为空同理——没有任何来源在集合里，匹配集必然为空，不必再问库。

        结果**按 id 升序**：它是预演响应 ``applicable`` 的来源，"哪些书会被改"
        因此是确定的（``build_ids`` 出于同一个理由排序）。调用方要的是集合语义，
        顺序只是可复现性的保证。
        """
        if not novel_ids or not sources:
            return []
        # ``= ANY(:array)`` 而不是 ``IN (...)``：模块 docstring 那句"``id = ANY($1)``
        # 取代 SQLite 时代的 ``IN``"就是这条惯例。批量编辑**没有** 5000 上限
        # （``cap=None``，"全选匹配"可以是全库 20 万级），``IN`` 会把每个元素渲染成
        # 一个绑定参数、拼出数 MB 的 SQL；``ANY`` 只传一个数组参数，代价与元素个数
        # 线性且常数因子小得多。``sources`` 只有一两个值，照旧用 ``IN``。
        stmt = (
            select(models.Novel.id)
            .where(
                models.Novel.id == any_(novel_ids),
                models.Novel.source.in_(sorted(sources)),
            )
            .order_by(models.Novel.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    @offload
    def update_metadata_many(
        self,
        novel_ids: list[int],
        values: dict[str, Any],
        *,
        only_where_null: str | None = None,
    ) -> int:
        """set-based 批量元数据写：一条 UPDATE 覆盖整个 id 列表，返回实际改动的行数。

        与 :meth:`update_metadata`（单行）刻意分开，理由与
        :meth:`add_tags_to_novels` 一致：**一条语句**——行级/语句级触发器只跑
        一次（``sync_tag_refs_batch`` 在语句级聚合整张 transition table），几百本
        的批量编辑不会变成几百次 trigger 调用。所以这里**没有**逐行
        ``get_by_id`` + ``setattr`` 的循环，也**绝不**走 ``_upsert_rows``
        （那是来源刷新的写路径，带"来源权威"覆盖白名单与"非空者胜"规则，见
        :meth:`update_metadata` 的 docstring）。

        Args:
            novel_ids: 目标行 id 列表（调用方已按来源收窄——本层不表达业务约束）。
            values: 列名 → 值，键必须属于 :data:`BATCH_METADATA_COLUMNS`，
                否则 ``ValueError``（调用方拼错了代码，不是用户输入错）。
                ``None`` 表示写 ``NULL``（解除关联）。
            only_where_null: 可选的附加条件——列名，并进 WHERE 的
                ``col IS NULL``。它是"只补空、不覆盖已有值"的 set-based 写法
                （批量解除作者关联时给 ``author_name IS NULL`` 的行补「未知作者」；
                单本语义里这一步是"读这一行的现值再分支"，批量里没有那一行可读）。

        Returns:
            真正被改动的行数（``RETURNING id`` 计数，不看 ``rowcount``）。

        为什么计数带"真的变了"这层收窄：WHERE 里有逐列的
        ``IS DISTINCT FROM``（与 :meth:`SQLAlchemyAuthorRepository.
        update_author_name` / :meth:`SQLAlchemySeriesRepository.
        update_series_name` 同一条规则），所以重复提交同一请求第二次返回 0，
        而不是把没变化的行也算成"改过"。用 ``RETURNING`` 而不是 ``rowcount``
        是因为后者在"匹配到但值相同"时同样会报数，说不了真话。
        """
        if not novel_ids or not values:
            # 空 values 拼不出合法 UPDATE（``SET`` 后面不能空），返回 0 而不是
            # 让 PostgreSQL 报语法错：用例已先行拒掉"两个键都不提交"。
            return 0
        for field in values:
            if field not in BATCH_METADATA_COLUMNS:
                raise ValueError(f"不支持的批量元数据字段: {field}")
        if only_where_null is not None and (
            only_where_null not in BATCH_METADATA_COLUMNS
            or only_where_null not in values
        ):
            raise ValueError(
                f"only_where_null 必须是 values 里的批量元数据列: {only_where_null}"
            )

        # 只写"值真的会变"的行：``OR`` 组进 WHERE，整组与 id 条件相 ``AND``。
        changed_conditions = [
            getattr(models.Novel, field).is_distinct_from(value)
            for field, value in values.items()
        ]
        conditions = [
            # 与 ``filter_ids_by_source`` 同一条理由用 ``ANY(array)``：批量编辑无
            # 上限，``IN`` 会为每个元素渲染一个绑定参数。
            models.Novel.id == any_(novel_ids),
            # 单元素时 `or_` 就渲染成那个条件本身，不必分支。
            or_(*changed_conditions),
        ]
        if only_where_null is not None:
            conditions.append(
                getattr(models.Novel, only_where_null).is_(None)
            )

        changed = list(self.session.execute(
            update(models.Novel)
            .where(*conditions)
            .values(**values)
            .returning(models.Novel.id)
        ).scalars().all())
        return len(changed)

    @offload
    def remove_tags_from_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Remove *tags* from every listed novel.

        Returns the number of novels that actually lost at least one tag.
        """
        if not novel_ids or not tags:
            return 0
        tag_names = sorted(set(tags))
        # One set-based UPDATE: drop every occurrence of the removed tags
        # (arrays are unique, so this is a plain per-tag array_remove chain).
        # Single statement → statement-level trigger aggregates once.
        expr = models.Novel.tags
        for t in tag_names:
            expr = func.array_remove(expr, t)
        changed = list(self.session.execute(
            update(models.Novel)
            .where(
                models.Novel.id.in_(novel_ids),
                models.Novel.tags.overlap(tag_names),
            )
            .values(tags=expr)
            .returning(models.Novel.id)
        ).scalars().all())
        return len(changed)


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
        """INSERT ... ON CONFLICT DO NOTHING placeholder rows (FK safety)."""
        if not series_ids:
            return
        for sid in series_ids:
            self.session.execute(
                pg_insert(models.Series)
                .values(series_id=sid)
                .on_conflict_do_nothing()
            )
        self.session.flush()

    async def get_by_id(self, series_id: int) -> dict | None:
        series = self.session.get(models.Series, series_id)
        if series is None:
            return None
        return model_to_dict(series)

    @offload
    def update_summary(self, series_ids: set[int] | None = None) -> None:
        """Recalculate series aggregates (runs in a worker thread)."""
        update_summary(
            self.session, models.Series, C.COL_SERIES_ID, series_ids,
            extra_columns=[
                func.max(models.Novel.series_name).label(C.COL_SERIES_NAME),
            ],
        )

    async def update_series_name(self, series_id: int, name: str) -> None:
        """写入 ``series`` 行的名字（编辑器的解析结果）。

        刻意**不**像 ``update_author_name`` 那样顺带改写同系列的全部 novel 行：
        作者那边需要回填 ``author_name IS NULL`` 的历史行，而系列名的规范方向是
        反过来的——``update_summary`` 用 ``max(novel.series_name)`` 从小说行推导
        系列行的名字。编辑器只写"本次这一本"（走 ``update_metadata`` 的 values），
        再把这一个系列行命名，两侧就一致了。
        """
        self.session.execute(
            update(models.Series)
            .where(
                models.Series.series_id == series_id,
                models.Series.series_name.is_distinct_from(name),
            )
            .values(series_name=name)
        )

    async def count_novels_by_series(self, series_id: int) -> int:
        """库内已有多少本挂在这个 series_id 下（预览里的"库内已有 N 本"）。

        走 ``ix_novel_series_id``；``series`` 行自己的 ``novel_count`` 是抓取
        管线刷新的聚合值，不是实时计数（与作者预览同一个理由）。
        """
        return int(self.session.execute(
            _select(func.count())
            .select_from(models.Novel)
            .where(models.Novel.series_id == series_id)
        ).scalar_one())

    async def series_with_empty_index(self) -> list[int]:
        return list(self.session.execute(
            _select(models.Novel.series_id)
            .where(
                models.Novel.series_id.isnot(None),
                models.Novel.series_index.is_(None),
            )
            .distinct()
        ).scalars().all())

    # 造 NovelDraft 要用的列，与 ``tasks/maintenance.py::_draft_from_row`` 的底料
    # 一一对应（列名 = NovelDraft 字段名）。修复任务"只改章节号"的 draft 必须以
    # 库内现值为底：``repo._upsert_rows`` 的覆盖白名单（like / view / title /
    # text / caption / series_name）是**无条件写入**的，draft 少哪一列就清哪一列。
    DRAFT_COLUMNS = (
        "id", "title", "author_id", "author_name", "path", "like", "view",
        "text", "caption", "series_id", "series_name", "series_index", "tags",
    )

    @classmethod
    def _draft_columns(cls) -> list:
        return [getattr(models.Novel, name) for name in cls.DRAFT_COLUMNS]

    async def novels_of_series(self, series_id: int) -> list[dict]:
        """库内挂在该系列下的小说（全套现值列，按 ``id`` 升序）。

        名册能算出权威章节号，但 ``_upsert_rows`` 只在"列是 NULL"或"列进了
        ``force_update``"时才写，所以修复任务要先知道每行的现状：号为空的行直接
        补，号已经是错值的行**必须**带 ``force_update`` 才写得进去。返回整行现值
        （而不是只返回 status 列）是为了让调用方能以库内现值为底造 draft——那样
        "只改章节号"就真的只改这一列，名册条目缺列也不会把库内数据清成 NULL。
        """
        return [
            dict(row._mapping)
            for row in self.session.execute(
                _select(*self._draft_columns())
                .where(models.Novel.series_id == series_id)
                .order_by(models.Novel.id)
            ).all()
        ]

    async def novel_columns(self, novel_id: int) -> dict | None:
        """单行的"造 draft"现值列（孤儿分支逐本用），没有这一行则 None。"""
        row = self.session.execute(
            _select(*self._draft_columns()).where(models.Novel.id == novel_id)
        ).one_or_none()
        return dict(row._mapping) if row is not None else None

    async def novels_with_orphan_index(
        self, limit: int | None = None, after_id: int = 0,
    ) -> list[tuple[int, str | None, int | None]]:
        """``series_id`` 为空、``series_index`` 却还留着的"孤儿号"行。

        2026-07-13 之前那版元数据刷新把 ``series_id`` 抹成 NULL、号却留下了
        （生产实测 1,594 行，全部 create_time < 2026-07-13）。这类行
        :meth:`series_with_empty_index` 永远扫不到——它要求 ``series_id IS NOT
        NULL``。按 ``id`` 升序返回 ``(id, series_name, series_index)``，供修复任务
        用游标分批推进。
        """
        stmt = (
            _select(
                models.Novel.id,
                models.Novel.series_name,
                models.Novel.series_index,
            )
            .where(
                models.Novel.series_id.is_(None),
                models.Novel.series_index.isnot(None),
                models.Novel.id > after_id,
            )
            .order_by(models.Novel.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            (int(row[0]), row[1], row[2])
            for row in self.session.execute(stmt).all()
        ]

    async def series_with_stored_index(
        self, after_id: int = 0, limit: int | None = None,
    ) -> list[int]:
        """至少有一本带号的系列（``fix_series_index`` 全量复查模式的候选）。

        与 :meth:`series_with_empty_index` 互补：那边是"有缺号"的系列（1,179 个），
        这边是"号齐全"的系列（25,069 个），并集 = 库里全部 26,248 个系列，且
        互不重叠。全量复查靠它把"号是错值、但一本缺号的都没有"的系列也捞出来
        ——旧任务只扫缺号系列，这类系列的错值（生产实测约 9,780 行）永远没人管。

        单本系列**也在内**：单本系列的号同样可能是错的（名册才是权威，库里只有
        一本不代表 pixiv 只有一本）。按 ``series_id`` 升序（游标推进）。
        """
        stmt = (
            _select(models.Novel.series_id)
            .where(
                models.Novel.series_id.isnot(None),
                models.Novel.series_id > after_id,
            )
            .group_by(models.Novel.series_id)
            .having(func.count(models.Novel.series_index) > 0)
            .order_by(models.Novel.series_id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [int(sid) for sid in self.session.execute(stmt).scalars().all()]
