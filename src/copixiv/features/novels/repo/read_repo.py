"""Novel read repository (queries, listing, scopes, blocked-tag exclusion)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import EpubStatus, Novel
from copixiv.core.search_text import datetime_to_us, normalize_search_segments, us_to_datetime
from copixiv.core.services import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    QuerySpec,
    resolve_active,
)
from copixiv.db import constants as C
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.base import BaseRepository
from copixiv.log import logger
from copixiv.query_engine.port import (
    EngineCursor,
    EngineNovelRecord,
    EngineQuery,
    NovelQueryEnginePort,
)

from .query_builder import (
    _FILTERABLE_NOVEL_FIELDS,
    _INT_FILTER_COLS,
    _SORTABLE_NOVEL_FIELDS,
    NovelQueryBuilder,
    _parse_cursor_val,
    _parse_query_int,
    blocked_tags_excluded,
    time_axis_col,
)

# Bounded count cap — matches ``batch_operations.BATCH_MAX_NOVELS``.
COUNT_CAP = 5000


@dataclass(frozen=True)
class NovelCount:
    """有界计数结果：``value`` 加上「它是不是下界」。"""

    value: int
    truncated: bool = False


_NOVEL_CARD_COLUMN_NAMES = (
    "id",
    "title",
    "author_id",
    "author_name",
    "series_id",
    "series_name",
    "series_index",
    "text",
    "like",
    "view",
    "has_epub",
    "create_time",
    "source",
    "is_favourite",
    "tags",
)
_CARD_COMPARE_FIELDS = _NOVEL_CARD_COLUMN_NAMES + (C.FIELD_IS_SPECIAL_FOLLOW,)


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
        shuffle=0,
        has_epub=EpubStatus(rec.has_epub),
        source=rec.source,
        source_key=None,
        tags=list(rec.tags),
        is_favourite=rec.is_favourite,
        is_special_follow=rec.is_special_follow,
    )


def _card_key(novel: Novel) -> tuple[Any, ...]:
    return tuple(getattr(novel, field) for field in _CARD_COMPARE_FIELDS)


def _novels_cards_match(pg_novels: list[Novel], rs_novels: list[Novel]) -> bool:
    if len(pg_novels) != len(rs_novels):
        return False
    return all(_card_key(pg) == _card_key(rs) for pg, rs in zip(pg_novels, rs_novels, strict=True))


def _novel_from_orm(obj) -> Novel:
    """Convert an ORM ``Novel`` to the domain :class:`Novel` model."""
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return Novel(**d)


def _novel_from_mapping(mapping) -> Novel:
    """Convert a row ``RowMapping`` (column-based select) to a domain Novel."""
    d = dict(mapping)
    if C.FIELD_IS_SPECIAL_FOLLOW in d:
        d[C.FIELD_IS_SPECIAL_FOLLOW] = bool(d[C.FIELD_IS_SPECIAL_FOLLOW])
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
                models.Author.is_special_follow == True,
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
            query_engine
            if query_engine is not None
            else session.info.get(engine_delta.SESSION_ENGINE_KEY)
        )
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE,
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM,
        }

    def _active_engine(self) -> NovelQueryEnginePort | None:
        eng = self._query_engine
        if eng is None or eng.mode == "pg" or not eng.ready():
            return None
        return eng

    def _compile_engine_query(
        self,
        spec: QuerySpec,
        *,
        count_blocked: bool = False,
        restrict_ids: list[int] | None = None,
    ) -> EngineQuery:
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)
        tags, keywords, field_filters = NovelQueryBuilder._categorize(spec.conditions)
        int_filters: list[tuple[str, int]] = []
        str_filters: list[tuple[str, str]] = []
        only_fav = False
        only_sf = False
        for qtype, value in field_filters.items():
            self._validate_query_field(qtype)
            if qtype == C.FIELD_IS_FAVOURITE:
                only_fav = True
                continue
            if qtype == C.FIELD_IS_SPECIAL_FOLLOW:
                only_sf = True
                continue
            if qtype not in _FILTERABLE_NOVEL_FIELDS:
                raise ValidationError(f"Field is not filterable: {qtype}")
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
        cursor: dict | None, order_by: str,
    ) -> EngineCursor | None | str:
        if not cursor:
            return None
        if not isinstance(cursor, dict):
            raise ValidationError("Invalid cursor format")
        if "id" not in cursor:
            raise ValidationError("Cursor missing 'id'")
        cursor_id = _parse_query_int(cursor["id"], "cursor.id")
        if order_by == C.ORDER_BY_RANDOM:
            if "shuffle" in cursor and "seed" not in cursor:
                return "FALLBACK_PG"
            if "seed" not in cursor or "h" not in cursor:
                raise ValidationError("Cursor missing 'seed'/'h' for random ordering")
            return EngineCursor(
                cursor_id=cursor_id,
                random_seed_hex=str(cursor["seed"]),
                random_h_hex=str(cursor["h"]),
            )
        if order_by == C.ORDER_BY_NONE or order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by for cursor pagination: {order_by}")
        if order_by == C.COL_ID:
            return EngineCursor(cursor_id=cursor_id, cursor_int=cursor_id)
        if order_by not in cursor:
            raise ValidationError(f"Cursor missing '{order_by}'")
        raw_val = _parse_cursor_val(order_by, cursor[order_by])
        if order_by == C.COL_CREATE_TIME:
            return EngineCursor(cursor_id=cursor_id, cursor_int=datetime_to_us(raw_val))
        if order_by in _INT_FILTER_COLS:
            return EngineCursor(cursor_id=cursor_id, cursor_int=int(raw_val))
        return EngineCursor(cursor_id=cursor_id, cursor_str=str(raw_val))

    def _hydrate_card_novels_ordered(self, novel_ids: list[int]) -> list[Novel]:
        if not novel_ids:
            return []
        cols: list[Any] = [
            getattr(models.Novel, name) for name in _NOVEL_CARD_COLUMN_NAMES
        ]
        cols.append(
            func.coalesce(models.Author.is_special_follow, False).label(
                C.FIELD_IS_SPECIAL_FOLLOW,
            )
        )
        stmt = (
            select(*cols)
            .select_from(models.Novel)
            .outerjoin(
                models.Author,
                models.Author.author_id == models.Novel.author_id,
            )
            .where(models.Novel.id.in_(novel_ids))
        )
        rows = self.session.execute(stmt).fetchall()
        by_id = {row._mapping["id"]: _novel_from_mapping(row._mapping) for row in rows}
        return [by_id[nid] for nid in novel_ids if nid in by_id]

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
        return {
            nid: path
            for nid, path in self.session.execute(stmt).all()
            if path
        }

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

    def _validate_and_resolve_blocked(self, spec: QuerySpec) -> frozenset[str]:
        """Validate filter condition fields on *spec* and return active blocked tags."""
        for q_type, _qvalue in spec.conditions:
            self._validate_query_field(q_type)
        return (
            self._blocked_tag_names()
            if self._exclusion_active(spec.exclude_blocked_tags)
            else frozenset()
        )

    def list_blocked_ids(self) -> list[int]:
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            return eng.list_blocked_ids()
        if not self._exclusion_active(None):
            pg_ids: list[int] = []
        else:
            pg_ids = self._blocked_novel_ids(self._blocked_tag_names())
        if eng is not None and eng.mode == "shadow":
            rs_ids = eng.list_blocked_ids()
            if set(pg_ids) != set(rs_ids):
                logger.warning(
                    "Shadow parity mismatch in list_blocked_ids: pg={} rs={}",
                    len(pg_ids), len(rs_ids),
                )
        return pg_ids

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
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            return eng.sort_ids(novel_ids, order_by=order_by, descending=descending)

        col = (
            time_axis_col(models.Novel)
            if order_by == C.COL_ID
            else getattr(models.Novel, order_by)
        )
        stmt = (
            select(models.Novel.id)
            .where(models.Novel.id.in_(novel_ids))
            .order_by(
                col.desc() if descending else col.asc(),
                models.Novel.id.desc() if descending else models.Novel.id.asc(),
            )
        )
        pg_ids = list(self.session.execute(stmt).scalars())
        if eng is not None and eng.mode == "shadow":
            rs_ids = eng.sort_ids(novel_ids, order_by=order_by, descending=descending)
            if pg_ids != rs_ids:
                logger.warning(
                    "Shadow parity mismatch in sort_novel_ids(order_by={}, desc={}): pg={} rs={}",
                    order_by, descending, pg_ids[:10], rs_ids[:10],
                )
        return pg_ids

    def _get_novels_pg(self, spec: QuerySpec) -> dict:
        if spec.order_by:
            self._validate_query_field(spec.order_by)
        self._validate_order_direction(spec.order_direction)
        blocked_names = self._validate_and_resolve_blocked(spec)

        # Random browsing — precomputed shuffle column for fast index seek.
        if spec.order_by == "random" and not spec.conditions:
            if not spec.cursor:
                novels = self._get_random_novels_shuffle(
                    spec.per_page, spec.min_like or 0, spec.min_text or 0,
                    blocked_names,
                )
                cursor_out: dict[str, Any] | None = None
                if novels and len(novels) >= spec.per_page:
                    last = novels[-1]
                    cursor_out = {"shuffle": last.shuffle, "id": last.id}
                return {"cursor": cursor_out, "novels": novels}

        page_spec = spec.model_copy(update={"per_page": spec.per_page + 1})

        builder = NovelQueryBuilder(
            self, page_spec, blocked_tag_names=blocked_names,
        )
        query, _ = builder.build()

        result = self.session.execute(query)
        novels = [_novel_from_mapping(row._mapping) for row in result.fetchall()]

        cursor_out = None
        if len(novels) > spec.per_page:
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

    def _get_novels_rust(
        self, eng: NovelQueryEnginePort, spec: QuerySpec,
    ) -> dict | None:
        if spec.order_by:
            self._validate_query_field(spec.order_by)
        if spec.order_by and spec.order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by field: {spec.order_by}")
        self._validate_order_direction(spec.order_direction)
        query = self._compile_engine_query(spec)
        order_by = spec.order_by or C.COL_ID
        parsed_cursor = self._compile_engine_cursor(spec.cursor, order_by)
        if parsed_cursor == "FALLBACK_PG":
            return None
        assert not isinstance(parsed_cursor, str)
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

    def get_novels(self, spec: QuerySpec) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*."""
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            rust_res = self._get_novels_rust(eng, spec)
            if rust_res is not None:
                return rust_res

        pg_res = self._get_novels_pg(spec)
        if eng is not None and eng.mode == "shadow":
            try:
                rust_res = self._get_novels_rust(eng, spec)
                if rust_res is not None:
                    pg_novels = pg_res["novels"]
                    rs_novels = rust_res["novels"]
                    pg_ids = [n.id for n in pg_novels]
                    rs_ids = [n.id for n in rs_novels]
                    pg_more = pg_res["cursor"] is not None
                    rs_more = rust_res["cursor"] is not None
                    if spec.order_by not in (C.ORDER_BY_RANDOM, C.ORDER_BY_NONE):
                        if pg_ids != rs_ids or pg_more != rs_more:
                            logger.warning(
                                "Shadow parity mismatch in get_novels(order_by={}): pg={} ({}) rs={} ({})",
                                spec.order_by, pg_ids[:10], pg_more, rs_ids[:10], rs_more,
                            )
                        elif not _novels_cards_match(pg_novels, rs_novels):
                            logger.warning(
                                "Shadow card-field mismatch in get_novels(order_by={}): ids={}",
                                spec.order_by, pg_ids[:10],
                            )
            except Exception:
                logger.exception("Shadow engine error in get_novels")
        return pg_res

    def count_novels(self, spec: QuerySpec) -> NovelCount:
        """Count VISIBLE novels matching *spec* (bounded)."""
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            self._validate_order_direction(spec.order_direction)
            q = self._compile_engine_query(spec)
            res = eng.count_novels(q, cap=COUNT_CAP)
            return NovelCount(res.count, truncated=res.truncated)

        blocked_names = self._validate_and_resolve_blocked(spec)
        pg_count = self._count_with_spec(spec, blocked_tag_names=blocked_names)
        if eng is not None and eng.mode == "shadow":
            q = self._compile_engine_query(spec)
            res = eng.count_novels(q, cap=COUNT_CAP)
            rs_count = NovelCount(res.count, truncated=res.truncated)
            if pg_count != rs_count:
                logger.warning(
                    "Shadow parity mismatch in count_novels: pg={} rs={}",
                    pg_count, rs_count,
                )
        return pg_count

    def _count_with_spec(
        self,
        spec: QuerySpec,
        *,
        restrict_ids: list[int] | None = None,
        blocked_tag_names: frozenset[str] = frozenset(),
        count_blocked: bool = False,
    ) -> NovelCount:
        """有界计数：``min(真实匹配数, COUNT_CAP)`` + 是否被截断。"""
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

    def count_excluded_novels(self, spec: QuerySpec) -> NovelCount:
        """Count novels matching *spec* that carry blocked tags (同样有界)."""
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            q = self._compile_engine_query(spec, count_blocked=True)
            res = eng.count_novels(q, cap=COUNT_CAP)
            return NovelCount(res.count, truncated=res.truncated)

        if not self._exclusion_active(spec.exclude_blocked_tags):
            for q_type, _qvalue in spec.conditions:
                self._validate_query_field(q_type)
            pg_count = NovelCount(0)
        else:
            for q_type, _qvalue in spec.conditions:
                self._validate_query_field(q_type)
            blocked_names = self._blocked_tag_names()
            if not blocked_names:
                pg_count = NovelCount(0)
            else:
                pg_count = self._count_with_spec(
                    spec, blocked_tag_names=blocked_names, count_blocked=True,
                )
        if eng is not None and eng.mode == "shadow":
            q = self._compile_engine_query(spec, count_blocked=True)
            res = eng.count_novels(q, cap=COUNT_CAP)
            rs_count = NovelCount(res.count, truncated=res.truncated)
            if pg_count != rs_count:
                logger.warning(
                    "Shadow parity mismatch in count_excluded_novels: pg={} rs={}",
                    pg_count, rs_count,
                )
        return pg_count

    def has_excluded_novels(self, spec: QuerySpec) -> bool:
        """Whether any novel matching *spec* carries blocked tags."""
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            q = self._compile_engine_query(spec, count_blocked=True)
            return eng.has_match(q)

        pg_has = False
        if self._exclusion_active(spec.exclude_blocked_tags):
            for q_type, _qvalue in spec.conditions:
                self._validate_query_field(q_type)
            blocked_names = self._blocked_tag_names()
            if blocked_names:
                builder = NovelQueryBuilder(
                    self, spec, blocked_tag_names=blocked_names,
                )
                stmt = builder.build_existence(count_blocked=True)
                if stmt is not None:
                    pg_has = self.session.execute(stmt).first() is not None
        else:
            for q_type, _qvalue in spec.conditions:
                self._validate_query_field(q_type)

        if eng is not None and eng.mode == "shadow":
            q = self._compile_engine_query(spec, count_blocked=True)
            rs_has = eng.has_match(q)
            if pg_has != rs_has:
                logger.warning(
                    "Shadow parity mismatch in has_excluded_novels: pg={} rs={}",
                    pg_has, rs_has,
                )
        return pg_has

    def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated."""
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            return eng.list_matching_ids(self._compile_engine_query(spec))

        blocked_names = self._validate_and_resolve_blocked(spec)
        builder = NovelQueryBuilder(
            self, spec, blocked_tag_names=blocked_names,
        )
        stmt = builder.build_ids()
        pg_ids = list(self.session.execute(stmt).scalars())
        if eng is not None and eng.mode == "shadow":
            rs_ids = eng.list_matching_ids(self._compile_engine_query(spec))
            if pg_ids != rs_ids:
                logger.warning(
                    "Shadow parity mismatch in list_matching_ids: pg={} rs={}",
                    len(pg_ids), len(rs_ids),
                )
        return pg_ids

    def filter_ids_in_scope(
        self,
        novel_ids: list[int],
        spec: QuerySpec,
    ) -> list[int]:
        """Return the subset of *novel_ids* matching *spec*."""
        if not novel_ids:
            return []
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            q = self._compile_engine_query(spec, restrict_ids=novel_ids)
            return eng.list_matching_ids(q)

        blocked_names = self._validate_and_resolve_blocked(spec)
        builder = NovelQueryBuilder(self, spec)
        stmt = builder.build_ids_in_scope(
            novel_ids, blocked_tag_names=blocked_names,
        )
        pg_ids = list(self.session.execute(stmt).scalars())
        if eng is not None and eng.mode == "shadow":
            q = self._compile_engine_query(spec, restrict_ids=novel_ids)
            rs_ids = eng.list_matching_ids(q)
            if pg_ids != rs_ids:
                logger.warning(
                    "Shadow parity mismatch in filter_ids_in_scope: pg={} rs={}",
                    pg_ids[:10], rs_ids[:10],
                )
        return pg_ids

    def get_novel_cards_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return card-projection novel models for the given IDs, in caller order."""
        if not novel_ids:
            return []
        eng = self._active_engine()
        if eng is not None and eng.mode == "rust":
            return [_novel_from_record(rec) for rec in eng.get_novel_rows_by_ids(novel_ids)]

        pg_novels = self._hydrate_card_novels_ordered(novel_ids)
        if eng is not None and eng.mode == "shadow":
            try:
                rs_novels = [
                    _novel_from_record(rec) for rec in eng.get_novel_rows_by_ids(novel_ids)
                ]
                if not _novels_cards_match(pg_novels, rs_novels):
                    logger.warning(
                        "Shadow parity mismatch in get_novel_cards_by_ids: ids={}",
                        novel_ids[:10],
                    )
            except Exception:
                logger.exception("Shadow engine error in get_novel_cards_by_ids")
        return pg_novels

    def get_novels_by_ids(self, novel_ids: list[int]) -> list[Novel]:
        """Return full novel models for the given IDs, in the given order."""
        if not novel_ids:
            return []
        rows = self.session.execute(
            select(models.Novel).where(models.Novel.id.in_(novel_ids))
        ).scalars().all()
        novels = [_novel_from_orm(n) for n in rows]
        _attach_special_follow(self.session, novels)
        by_id = {n.id: n for n in novels}
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

        _attach_special_follow(self.session, novels)
        return novels

    # ---- helpers -------------------------------------------------------------

    def _validate_query_field(self, field: str) -> None:
        if field not in self.VALID_NOVEL_QUERY_FIELDS:
            raise ValidationError(f"Invalid query field: {field}")

    @staticmethod
    def _validate_order_direction(order_direction: str) -> None:
        if order_direction.upper() not in ("ASC", "DESC"):
            raise ValidationError(
                f"Invalid order_direction: {order_direction} (expected ASC/DESC)"
            )
