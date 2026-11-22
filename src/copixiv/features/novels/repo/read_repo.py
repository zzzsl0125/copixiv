"""Novel read repository backed exclusively by the in-process Rust query engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_PIXIV, EpubStatus, Novel
from copixiv.core.search import QuerySpec, normalize_search_segments
from copixiv.core.time import datetime_to_us, us_to_datetime
from copixiv.db import constants as C
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository
from copixiv.query_engine.port import (
    EngineCursor,
    EngineNovelRecord,
    EngineQuery,
    NovelQueryEnginePort,
)

# Bounded count cap for list/count endpoints.
COUNT_CAP = 5000

_INT_FILTER_COLS = frozenset(
    {
        C.COL_ID,
        C.COL_AUTHOR_ID,
        C.COL_SERIES_ID,
        C.COL_LIKES,
        C.COL_VIEWS,
        C.COL_TEXTS,
        C.COL_SERIES_INDEX,
        C.COL_HAS_EPUB,
    }
)
_STR_FILTER_COLS = frozenset(
    {
        C.COL_TITLE,
        C.COL_AUTHOR_NAME,
        C.COL_SERIES_NAME,
        C.COL_SOURCE,
    }
)
_FILTERABLE_NOVEL_FIELDS = _INT_FILTER_COLS | _STR_FILTER_COLS
_SORTABLE_NOVEL_FIELDS = _INT_FILTER_COLS | frozenset(
    {
        C.COL_CREATE_TIME,
        C.COL_TITLE,
        C.COL_AUTHOR_NAME,
        C.COL_SERIES_NAME,
        C.ORDER_BY_RANDOM,
    }
)
_VALID_CONDITION_FIELDS = _FILTERABLE_NOVEL_FIELDS | frozenset(
    {
        C.FIELD_TAGS,
        C.FIELD_KEYWORD,
        C.FIELD_IS_FAVOURITE,
        C.FIELD_IS_SPECIAL_FOLLOW,
    }
)


@dataclass(frozen=True)
class NovelCount:
    """Bounded count result: ``value`` paired with truncation flag."""

    value: int
    truncated: bool = False


def _parse_query_int(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise ValidationError(f"Invalid integer for {label}: {raw!r}")
    try:
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid integer for {label}: {raw!r}") from exc


def _parse_cursor_val(order_by: str, raw: Any) -> Any:
    if raw is None:
        if order_by == C.COL_CREATE_TIME:
            return None
        raise ValidationError(f"Cursor missing non-null value for {order_by}")
    if order_by in _INT_FILTER_COLS:
        return _parse_query_int(raw, f"cursor.{order_by}")
    if order_by == C.COL_CREATE_TIME:
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError(f"Invalid datetime cursor for {order_by}: {raw!r}") from exc
        raise ValidationError(f"Invalid datetime cursor for {order_by}: {raw!r}")
    if not isinstance(raw, str):
        raise ValidationError(f"Invalid string cursor for {order_by}: {raw!r}")
    return raw


def _categorize_conditions(conditions) -> tuple[set[str], set[str], dict[str, str]]:
    """Split an ordered condition list into ``(tags, keywords, field_filters)``."""
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


def _novel_from_record(rec: EngineNovelRecord) -> Novel:
    """Materialize a card-projection :class:`Novel` directly from Rust ``NovelRecord``."""
    return Novel.model_construct(
        id=rec.id,
        title=rec.title,
        author_id=rec.author_id,
        author_name=rec.author_name,
        path=None,
        like=rec.like,
        view=rec.view,
        text=rec.text,
        caption=None,
        series_id=rec.series_id,
        series_name=rec.series_name,
        series_index=rec.series_index,
        create_time=us_to_datetime(rec.create_time_us),
        has_epub=EpubStatus(rec.has_epub),
        source=rec.source,
        source_key=None,
        tags=list(rec.tags),
        is_favourite=rec.is_favourite,
        is_special_follow=rec.is_special_follow,
    )


def _novel_from_orm(obj) -> Novel:
    """Convert an ORM ``Novel`` to the domain :class:`Novel` model."""
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return Novel(**d)


def _attach_special_follow(session: Session, novels: list[Novel]) -> None:
    """Populate ``is_special_follow`` on *novels* from ``author`` in one query."""
    author_ids = {n.author_id for n in novels if n.author_id}
    if not author_ids:
        for novel in novels:
            novel.is_special_follow = False
        return
    sf = set(
        session.execute(
            select(models.Author.author_id).where(
                models.Author.is_special_follow == True,  # noqa: E712
                models.Author.author_id.in_(author_ids),
            )
        )
        .scalars()
        .all()
    )
    for novel in novels:
        novel.is_special_follow = novel.author_id in sf


class SQLAlchemyNovelReadRepository(BaseRepository):
    """Read-only repository for novel queries and scope filtering."""

    def __init__(
        self,
        session: Session,
        *,
        query_engine: NovelQueryEnginePort | None = None,
    ):
        super().__init__(session)
        self._query_engine: NovelQueryEnginePort | None = (
            query_engine if query_engine is not None else session.info.get(engine_delta.SESSION_ENGINE_KEY)
        )

    def _require_engine(self) -> NovelQueryEnginePort:
        eng = self._query_engine
        if eng is None or not eng.ready():
            raise RuntimeError("Rust query_engine is not attached or not ready on this session")
        engine_delta.sync_engine_if_needed(self.session, eng)
        return eng

    def _compile_engine_query(
        self,
        spec: QuerySpec,
        *,
        count_blocked: bool = False,
        restrict_ids: list[int] | None = None,
    ) -> EngineQuery:
        for q_type, _qvalue in spec.conditions:
            if q_type not in _VALID_CONDITION_FIELDS:
                raise ValidationError(f"Invalid query field: {q_type}")
        tags, keywords, field_filters = _categorize_conditions(spec.conditions)
        int_filters: list[tuple[str, int]] = []
        str_filters: list[tuple[str, str]] = []
        only_fav = False
        only_sf = False
        for qtype, value in field_filters.items():
            if qtype == C.FIELD_IS_FAVOURITE:
                only_fav = True
                continue
            if qtype == C.FIELD_IS_SPECIAL_FOLLOW:
                only_sf = True
                continue
            if value == "":
                continue
            if qtype in _INT_FILTER_COLS:
                int_filters.append((qtype, _parse_query_int(value, qtype)))
            else:
                str_filters.append((qtype, value))
        kw_segments: list[str] = []
        for kw in keywords:
            kw_segments.extend(normalize_search_segments(kw))
        return EngineQuery(
            tags=tuple(tags),
            keyword_segments=tuple(kw_segments),
            int_filters=tuple(int_filters),
            str_filters=tuple(str_filters),
            only_favourite=only_fav,
            only_special_follow=only_sf,
            min_like=spec.min_like,
            min_text=spec.min_text,
            exclude_blocked_tags=spec.exclude_blocked_tags,
            count_blocked=count_blocked,
            restrict_ids=tuple(restrict_ids) if restrict_ids is not None else None,
            exclude_ids=tuple(spec.exclude_ids) if spec.exclude_ids else None,
        )

    @staticmethod
    def _compile_engine_cursor(
        cursor: dict | None,
        order_by: str,
    ) -> EngineCursor | None:
        if not cursor:
            return None
        if not isinstance(cursor, dict):
            raise ValidationError("Invalid cursor format")
        if "id" not in cursor:
            raise ValidationError("Cursor missing 'id'")
        cursor_id = _parse_query_int(cursor["id"], "cursor.id")
        if order_by == C.ORDER_BY_RANDOM:
            if "seed" not in cursor or "h" not in cursor:
                raise ValidationError("Cursor missing 'seed'/'h' for random ordering")
            return EngineCursor(
                cursor_id=cursor_id,
                random_seed_hex=str(cursor["seed"]),
                random_h_hex=str(cursor["h"]),
            )
        if order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by for cursor pagination: {order_by}")
        if order_by == C.COL_ID:
            return EngineCursor(cursor_id=cursor_id, cursor_int=cursor_id)
        if order_by not in cursor:
            raise ValidationError(f"Cursor missing '{order_by}'")
        raw_val = _parse_cursor_val(order_by, cursor[order_by])
        if order_by == C.COL_CREATE_TIME:
            return EngineCursor(
                cursor_id=cursor_id,
                cursor_int=datetime_to_us(raw_val) if raw_val is not None else None,
            )
        if order_by in _INT_FILTER_COLS:
            return EngineCursor(cursor_id=cursor_id, cursor_int=int(raw_val))
        return EngineCursor(cursor_id=cursor_id, cursor_str=str(raw_val))

    def get_by_id(self, novel_id: int) -> Novel | None:
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return None
        domain = _novel_from_orm(novel)
        _attach_special_follow(self.session, [domain])
        return domain

    def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    def get_stored_paths(self, novel_ids: set[int]) -> dict[int, str]:
        """``{id: path}`` for the given ids — the on-disk anchor of each row."""
        if not novel_ids:
            return {}
        stmt = select(models.Novel.id, models.Novel.path).where(
            models.Novel.id.in_(novel_ids),
            models.Novel.path.isnot(None),
        )
        return {nid: path for nid, path in self.session.execute(stmt).all() if path}

    def sort_novel_ids(
        self,
        novel_ids: list[int],
        order_by: str = C.COL_LIKES,
        order_direction: str = "DESC",
    ) -> list[int]:
        """Return *novel_ids* ordered by a novel column (id / like / view / text / create_time)."""
        if not novel_ids or order_by not in (
            C.COL_ID,
            C.COL_LIKES,
            C.COL_VIEWS,
            C.COL_TEXTS,
            C.COL_CREATE_TIME,
        ):
            return list(novel_ids)
        descending = order_direction.upper() == "DESC"
        return self._require_engine().sort_ids(novel_ids, order_by=order_by, descending=descending)

    def get_novels(self, spec: QuerySpec, *, only_blocked: bool = False) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*."""
        eng = self._require_engine()
        if spec.order_by and spec.order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by field: {spec.order_by}")
        self._validate_order_direction(spec.order_direction)

        query = self._compile_engine_query(spec, count_blocked=only_blocked)
        order_by = spec.order_by or C.COL_ID
        parsed_cursor = self._compile_engine_cursor(spec.cursor, order_by)
        descending = spec.order_direction.upper() == "DESC"
        page = eng.query_novel_rows(
            query,
            order_by=order_by,
            descending=descending,
            limit=spec.per_page,
            cursor=parsed_cursor,
        )
        novels = [_novel_from_record(rec) for rec in page.rows]
        cursor_out: dict[str, Any] | None = None
        if page.has_more and novels:
            if order_by == C.ORDER_BY_RANDOM:
                cursor_out = page.random_cursor
            else:
                last = novels[-1]
                cursor_out = {
                    "id": last.id,
                    order_by: getattr(last, order_by, None),
                }
        return {"novels": novels, "cursor": cursor_out}

    def count_novels(self, spec: QuerySpec) -> NovelCount:
        """Count VISIBLE novels matching *spec* (bounded)."""
        eng = self._require_engine()
        self._validate_order_direction(spec.order_direction)
        res = eng.count_novels(self._compile_engine_query(spec), cap=COUNT_CAP)
        return NovelCount(res.count, truncated=res.truncated)

    def has_excluded_novels(self, spec: QuerySpec) -> bool:
        """Whether any novel matching *spec* carries blocked tags."""
        eng = self._require_engine()
        return eng.has_match(self._compile_engine_query(spec, count_blocked=True))

    def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated."""
        eng = self._require_engine()
        return eng.list_matching_ids(self._compile_engine_query(spec))

    def filter_ids_in_scope(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        """Return the subset of *novel_ids* matching *spec*."""
        if not novel_ids:
            return []
        eng = self._require_engine()
        return eng.list_matching_ids(self._compile_engine_query(spec, restrict_ids=novel_ids))

    def get_novel_cards_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return card-projection novel models for the given IDs, in caller order."""
        if not novel_ids:
            return []
        eng = self._require_engine()
        return [_novel_from_record(rec) for rec in eng.get_novel_rows_by_ids(novel_ids)]

    def get_novels_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return full novel models for the given IDs, in the given order."""
        if not novel_ids:
            return []
        rows = (
            self.session.execute(select(models.Novel).where(models.Novel.id.in_(novel_ids))).scalars().all()
        )
        novels = [_novel_from_orm(n) for n in rows]
        by_id = {n.id: n for n in novels}
        return [by_id[nid] for nid in novel_ids if nid in by_id]

    def get_latest_novel_by_ids(self, novel_ids: list[int]) -> Novel | None:
        """Return the highest-ID novel among *novel_ids* (without ``is_special_follow``)."""
        if not novel_ids:
            return None
        row = self.session.execute(
            select(models.Novel)
            .where(models.Novel.id.in_(novel_ids))
            .order_by(models.Novel.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _novel_from_orm(row) if row is not None else None

    @staticmethod
    def _validate_order_direction(order_direction: str) -> None:
        if order_direction.upper() not in ("ASC", "DESC"):
            raise ValidationError(f"Invalid order_direction: {order_direction} (expected ASC/DESC)")

    def list_all_ids(self) -> set[int]:
        """Return every novel ID currently stored in PostgreSQL."""
        rows = self.session.execute(select(models.Novel.id)).scalars().all()
        return {int(nid) for nid in rows}

    def list_all_id_paths(self) -> list[tuple[int, str | None]]:
        """Return ``(id, path)`` for every novel currently stored in PostgreSQL."""
        rows = self.session.execute(select(models.Novel.id, models.Novel.path)).all()
        return [(int(nid), path) for nid, path in rows]

    def iter_epub_sweep_page(self, after_id: int | None, limit: int) -> list[Any]:
        """Return one keyset page of ``(id, path, has_epub)`` ordered by ``id``."""
        stmt = select(models.Novel.id, models.Novel.path, models.Novel.has_epub)
        if after_id is not None:
            stmt = stmt.where(models.Novel.id > after_id)
        return list(self.session.execute(stmt.order_by(models.Novel.id).limit(limit)).fetchall())

    def count_missing_source(self) -> int:
        """Count Pixiv novels currently marked ``MISSING_SOURCE``."""
        return int(
            self.session.execute(
                select(func.count())
                .select_from(models.Novel)
                .where(
                    models.Novel.has_epub == EpubStatus.MISSING_SOURCE,
                    models.Novel.source == SOURCE_PIXIV,
                    models.Novel.id > 0,
                )
            ).scalar()
            or 0
        )

    def find_refetch_candidates(
        self,
        *,
        limit: int | None,
        max_retries: int,
        skip_ids: set[int] | None = None,
        skip_cutoff: datetime | None = None,
    ) -> list[int]:
        """Return Pixiv novel IDs with ``MISSING_SOURCE`` not excluded by failures."""
        skip_cond = models.FailedNovel.failed_times >= max_retries
        if skip_cutoff is not None:
            skip_cond = or_(
                skip_cond,
                and_(
                    models.FailedNovel.last_failed_at.isnot(None),
                    models.FailedNovel.last_failed_at >= skip_cutoff,
                ),
            )

        failed_skip_exists = exists().where(
            models.FailedNovel.novel_id == models.Novel.id,
            skip_cond,
        )

        stmt = select(models.Novel.id).where(
            models.Novel.has_epub == EpubStatus.MISSING_SOURCE,
            models.Novel.source == SOURCE_PIXIV,
            models.Novel.id > 0,
            ~failed_skip_exists,
        )
        if skip_ids:
            stmt = stmt.where(models.Novel.id.not_in(skip_ids))
        stmt = stmt.order_by(models.Novel.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [int(nid) for nid in self.session.execute(stmt).scalars().all()]
