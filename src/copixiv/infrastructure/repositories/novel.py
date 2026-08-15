"""Novel repository — full implementation."""

import asyncio
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    select, func, case, and_, update, delete as _delete, text,
)
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C
from copixiv.domain.models.novel import EpubStatus, Novel
from .base import BaseRepository, model_to_dict
from .fts import FTSManager
from .tag import SQLAlchemyTagRepository
from .query_builder import NovelQueryBuilder


class SQLAlchemyNovelRepository(BaseRepository):
    """Repository for novel CRUD and queries."""

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

    # ---- read ----------------------------------------------------------------

    async def get_by_id(self, novel_id: int) -> dict | None:
        novel = self.session.get(models.Novel, novel_id)
        return model_to_dict(novel) if novel else None

    async def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    async def get_novels(
        self,
        queries: dict[str, str] | None = None,
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
        cursor: dict | None = None,
        per_page: int = 50,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> dict:
        """Retrieve a paginated, filtered list of novels.

        Heavy query — executes in a worker thread so the event loop is
        never blocked by SQLite work (tag/FTS subqueries, sorting).
        """
        return await asyncio.to_thread(
            self._get_novels_sync,
            queries, order_by, order_direction, cursor, per_page,
            min_like, min_text,
        )

    def _get_novels_sync(
        self,
        queries: dict[str, str] | None = None,
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
        cursor: dict | None = None,
        per_page: int = 50,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> dict:
        # Validate fields
        if order_by:
            self._validate_query_field(order_by)
        self._validate_order_direction(order_direction)
        if queries:
            for q_type in queries.values():
                self._validate_query_field(q_type)

        # Random browsing — use precomputed shuffle column for fast index seek.
        # First page: pick a random starting point in the shuffle space so
        # each visit shows a different slice.  Wrap around if the tail
        # doesn't have enough rows.
        if order_by == "random" and not queries:
            import random as _random
            if not cursor:
                novels = self._get_random_novels_shuffle(
                    per_page, min_like or 0, min_text or 0,
                )
                cursor_out = None
                if novels and len(novels) >= per_page:
                    last = novels[-1]
                    cursor_out = {
                        "shuffle": last.get("shuffle", 0),
                        "id": last["id"],
                    }
                return {"cursor": cursor_out, "novels": novels}
            # else: has cursor → fall through to query builder below

        params = {
            "queries": queries,
            "order_by": order_by,
            "order_direction": order_direction,
            "cursor": cursor,
            "per_page": per_page + 1,  # +1 to detect if there are more pages
            "min_like": min_like,
            "min_text": min_text,
        }

        builder = NovelQueryBuilder(self, **params)
        query, _query_params = builder.build()

        result = self.session.execute(query)
        novels = [dict(row._mapping) for row in result.fetchall()]

        cursor_out = None
        if len(novels) > per_page:
            n = novels.pop()
            if order_by == "random":
                cursor_out = {
                    "shuffle": n.get("shuffle", 0),
                    "id": n["id"],
                }
            else:
                cursor_out = {"id": n["id"], order_by: n.get(order_by)}

        # Batch-load tags for all returned novels (replaces per-row subquery)
        if novels:
            novel_ids = [n["id"] for n in novels]
            tag_map = self._batch_load_tags(novel_ids)
            for novel in novels:
                novel[C.COL_TAGS] = tag_map.get(novel["id"], [])

        return {"novels": novels, "cursor": cursor_out}

    async def count_novels(
        self,
        queries: dict[str, str] | None = None,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> int:
        """Count novels matching filters (runs in a worker thread)."""
        return await asyncio.to_thread(
            self._count_novels_sync, queries, min_like, min_text,
        )

    def _count_novels_sync(
        self,
        queries: dict[str, str] | None = None,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> int:
        if queries:
            for q_type in queries.values():
                self._validate_query_field(q_type)

        params = {
            "queries": queries or {},
            "min_like": min_like,
            "min_text": min_text,
        }
        builder = NovelQueryBuilder(self, **params)
        count_stmt = builder.build_count()
        if count_stmt is None:
            # No filters — cheap COUNT(*) on the whole table
            result = self.session.execute(
                select(func.count()).select_from(models.Novel)
            )
        else:
            result = self.session.execute(count_stmt)
        return result.scalar()

    # ---- write ---------------------------------------------------------------

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
        from pydantic import BaseModel
        novels = [
            n.model_dump() if isinstance(n, BaseModel) else dict(n)
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

        from copixiv.app.logger import logger
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
        from copixiv.domain.exceptions import NotFoundError

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
        from copixiv.domain.exceptions import NotFoundError

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
        """
        return await asyncio.to_thread(FTSManager(self.session).batch_rebuild_fts)

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

    def _get_random_novels_shuffle(
        self, limit: int, min_likes: int, min_texts: int
    ) -> list[dict]:
        """Return *limit* novels in shuffle order, starting from a random offset.

        Uses the precomputed ``shuffle`` column and its index for O(1)
        keyset-style performance.  A random starting threshold is picked so
        each visit shows a different slice; if the tail doesn't have enough
        rows the query wraps around from ``shuffle >= 0``.

        The composite index ``ix_novel_shuffle_like_text`` (shuffle, like, text)
        allows SQLite to evaluate the like/text filters directly from the index
        without main-table lookups for candidate rows that don't pass.

        Tags, favourite, and special_follow flags are loaded in batch after
        the main query — no per-row correlated subqueries.
        """
        import random as _random

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
            )
            .order_by(models.Novel.shuffle.asc(), models.Novel.id.asc())
            .limit(limit)
        ).scalars().all()
        for novel in rows:
            novels.append(model_to_dict(novel))

        # Wrap around if the tail didn't have enough rows.
        if len(novels) < limit and start > 0:
            remaining = limit - len(novels)
            seen_ids = {n["id"] for n in novels}
            rows = self.session.execute(
                select(models.Novel)
                .where(
                    models.Novel.like >= min_likes,
                    models.Novel.text >= min_texts,
                    models.Novel.shuffle >= 0,
                )
                .order_by(models.Novel.shuffle.asc(), models.Novel.id.asc())
                .limit(remaining + len(seen_ids))
            ).scalars().all()
            for novel in rows:
                nd = model_to_dict(novel)
                if nd["id"] not in seen_ids:
                    novels.append(nd)
                    if len(novels) >= limit:
                        break

        # ---- batch-load tags, favourite, and special_follow flags ---------
        novel_ids = [n["id"] for n in novels]
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
                novel[C.COL_TAGS] = tag_map.get(novel["id"], [])
                novel[C.FIELD_IS_FAVOURITE] = novel["id"] in fav_ids
                novel[C.FIELD_IS_SPECIAL_FOLLOW] = (
                    novel.get(C.COL_AUTHOR_ID) in sf_author_ids
                )

        return novels

    # ---- batch helpers -------------------------------------------------------

    def _batch_load_tags(self, novel_ids: list[int]) -> dict[int, list[str]]:
        """Return a mapping of novel_id → tag name list for the given IDs.

        Replaces the per-row correlated scalar subquery with a single batch
        query — one round-trip instead of N.
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
