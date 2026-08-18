"""Novel READ repository — queries, listing, scopes, blocked-tag exclusion.

Split out of the monolithic ``novel.py`` (docs/MODULARITY.md §M4).
Write operations live in ``novel_write.py``; the facade class
``SQLAlchemyNovelRepository`` (``novel.py``) combines both so
``SqlUnitOfWork`` consumers are unchanged.
"""

import asyncio
import time as _time

from sqlalchemy import select, func
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Count-result TTL cache (process-wide)
#
# Count queries are expensive on popular tags (186-222 ms for R-18) but
# change only when the novel set is mutated (ingest / delete / tag edit /
# blocked-tag change / favourite toggle).  Writes are sparse relative to
# reads, and the count is already consumed fire-and-forget by the frontend
# (ExclusionBar / BatchBar), so a short TTL gives near-exact freshness at
# a fraction of the cost.
#
# The cache is keyed on a normalized signature of everything that affects
# the count: conditions, thresholds, and the effective blocked-tag set.
# Entries with a non-empty ``exclude_ids`` are not cached (that path is
# batch-scoped and rarely repeated with the same id list).
# ---------------------------------------------------------------------------
_COUNT_CACHE_TTL = 60.0          # seconds
_count_cache: dict[tuple, tuple[float, int]] = {}


def invalidate_count_cache() -> None:
    """Drop every cached count (call after any novel-set mutation)."""
    _count_cache.clear()

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from copixiv.domain.models.novel import Novel
from copixiv.domain.services.exclusion import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    resolve_active,
)
from copixiv.domain.services.query_spec import QuerySpec
from .base import BaseRepository
from .query_builder import (
    NovelQueryBuilder,
    _BLOCKED_COUNT_THRESHOLD,
    blocked_tags_not_exists,
)


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
    # The *decision* lives in domain/services/exclusion.py (§M4): the
    # repository only supplies the raw setting value and blocked names.

    def _exclusion_active(self, explicit: bool | None) -> bool:
        """Resolve whether blocked-tag exclusion applies to this query.

        *explicit* (from the API ``exclude_blocked`` param) wins when
        given; otherwise the global runtime setting applies (default on
        when the settings row is missing) — decided by the domain policy
        :func:`copixiv.domain.services.exclusion.resolve_active`.
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

    # Same value as BATCH_ID_CHUNK_SIZE in application.novel — kept local
    # so the repository layer doesn't import the application layer.
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

        # TTL cache — skip when exclude_ids is set (batch-scoped, rarely
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
                ts, val = hit
                if _time.monotonic() - ts < _COUNT_CACHE_TTL:
                    return val

        result = self._compute_count(spec, blocked_names)

        if cache_key is not None:
            _count_cache[cache_key] = (_time.monotonic(), result)
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
        from copixiv.domain.exceptions import ValidationError

        if field not in self.VALID_NOVEL_QUERY_FIELDS:
            raise ValidationError(f"Invalid query field: {field}")

    @staticmethod

    def _validate_order_direction(order_direction: str) -> None:
        from copixiv.domain.exceptions import ValidationError

        if order_direction.upper() not in ("ASC", "DESC"):
            raise ValidationError(
                f"Invalid order_direction: {order_direction} (expected ASC/DESC)"
            )
