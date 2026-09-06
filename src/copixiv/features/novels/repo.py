"""Novel data layer — read/write repos, facade, query builder, and series repo.

postgres-migration: the novel_tag/favourite/special_follow join tables are
gone.  Tags live in ``novel.tags text[]`` (+ GIN), ``is_favourite`` is a
``novel`` column, ``is_special_follow`` is an ``author`` column, and keyword
search runs against the application-maintained ``novel_search`` derived table
(``to_tsvector('simple', search_text) @@ to_tsquery('simple', '<gram>')``).
``reference_count`` is maintained by the statement-level ``sync_tag_refs``
trigger (fires only on ``INSERT``/``UPDATE OF tags``/``DELETE``, aggregates
the whole statement via transition tables), deleted
rows cascade to ``novel_search``/``failed_novel`` via FK ``ON DELETE
CASCADE``, and ``id = ANY($1)`` / ``tags @>`` / ``NOT (tags && ...)`` replace
the SQLite-era ``IN``/``EXISTS`` adaptive filters and manual DELETE bookkeeping.

``FTSManager`` moves separately to ``copixiv.features.novels.fts``.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import (
    select, select as _select,
    func, Select, update, delete as _delete,
    text as _text,
    literal_column, exists as _exists, tuple_ as _tuple,
    event,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db import constants as C
from copixiv.core.models import Novel, EpubStatus
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
from copixiv.db.data_version import current_epoch
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.features.novels.fts import FTSManager, gram_tokenize


def fts_query_to_pg(fts_query: str) -> str:
    """Convert a char-gram FTS query string into a PostgreSQL tsquery phrase.

    ``BaseQueryBuilder._build_fts_query_string`` emits whitespace-joined char
    grams (``哈 利 波 特``), with ``&`` between AND-ed segments.  PostgreSQL's
    phrase syntax wraps each gram phrase in **single** quotes (a
    ``to_tsquery('simple', '<gram>')`` phrase is ``'哈 利 波 特'`` — a
    ``<->`` adjacency phrase).  This converts the unquoted gram text into the
    value bound to ``to_tsquery('simple', :fts_query)``.
    """
    if not fts_query:
        return ""
    phrases = [p.strip() for p in fts_query.split("&") if p.strip()]
    return " & ".join(f"'{p}'" for p in phrases)


# =========================================================================
# Query builder — base (pagination/ordering)
# =========================================================================


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
        """Convert a user keyword string into a char-gram phrase query.

        Char-gram semantics (see :func:`copixiv.features.novels.fts.gram_tokenize`):

        * the input is split on whitespace into segments — whitespace is an
          explicit ``AND`` (mirrors the old "space-separated tokens" UX);
        * a segment made up entirely of non-alphanumeric characters (a
          "pure punctuation" segment such as ``---``) carries no search
          meaning and is dropped — matching the rule that a query that
          collapses to nothing filters nothing;
        * every other segment is char-grammed (``gram_tokenize``), yielding a
          space-separated phrase — a no-space query is therefore an exact
          contiguous-substring match (``哈利波特`` → ``哈 利 波 特``);
        * segments are joined with ``&`` (PostgreSQL tsquery AND); if nothing
          survives the empty contract is preserved (``""`` means "no filter").

        The emitted string carries **no quote characters** — the PostgreSQL
        phrase quoting (single quotes) is applied by :func:`fts_query_to_pg`,
        which the caller binds to ``to_tsquery('simple', ...)``.  (The FTS5
        double-quote form is gone.)
        """
        if not keyword_string.strip():
            return ""

        phrases: list[str] = []
        for seg in keyword_string.split():
            # Drop pure-punctuation segments (e.g. ``---``): they contain no
            # alphanumeric character, so they cannot form a meaningful phrase.
            if not any(ch.isalpha() or ch.isnumeric() for ch in seg):
                continue
            phrases.append(gram_tokenize(seg))

        if not phrases:
            return ""
        return " & ".join(phrases)

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

        col = getattr(self.main_model, order_by, None)
        if col is not None:
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


class NovelQueryBuilder(BaseQueryBuilder):
    """Builds single-phase Novel list and count queries (PostgreSQL forms).

    Query structure (conceptual)::

        SELECT novel.*, (SELECT is_special_follow FROM author WHERE author_id=novel.author_id)
        FROM novel
        WHERE novel.tags @> ARRAY[...]                 -- tag filters (AND)
          AND NOT (novel.tags && ARRAY[blocked])       -- blocked exclusion
          AND to_tsvector('simple', novel_search.search_text) @@ to_tsquery('simple', '...')
          AND [thresholds / author_id / series_id / cursor]
        ORDER BY ...
        LIMIT ...

    ``is_favourite`` is a direct ``novel`` column; ``is_special_follow`` comes
    from ``author``.  Tag filtering uses ``tags @> ARRAY[...]`` (all names
    present = AND); blocked exclusion uses ``NOT (tags && ...)``.  Keyword
    search uses a correlated EXISTS over ``novel_search``.
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

        main = self._base_select()
        main = self._join_field_filter_tables(main, field_filters)
        main = self._where_tag_filter(main, tags)
        main = self._where_fts_filter(main, keywords)
        main = self._where_field_filters(main, field_filters)
        main = self._where_thresholds(main)

        blocked = self.blocked_tag_names
        if blocked:
            main = main.where(blocked_tags_excluded(blocked))

        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            main = main.where(self.main_model.id.not_in(exclude_ids))

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
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._join_field_filter_tables(stmt, field_filters)
        stmt = self._where_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)

        id_set = self.ids
        if id_set:
            stmt = stmt.where(self.main_model.id.in_(id_set))

        if self.blocked_tag_names:
            stmt = stmt.where(
                ~models.Novel.tags.overlap(list(self.blocked_tag_names))
            )

        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            stmt = stmt.where(self.main_model.id.not_in(exclude_ids))
        return stmt

    def build_ids_in_scope(
        self, novel_ids: list[int], blocked_tag_names: frozenset[str],
    ) -> Select:
        """Build an ID query intersecting *novel_ids* with *spec*, minus blocked."""
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

        stmt = select(self.main_model.id).select_from(self.main_model)
        stmt = self._join_field_filter_tables(stmt, field_filters)
        stmt = self._where_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)
        stmt = stmt.where(self.main_model.id.in_(novel_ids))
        if blocked_tag_names:
            stmt = stmt.where(
                ~models.Novel.tags.overlap(list(blocked_tag_names))
            )
        exclude_ids = self.spec.exclude_ids
        if exclude_ids:
            stmt = stmt.where(self.main_model.id.not_in(exclude_ids))
        return stmt

    def build_count(
        self, *, count_blocked: bool = False,
    ) -> Select | None:
        """Build a COUNT(*) query with the same filters, without limit.

        Returns None when there are no filters (caller can use a cheap
        ``SELECT COUNT(*) FROM novel``).  ``count_blocked=True`` counts the
        novels that *do* carry blocked tags (the excluded set) instead of
        excluding them.
        """
        if not self._has_scope_filters():
            return None

        stmt = select(func.count()).select_from(self.main_model)
        return self._apply_scope_filters(stmt, count_blocked=count_blocked)

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
        stmt = self._apply_scope_filters(stmt, count_blocked=count_blocked)
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

    def _apply_scope_filters(
        self, stmt: Select, *, count_blocked: bool = False,
    ) -> Select:
        """Apply the shared filter conditions (tags / keyword / field
        filters / thresholds / blocked exclusion / exclude_ids).

        Shared by the count and existence queries so both stay in lock-step
        with the list query's WHERE clauses.  ``count_blocked=True`` swaps
        the blocked-tag exclusion for its positive form (``tags && ...``).
        """
        conditions = self.spec.conditions
        tags, keywords, field_filters = self._categorize(conditions)

        stmt = self._join_field_filter_tables(stmt, field_filters)
        stmt = self._where_tag_filter(stmt, tags)
        stmt = self._where_fts_filter(stmt, keywords)
        stmt = self._where_field_filters(stmt, field_filters)
        stmt = self._where_thresholds(stmt)

        if self.restrict_ids:
            stmt = stmt.where(self.main_model.id.in_(self.restrict_ids))

        blocked = self.blocked_tag_names
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
    # Internal: FTS / keyword filter — to_tsvector @@ to_tsquery
    # ------------------------------------------------------------------

    def _where_fts_filter(
        self, stmt: Select, keywords: set[str],
    ) -> Select:
        """Add keyword filter via correlated EXISTS over ``novel_search``.

        ``novel_search.search_text = build_search_text(title, author, series, tags)``
        is the char-gram text; ``to_tsvector('simple', search_text) @@
        to_tsquery('simple', '<gram phrase>')`` matches it.  The ``simple``
        tokeniser keeps the ``龖`` placeholder, so punctuation queries work.
        """
        if not keywords:
            return stmt

        keyword_string = " ".join(filter(None, keywords))
        if not keyword_string.strip():
            return stmt

        fts_query = self._build_fts_query_string(keyword_string)
        self._fts_query = fts_query

        # An empty query (e.g. a keyword made entirely of punctuation, or a
        # keyword that collapsed to nothing) means "no filter".
        if not fts_query:
            return stmt

        pg_query = fts_query_to_pg(fts_query)
        exists_subq = _exists(
            select(literal_column("1"))
            .select_from(models.NovelSearch)
            .where(
                models.NovelSearch.novel_id == self.main_model.id,
                _text(
                    "to_tsvector('simple', novel_search.search_text) "
                    "@@ to_tsquery('simple', :fts_query)"
                ).bindparams(fts_query=pg_query),
            )
        )
        return stmt.where(exists_subq)

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


# Count-result cache (process-wide, epoch-validated).  See the original
# design around the "filter signature + epoch invalidation" mechanism.
_count_cache: dict[tuple, tuple[int, int]] = {}

# ---------------------------------------------------------------------------
# Write-back count recompute (single-user nicety, best-effort)
# ---------------------------------------------------------------------------
# Every real write commit bumps the data-version epoch, which invalidates the
# whole count cache; the next count request for a common keyword then pays the
# full slow query again (up to ~2.5s for phrase counts such as ``keyword:R-18``).
# For a single-user deployment the cache key set is small, so recompute all
# cached keys in a background daemon thread right after a dirty commit — by the
# time the user opens batch stats the values are warm again.  Failures are
# swallowed: this is a nicety, never a correctness path.
_last_recompute_epoch: int = 0


def _recompute_count_cache(engine) -> None:
    from copixiv.db.engine import create_session_factory

    try:
        with create_session_factory(engine)() as session:
            repo = SQLAlchemyNovelRepository(session)
            for key in list(_count_cache):
                try:
                    repo._count_novels_sync(QuerySpec(
                        conditions=list(key[0]),
                        min_like=key[1] or None,
                        min_text=key[2] or None,
                        exclude_blocked_tags=key[3],
                    ))
                except Exception:
                    pass  # one stale key must not abort the sweep
    except Exception:
        pass  # best-effort: recompute failures are invisible to the app


@event.listens_for(Session, "after_commit")
def _recompute_count_cache_after_write(session) -> None:
    """Re-warm cached counts after a real (epoch-bumping) commit.

    Registered here (module level) so every write path — repository, tasks,
    scripts — gets the re-warm without touching each call site.  The epoch
    guard keeps read-only commits (which also fire ``after_commit``) from
    spawning threads.
    """
    global _last_recompute_epoch
    epoch = current_epoch()
    if epoch == _last_recompute_epoch or not _count_cache:
        return
    _last_recompute_epoch = epoch
    threading.Thread(
        target=_recompute_count_cache, args=(session.get_bind(),),
        name="copixiv-count-recompute", daemon=True,
    ).start()


def _novel_from_orm(obj) -> Novel:
    """Convert an ORM ``Novel`` to the domain :class:`Novel` model.

    ``create_time`` is a ``timestamptz`` (``datetime``) in PostgreSQL; the
    domain model keeps a string contract, so it is converted to an ISO string.
    ``tags`` comes directly from the ``novel.tags`` column.
    """
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    ct = d.get(C.COL_CREATE_TIME)
    if isinstance(ct, datetime):
        d[C.COL_CREATE_TIME] = ct.isoformat()
    return Novel(**d)


def _novel_from_mapping(mapping) -> Novel:
    """Convert a row ``RowMapping`` (column-based select) to a domain Novel."""
    d = dict(mapping)
    ct = d.get(C.COL_CREATE_TIME)
    if isinstance(ct, datetime):
        d[C.COL_CREATE_TIME] = ct.isoformat()
    return Novel(**d)


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

    async def sort_novel_ids(
        self,
        novel_ids: list[int],
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
    ) -> list[int]:
        """Return *novel_ids* ordered by a novel column (id / like / text).

        Pushes the sort to PostgreSQL: ``WHERE id = ANY($1) ORDER BY <col>``.
        Missing IDs are dropped.  Runs in a worker thread.
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

        col = getattr(models.Novel, order_by)
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

    async def get_novels(self, spec: QuerySpec) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*."""
        return await asyncio.to_thread(self._get_novels_sync, spec)

    def _get_novels_sync(self, spec: QuerySpec) -> dict:
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
            n = novels.pop()
            if spec.order_by == "random":
                cursor_out = {"shuffle": n.shuffle, "id": n.id}
            else:
                cursor_out = {
                    "id": n.id,
                    spec.order_by: getattr(n, spec.order_by, None),
                }

        return {"novels": novels, "cursor": cursor_out}

    async def count_novels(self, spec: QuerySpec) -> int:
        """Count VISIBLE novels matching *spec* (runs in a worker thread)."""
        return await asyncio.to_thread(self._count_novels_sync, spec)

    def _count_novels_sync(self, spec: QuerySpec) -> int:
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )

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
        return self._count_with_spec(spec, blocked_tag_names=blocked_names)

    def _count_with_spec(
        self,
        spec: QuerySpec,
        *,
        restrict_ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] = frozenset(),
        count_blocked: bool = False,
    ) -> int:
        builder = NovelQueryBuilder(
            self, spec,
            restrict_ids=restrict_ids,
            blocked_tag_names=blocked_tag_names,
        )
        count_stmt = builder.build_count(count_blocked=count_blocked)
        if count_stmt is None:
            result = self.session.execute(
                select(func.count()).select_from(models.Novel)
            )
        else:
            result = self.session.execute(count_stmt)
        return result.scalar()

    async def count_excluded_novels(self, spec: QuerySpec) -> int:
        """Count novels matching *spec* that carry blocked tags."""
        return await asyncio.to_thread(self._count_excluded_novels_sync, spec)

    def _count_excluded_novels_sync(self, spec: QuerySpec) -> int:
        if not self._exclusion_active(spec.exclude_blocked_tags):
            return 0
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)

        blocked_names = self._blocked_tag_names()
        if not blocked_names:
            return 0
        return self._count_with_spec(
            spec, blocked_tag_names=blocked_names, count_blocked=True,
        )

    async def has_excluded_novels(self, spec: QuerySpec) -> bool:
        """Whether any novel matching *spec* carries blocked tags.

        Mirror-predicate existence query (``SELECT 1 ... LIMIT 1``) instead
        of a full COUNT — powers the ExclusionBar visibility on the first
        page response without an extra request.
        """
        return await asyncio.to_thread(self._has_excluded_novels_sync, spec)

    def _has_excluded_novels_sync(self, spec: QuerySpec) -> bool:
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

    async def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated."""
        return await asyncio.to_thread(self._list_matching_ids_sync, spec)

    def _list_matching_ids_sync(self, spec: QuerySpec) -> list[int]:
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

    async def filter_ids_in_scope(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        """Return the subset of *novel_ids* matching *spec*."""
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

    async def get_novels_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return full novel models for the given IDs, in the given order.

        Missing IDs are silently dropped.
        """
        return await asyncio.to_thread(self._get_novels_by_ids_sync, novel_ids)

    def _get_novels_by_ids_sync(self, novel_ids: list[int]) -> list[Novel]:
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
        self, novels: list[NovelDraft], force_update: list[str] | None = None
    ) -> int:
        """Insert or update novels, then sync tags and the ``novel_search`` index."""
        return await asyncio.to_thread(
            self._upsert_novels_sync, novels, force_update,
        )

    def _upsert_novels_sync(
        self, novels: list[NovelDraft], force_update: list[str] | None = None
    ) -> int:
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
        new_ids, fts_dirty_ids = self._upsert_rows(
            novels, existing_map, force_update, novel_tags_map,
        )

        # 4. Set the tags array on each novel (popped in _resolve_tag_aliases),
        #    flush once, then refresh novel_search for all affected ids.
        changed_ids: set[int] = set()
        for nid, tag_list in novel_tags_map.items():
            novel = existing_map.get(nid)
            if novel is None:
                novel = self.session.get(models.Novel, nid)
            if novel is not None:
                normalized = sorted(set(tag_list))
                if list(novel.tags or []) != normalized:
                    novel.tags = normalized
                    changed_ids.add(nid)
        self.session.flush()

        # 5. Update novel_search index
        fts = FTSManager(self.session)
        all_dirty = set(new_ids) | set(fts_dirty_ids) | changed_ids
        if all_dirty:
            fts.update_novel_fts_index(list(all_dirty))

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
    ) -> tuple[list[int], list[int]]:
        """Insert new or update existing novel rows.

        New rows receive their tag array inline (single insert, no
        separate tag UPDATE); existing rows keep their tags unless step 4
        detects a real change.  Returns ``(new_ids, fts_dirty_ids)``.
        """
        update_fields_set = set([
            "like", "view", "title", "text", "caption",
            "series_name", "create_time",
            "has_epub",
        ] + force_update)

        new_ids: list[int] = []
        fts_dirty_ids: list[int] = []

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
        """Delete a novel row.

        The ``sync_tag_refs`` trigger decrements ``tag.reference_count`` from
        the deleted row's tags; the ``novel_search`` FK ``ON DELETE CASCADE``
        drops its search row.  The ``failed_novel`` ledger deliberately has
        no FK (failures may be recorded for never-persisted novels), so its
        rows for this novel are removed explicitly.
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

    async def rebuild_fts(self) -> int:
        """Rebuild the ``novel_search`` derived table from scratch.

        Returns the number of novels indexed.  Uses ``FTSManager.batch_rebuild_fts``.
        """
        count = await asyncio.to_thread(
            FTSManager(self.session).batch_rebuild_fts
        )
        return count

    # ---- batch operations ----------------------------------------------------

    async def delete_many(self, novel_ids: list[int]) -> list[str]:
        """Delete many novels.

        Returns the ``path`` of each deleted novel (best-effort file cleanup
        is the caller's job).  Deletion cascades to ``novel_search`` /
        ``failed_novel`` and the trigger maintains ``reference_count``.
        """
        return await asyncio.to_thread(self._delete_many_sync, novel_ids)

    def _delete_many_sync(self, novel_ids: list[int]) -> list[str]:
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

    async def add_tags_to_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Add *tags* to every listed novel.

        The ``sync_tag_refs`` trigger updates ``reference_count``; changed
        ``novel_search`` rows are refreshed (tags are a search segment).
        Returns the number of novels that actually received at least one new tag.
        """
        return await asyncio.to_thread(
            self._add_tags_to_novels_sync, novel_ids, tags,
        )

    def _add_tags_to_novels_sync(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
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
        if changed:
            self.session.flush()
            FTSManager(self.session).update_novel_fts_index(changed)
        return len(changed)

    async def remove_tags_from_novels(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
        """Remove *tags* from every listed novel.

        Returns the number of novels that actually lost at least one tag.
        """
        return await asyncio.to_thread(
            self._remove_tags_from_novels_sync, novel_ids, tags,
        )

    def _remove_tags_from_novels_sync(
        self, novel_ids: list[int], tags: set[str]
    ) -> int:
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
        if changed:
            self.session.flush()
            FTSManager(self.session).update_novel_fts_index(changed)
        return len(changed)

    def rewrite_tags(self, novel_id: int, new_tags: set[str]) -> None:
        """Replace a novel's tag set, keeping ``tag.reference_count`` exact.

        The ``sync_tag_refs`` trigger maintains ``reference_count`` from the
        new array; ``novel_search`` is refreshed (tags are a search segment).
        """
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return
        normalized = sorted(set(new_tags))
        if list(novel.tags or []) != normalized:
            novel.tags = normalized
            self.session.flush()
            FTSManager(self.session).update_novel_fts_index([novel_id])


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
