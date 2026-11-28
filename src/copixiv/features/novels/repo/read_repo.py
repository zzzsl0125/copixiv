"""Novel read repository backed exclusively by the in-process Rust query engine."""

from __future__ import annotations

import functools
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, any_, exists, func, or_, select
from sqlalchemy.orm import Session

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV, EpubStatus
from copixiv.core.search import (
    EXCLUDE_BLOCKED_SETTING_KEY,
    QuerySpec,
    normalize_search_segments,
    resolve_exclude_blocked,
)
from copixiv.core.snapshot_types import SlimNovelRecord
from copixiv.core.sources import SourceCapability, SourceRuntimeConfig
from copixiv.core.time import datetime_to_us, us_to_datetime
from copixiv.db import delta as engine_delta
from copixiv.db import models
from copixiv.db.novel_snapshot import fold_series_rows
from copixiv.features.novels import field_policy
from copixiv.features.novels.schemas import NovelBase
from copixiv.query_engine.adapter import RustNovelQueryEngine
from copixiv.query_engine.port import EngineCursor, EngineQuery

if TYPE_CHECKING:
    import copixiv_rs

# Bounded count cap for list/count endpoints.
COUNT_CAP = 5000

_INT_FILTER_COLS = frozenset(
    {
        "id",
        "author_id",
        "series_id",
        "like",
        "view",
        "text",
        "series_index",
        "has_epub",
    }
)
_STR_FILTER_COLS = frozenset(
    {
        "title",
        "author_name",
        "series_name",
        "source",
    }
)
_FILTERABLE_NOVEL_FIELDS = _INT_FILTER_COLS | _STR_FILTER_COLS
_SORTABLE_NOVEL_FIELDS = _INT_FILTER_COLS | frozenset(
    {
        "create_time",
        "title",
        "author_name",
        "series_name",
        "random",
    }
)
_VALID_CONDITION_FIELDS = _FILTERABLE_NOVEL_FIELDS | frozenset(
    {
        "tags",
        "keyword",
        "is_favourite",
        "is_special_follow",
    }
)


@dataclass(frozen=True)
class NovelCount:
    """Bounded count result: ``value`` paired with truncation flag."""

    value: int
    truncated: bool = False


@dataclass(slots=True)
class StoredNovel:
    """Stored novel row materialized from PostgreSQL (excludes ``caption`` and ``source_key``)."""

    id: int
    title: str
    author_id: int | None = None
    author_name: str | None = None
    path: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    create_time: datetime | None = None
    has_epub: EpubStatus = EpubStatus.NO
    source: str = SOURCE_PIXIV
    external_url: str | None = None
    can_sync: bool = False
    tags: list[str] = field(default_factory=list)
    is_favourite: bool = False
    is_special_follow: bool = False


def _parse_query_int(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise ValidationError(f"Invalid integer for {label}: {raw!r}")
    try:
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid integer for {label}: {raw!r}") from exc


def _parse_cursor_val(order_by: str, raw: Any) -> Any:
    if raw is None:
        if order_by == "create_time":
            return None
        raise ValidationError(f"Cursor missing non-null value for {order_by}")
    if order_by in _INT_FILTER_COLS:
        return _parse_query_int(raw, f"cursor.{order_by}")
    if order_by == "create_time":
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


_ASCII_LOWER_TRANS = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)
_MASK64 = (1 << 64) - 1


def _ascii_lower(text: str) -> str:
    return text.translate(_ASCII_LOWER_TRANS)


def _splitmix64(x: int) -> int:
    x = (x + 0x9E37_79B9_7F4A_7C15) & _MASK64
    x = ((x ^ (x >> 30)) * 0xBF58_476D_1CE4_E5B9) & _MASK64
    x = ((x ^ (x >> 27)) * 0x94D0_49BB_1331_11EB) & _MASK64
    return x ^ (x >> 31)


def _compare_field_tie(va: Any, aid: int, vb: Any, bid: int, *, descending: bool) -> int:
    """Compare two sort keys matching Rust's ``CanonNumKey`` NULL and tie-break order."""
    if (va is None) != (vb is None):
        if descending:
            return -1 if va is None else 1
        return 1 if va is None else -1
    if va is not None and va != vb:
        if descending:
            return -1 if va > vb else 1
        return -1 if va < vb else 1
    if aid == bid:
        return 0
    if descending:
        return -1 if aid > bid else 1
    return -1 if aid < bid else 1


def _chapter_id_sort_key(
    series_index: int | None, novel_id: int, *, descending: bool
) -> tuple[bool, int, int, int]:
    """Series chapter order: ``series_index ASC NULLS LAST, abs(id) ASC, id ASC`` (mirrored for DESC)."""
    idx = int(series_index) if series_index is not None else 0
    if descending:
        return (series_index is None, -idx, -abs(novel_id), -novel_id)
    return (series_index is None, idx, abs(novel_id), novel_id)


def _slim_sort_value(obj: Any, order_by: str) -> Any:
    if order_by == "id":
        return abs(int(obj.id))
    if order_by == "create_time":
        val = getattr(obj, "create_time", None)
        if val is not None:
            return datetime_to_us(val)
        return getattr(obj, "create_time_us", None)
    return getattr(obj, order_by, None)


def _categorize_conditions(conditions) -> tuple[set[str], set[str], dict[str, str]]:
    """Split an ordered condition list into ``(tags, keywords, field_filters)``."""
    tags: set[str] = set()
    keywords: set[str] = set()
    field_filters: dict[str, str] = {}
    for qtype, value in conditions:
        if not isinstance(value, str) or value.strip() == "":
            continue
        if qtype == "tags":
            tags.add(value)
        elif qtype == "keyword":
            keywords.add(value)
        else:
            field_filters[qtype] = value
    return tags, keywords, field_filters


def _novel_from_record(rec: copixiv_rs.NovelRecord | SlimNovelRecord) -> NovelBase:
    """Materialize a card-projection :class:`NovelBase` directly from Rust ``NovelRecord`` or ``SlimNovelRecord``."""
    return NovelBase.model_construct(
        id=rec.id,
        title=rec.title,
        author_id=rec.author_id,
        author_name=rec.author_name,
        like=rec.like,
        view=rec.view,
        text=rec.text,
        create_time=us_to_datetime(rec.create_time_us),
        has_epub=EpubStatus(rec.has_epub),
        tags=list(rec.tags),
        is_favourite=rec.is_favourite,
        is_special_follow=rec.is_special_follow,
        series_id=rec.series_id,
        series_name=rec.series_name,
        series_index=rec.series_index,
        source=str(rec.source),
        external_url=None,
        can_sync=False,
        is_series=(rec.series_id is not None),
        chapter_count=int(getattr(rec, "chapter_count", 1)),
    )


def _novel_base_from_orm(obj: models.Novel, *, is_special_follow: bool = False) -> NovelBase:
    """Convert an ORM ``Novel`` row into an un-folded chapter :class:`NovelBase`."""
    return NovelBase.model_construct(
        id=int(obj.id),
        title=obj.title,
        author_id=obj.author_id,
        author_name=obj.author_name,
        like=int(obj.like),
        view=int(obj.view),
        text=int(obj.text),
        create_time=obj.create_time,
        has_epub=EpubStatus(obj.has_epub),
        tags=list(obj.tags) if obj.tags else [],
        is_favourite=bool(obj.is_favourite),
        is_special_follow=bool(is_special_follow),
        series_id=obj.series_id,
        series_name=obj.series_name,
        series_index=obj.series_index,
        source=str(obj.source),
        external_url=None,
        can_sync=False,
        is_series=False,
        chapter_count=1,
    )


def _novel_from_orm(obj: models.Novel, *, is_special_follow: bool = False) -> StoredNovel:
    """Convert an ORM ``Novel`` row into a lightweight :class:`StoredNovel`."""
    return StoredNovel(
        id=obj.id,
        title=obj.title,
        author_id=obj.author_id,
        author_name=obj.author_name,
        path=obj.path,
        like=obj.like,
        view=obj.view,
        text=obj.text,
        series_id=obj.series_id,
        series_name=obj.series_name,
        series_index=obj.series_index,
        create_time=obj.create_time,
        has_epub=EpubStatus(obj.has_epub),
        source=str(obj.source),
        tags=list(obj.tags) if obj.tags else [],
        is_favourite=obj.is_favourite,
        is_special_follow=is_special_follow,
    )


def hydrate_novel_links(
    session: Session,
    novels: Sequence[NovelBase | StoredNovel],
    *,
    source_service: Any | None = None,
    source_registry: Any | None = None,
) -> None:
    """Populate ``external_url`` and ``can_sync`` on materialized novels in-place."""
    if not novels:
        return

    registry = (
        source_service.registry
        if source_service is not None and getattr(source_service, "registry", None) is not None
        else (source_registry or field_policy._get_registry())
    )

    extra_novel_ids = sorted(
        {
            int(novel.id)
            for novel in novels
            if not (getattr(novel, "is_series", False) and novel.series_id is not None)
            and str(novel.source) not in (SOURCE_PIXIV, SOURCE_LOCAL)
        }
    )
    novel_source_keys: dict[int, str] = {}
    if extra_novel_ids:
        rows = session.execute(
            select(models.Novel.id, models.Novel.source_key).where(
                models.Novel.id == any_(cast(Any, extra_novel_ids))
            )
        ).all()
        novel_source_keys = {int(nid): str(skey) for nid, skey in rows if skey}

    extra_series_ids = sorted(
        {
            int(novel.series_id)
            for novel in novels
            if getattr(novel, "is_series", False)
            and novel.series_id is not None
            and str(novel.source) not in (SOURCE_PIXIV, SOURCE_LOCAL)
        }
    )
    series_source_keys: dict[int, str] = {}
    if extra_series_ids:
        srows = session.execute(
            select(models.Series.series_id, models.Series.source_key).where(
                models.Series.series_id == any_(cast(Any, extra_series_ids))
            )
        ).all()
        series_source_keys = {int(sid): str(skey) for sid, skey in srows if skey}

    runtime_cache: dict[str, SourceRuntimeConfig] = {}
    for novel in novels:
        src = str(novel.source)
        if src not in runtime_cache:
            if source_service is not None:
                runtime_cache[src] = source_service.get_runtime_config(src)
            else:
                runtime_cache[src] = SourceRuntimeConfig(
                    source=src,
                    enabled=registry.get(src) is not None,
                )
        runtime = runtime_cache[src]

        is_series_card = bool(getattr(novel, "is_series", False) and novel.series_id is not None)
        if is_series_card and novel.series_id is not None:
            if src == SOURCE_PIXIV:
                skey: str | None = str(novel.series_id)
            elif src == SOURCE_LOCAL:
                skey = None
            else:
                skey = series_source_keys.get(int(novel.series_id))
            novel.external_url = registry.build_external_url(src, "series", skey, runtime) if skey else None
        else:
            if src == SOURCE_PIXIV:
                skey = str(novel.id)
            elif src == SOURCE_LOCAL:
                skey = None
            else:
                skey = novel_source_keys.get(int(novel.id))
            novel.external_url = registry.build_external_url(src, "novel", skey, runtime) if skey else None

        novel.can_sync = bool(
            registry.has_capability(src, SourceCapability.SYNC_NOVEL)
            and (source_service is None or source_service.is_enabled(src))
        )


class SQLAlchemyNovelReadRepository:
    """Read-only repository for novel queries and scope filtering."""

    def __init__(
        self,
        session: Session,
        *,
        query_engine: RustNovelQueryEngine | None = None,
    ):
        self.session = session
        self._query_engine: RustNovelQueryEngine | None = (
            query_engine if query_engine is not None else session.info.get(engine_delta.SESSION_ENGINE_KEY)
        )

    def hydrate_links(
        self,
        novels: Sequence[NovelBase | StoredNovel],
        *,
        source_service: Any | None = None,
        source_registry: Any | None = None,
    ) -> None:
        """Populate ``external_url`` and ``can_sync`` on materialized novels."""
        hydrate_novel_links(
            self.session,
            novels,
            source_service=source_service,
            source_registry=source_registry,
        )

    def _require_engine(self) -> RustNovelQueryEngine:
        eng = self._query_engine
        if eng is None or not eng.ready():
            raise RuntimeError("Rust query_engine is not attached or not ready on this session")
        engine_delta.sync_engine_if_needed(self.session, eng)
        return eng

    @staticmethod
    def _parse_spec_conditions(
        spec: QuerySpec,
    ) -> tuple[list[str], list[str], list[tuple[str, int]], list[tuple[str, str]], bool, bool]:
        for q_type, _qvalue in spec.conditions:
            if q_type not in _VALID_CONDITION_FIELDS:
                raise ValidationError(f"Invalid query field: {q_type}")
        tags, keywords, field_filters = _categorize_conditions(spec.conditions)
        int_filters: list[tuple[str, int]] = []
        str_filters: list[tuple[str, str]] = []
        only_fav = False
        only_sf = False
        for qtype, value in field_filters.items():
            if qtype == "is_favourite":
                only_fav = True
                continue
            if qtype == "is_special_follow":
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
        return list(tags), kw_segments, int_filters, str_filters, only_fav, only_sf

    def _extract_explicit_series_id(self, spec: QuerySpec) -> int | None:
        _, _, int_filters, _, _, _ = self._parse_spec_conditions(spec)
        for qtype, val in int_filters:
            if qtype == "series_id":
                return val
        return None

    def _compile_engine_query(
        self,
        spec: QuerySpec,
        *,
        count_blocked: bool = False,
        restrict_ids: list[int] | None = None,
    ) -> EngineQuery:
        tags, kw_segments, int_filters, str_filters, only_fav, only_sf = self._parse_spec_conditions(spec)

        # Redirect explicit ``id:<non-rep-chapter>`` search to its folded series card.
        has_series_filter = any(k == "series_id" for k, _ in int_filters)
        if not has_series_filter and self._query_engine is not None and self._query_engine.ready():
            redirected: list[tuple[str, int]] = []
            for k, val in int_filters:
                if k == "id" and not self._query_engine.get_novel_rows_by_ids([val]):
                    sid = self.session.execute(
                        select(models.Novel.series_id).where(models.Novel.id == val)
                    ).scalar_one_or_none()
                    if sid is not None:
                        redirected.append(("series_id", int(sid)))
                        continue
                redirected.append((k, val))
            int_filters = redirected

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
        if order_by == "random":
            if "seed" not in cursor or "h" not in cursor:
                raise ValidationError("Cursor missing 'seed'/'h' for random ordering")
            return EngineCursor(
                cursor_id=cursor_id,
                random_seed_hex=str(cursor["seed"]),
                random_h_hex=str(cursor["h"]),
            )
        if order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by for cursor pagination: {order_by}")
        if order_by == "id":
            return EngineCursor(cursor_id=cursor_id, cursor_int=cursor_id)
        if order_by not in cursor:
            raise ValidationError(f"Cursor missing '{order_by}'")
        raw_val = _parse_cursor_val(order_by, cursor[order_by])
        if order_by == "create_time":
            return EngineCursor(
                cursor_id=cursor_id,
                cursor_int=datetime_to_us(raw_val) if raw_val is not None else None,
            )
        if order_by in _INT_FILTER_COLS:
            return EngineCursor(cursor_id=cursor_id, cursor_int=int(raw_val))
        return EngineCursor(cursor_id=cursor_id, cursor_str=str(raw_val))

    def get_by_id(self, novel_id: int) -> StoredNovel | None:
        novel = self.session.get(models.Novel, novel_id)
        if novel is None:
            return None
        is_sf = False
        if novel.author_id:
            is_sf = bool(
                self.session.execute(
                    select(models.Author.is_special_follow).where(models.Author.author_id == novel.author_id)
                ).scalar_one_or_none()
            )
        return _novel_from_orm(novel, is_special_follow=is_sf)

    def get_existing_ids(self, novel_ids: set[int]) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        return set(self.session.execute(stmt).scalars().all())

    def get_ids_missing_series_index(self, novel_ids: set[int]) -> set[int]:
        """Return *novel_ids* whose stored ``series_index`` is NULL (excluding retired series)."""
        if not novel_ids:
            return set()
        retired = select(models.SeriesDeadLetter.series_id).where(
            models.SeriesDeadLetter.retired_at.isnot(None)
        )
        stmt = select(models.Novel.id).where(
            models.Novel.id.in_(novel_ids),
            models.Novel.series_index.is_(None),
            or_(models.Novel.series_id.is_(None), ~models.Novel.series_id.in_(retired)),
        )
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

    def get_series_paths(self, series_ids: set[int]) -> dict[int, list[str]]:
        """``{series_id: [path, ...]}`` for resolving existing series directories."""
        if not series_ids:
            return {}
        stmt = select(models.Novel.series_id, models.Novel.path).where(
            models.Novel.series_id.in_(series_ids),
            models.Novel.path.isnot(None),
        )
        grouped: dict[int, list[str]] = {}
        for sid, path in self.session.execute(stmt).all():
            if sid is not None and path:
                grouped.setdefault(int(sid), []).append(path)
        return grouped

    def sort_novel_ids(
        self,
        novel_ids: list[int],
        order_by: str = "like",
        order_direction: str = "DESC",
    ) -> list[int]:
        """Return *novel_ids* ordered by a novel column (id / like / view / text / create_time)."""
        if not novel_ids or order_by not in (
            "id",
            "like",
            "view",
            "text",
            "create_time",
        ):
            return list(novel_ids)
        descending = order_direction.upper() == "DESC"
        eng = self._require_engine()
        unique_ids = list(dict.fromkeys(int(nid) for nid in novel_ids))
        rust_recs = eng.get_novel_rows_by_ids(unique_ids)
        if len(rust_recs) == len(unique_ids):
            return eng.sort_ids(novel_ids, order_by=order_by, descending=descending)

        sort_vals: dict[int, Any] = {rec.id: _slim_sort_value(rec, order_by) for rec in rust_recs}
        missing_ids = [nid for nid in unique_ids if nid not in sort_vals]
        if missing_ids:
            cols: dict[str, Any] = {
                "id": models.Novel.id,
                "like": models.Novel.like,
                "view": models.Novel.view,
                "text": models.Novel.text,
                "create_time": models.Novel.create_time,
            }
            col = cols[order_by]
            rows = self.session.execute(
                select(models.Novel.id, col).where(models.Novel.id == any_(cast(Any, missing_ids)))
            ).all()
            for nid, raw_val in rows:
                nid_int = int(nid)
                if order_by == "id":
                    sort_vals[nid_int] = abs(nid_int)
                elif order_by == "create_time":
                    sort_vals[nid_int] = datetime_to_us(raw_val) if raw_val is not None else None
                else:
                    sort_vals[nid_int] = raw_val

        def _cmp_ids(a: int, b: int) -> int:
            return _compare_field_tie(
                sort_vals[a],
                a,
                sort_vals[b],
                b,
                descending=descending,
            )

        existing = [nid for nid in unique_ids if nid in sort_vals]
        existing.sort(key=functools.cmp_to_key(_cmp_ids))
        return existing

    def _query_series_chapters_pg(
        self,
        spec: QuerySpec,
        series_id: int,
        *,
        only_blocked: bool = False,
        restrict_ids: list[int] | None = None,
    ) -> list[NovelBase]:
        """Return filtered individual chapter cards for *series_id* directly from PostgreSQL."""
        tags, kw_segments, int_filters, str_filters, only_fav, only_sf = self._parse_spec_conditions(spec)

        setting_row = self.session.execute(
            select(models.Setting.value).where(models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY)
        ).scalar_one_or_none()
        exclude_active = resolve_exclude_blocked(spec.exclude_blocked_tags, setting_row)
        blocked_tags = {
            str(t)
            for t in self.session.execute(
                select(models.TagPreference.tag).where(
                    models.TagPreference.preference == models.TagPreferenceORM.blocked
                )
            )
            .scalars()
            .all()
        }

        if only_blocked and (not exclude_active or not blocked_tags):
            return []

        stmt = (
            select(models.Novel, func.coalesce(models.Author.is_special_follow, False))
            .outerjoin(models.Author, models.Author.author_id == models.Novel.author_id)
            .where(models.Novel.series_id == series_id)
        )
        rows = self.session.execute(stmt).all()
        restrict_set = set(restrict_ids) if restrict_ids is not None else None
        exclude_set = set(spec.exclude_ids) if spec.exclude_ids else None
        low_segments = [_ascii_lower(seg) for seg in dict.fromkeys(kw_segments) if seg]

        matched: list[NovelBase] = []
        for obj, is_sf in rows:
            nid = int(obj.id)
            if restrict_set is not None and nid not in restrict_set:
                continue
            if exclude_set is not None and nid in exclude_set:
                continue
            if only_fav and not obj.is_favourite:
                continue
            if only_sf and not is_sf:
                continue

            card = _novel_base_from_orm(obj, is_special_follow=bool(is_sf))

            if any(getattr(card, field) != val for field, val in int_filters):
                continue
            if any((getattr(card, field) or "") != val for field, val in str_filters):
                continue

            card_tags = set(card.tags)
            if tags and not all(t in card_tags for t in tags):
                continue

            has_blocked = bool(blocked_tags and any(t in blocked_tags for t in card.tags))
            if only_blocked:
                if not has_blocked:
                    continue
            elif exclude_active and has_blocked:
                continue

            if spec.min_like is not None and spec.min_like > 0 and card.like < spec.min_like:
                continue
            if spec.min_text is not None and spec.min_text > 0 and card.text < spec.min_text:
                continue

            if low_segments:
                fields = [
                    _ascii_lower(card.title),
                    _ascii_lower(card.author_name or ""),
                    _ascii_lower(card.series_name or ""),
                    *(_ascii_lower(t) for t in card.tags),
                ]
                if not all(any(seg in f for f in fields) for seg in low_segments):
                    continue

            matched.append(card)
        return matched

    def _paginate_series_chapters(
        self,
        matched: list[NovelBase],
        spec: QuerySpec,
    ) -> dict[str, Any]:
        order_by = spec.order_by or "id"
        descending = spec.order_direction.upper() == "DESC"
        cursor = spec.cursor

        if cursor is not None:
            if not isinstance(cursor, dict):
                raise ValidationError("Invalid cursor format")
            if "id" not in cursor:
                raise ValidationError("Cursor missing 'id'")
            cursor_id = _parse_query_int(cursor["id"], "cursor.id")
        else:
            cursor_id = None

        if order_by == "random":
            if cursor is not None and cursor_id is not None:
                if "seed" not in cursor or "h" not in cursor:
                    raise ValidationError("Cursor missing 'seed'/'h' for random ordering")
                try:
                    seed = int(str(cursor["seed"]), 16)
                    cursor_pair: tuple[int, int] | None = (int(str(cursor["h"]), 16), cursor_id)
                except ValueError as exc:
                    raise ValidationError("Invalid random cursor hex") from exc
            else:
                seed = secrets.randbits(64)
                cursor_pair = None
            ranked = sorted(
                ((_splitmix64((c.id & _MASK64) ^ seed), c.id, c) for c in matched),
                key=lambda item: (item[0], item[1]),
            )
            if cursor_pair is not None:
                ranked = [item for item in ranked if (item[0], item[1]) > cursor_pair]
            has_more = len(ranked) > spec.per_page
            page_items = ranked[: spec.per_page]
            novels = [item[2] for item in page_items]
            cursor_out: dict[str, Any] | None = None
            if has_more and page_items:
                last_h, last_id, _ = page_items[-1]
                cursor_out = {"seed": f"{seed:016x}", "h": f"{last_h:016x}", "id": last_id}
            return {"novels": novels, "cursor": cursor_out}

        if order_by == "id":
            ordered = sorted(
                matched,
                key=lambda c: _chapter_id_sort_key(c.series_index, c.id, descending=descending),
            )
            if cursor_id is not None and cursor is not None:
                if "series_index" in cursor:
                    raw_idx = cursor["series_index"]
                    cursor_idx = (
                        _parse_query_int(raw_idx, "cursor.series_index") if raw_idx is not None else None
                    )
                else:
                    matched_by_id = {c.id: c.series_index for c in matched}
                    if cursor_id in matched_by_id:
                        cursor_idx = matched_by_id[cursor_id]
                    else:
                        cursor_idx = self.session.execute(
                            select(models.Novel.series_index).where(models.Novel.id == cursor_id)
                        ).scalar_one_or_none()
                cursor_key = _chapter_id_sort_key(cursor_idx, cursor_id, descending=descending)
                ordered = [
                    c
                    for c in ordered
                    if _chapter_id_sort_key(c.series_index, c.id, descending=descending) > cursor_key
                ]
            has_more = len(ordered) > spec.per_page
            novels = ordered[: spec.per_page]
            cursor_out = None
            if has_more and novels:
                last = novels[-1]
                cursor_out = {"id": last.id, "series_index": last.series_index}
            return {"novels": novels, "cursor": cursor_out}

        if order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by for cursor pagination: {order_by}")

        def _field_val(c: NovelBase) -> Any:
            if order_by == "create_time":
                return datetime_to_us(c.create_time)
            return getattr(c, order_by, None)

        def _cmp_cards(a: NovelBase, b: NovelBase) -> int:
            return _compare_field_tie(
                _field_val(a),
                a.id,
                _field_val(b),
                b.id,
                descending=descending,
            )

        ordered = sorted(matched, key=functools.cmp_to_key(_cmp_cards))
        if cursor_id is not None and cursor is not None:
            if order_by not in cursor:
                raise ValidationError(f"Cursor missing '{order_by}'")
            raw_val = _parse_cursor_val(order_by, cursor[order_by])
            cval = (
                (datetime_to_us(raw_val) if raw_val is not None else None)
                if order_by == "create_time"
                else raw_val
            )
            ordered = [
                c
                for c in ordered
                if _compare_field_tie(_field_val(c), c.id, cval, cursor_id, descending=descending) > 0
            ]
        has_more = len(ordered) > spec.per_page
        novels = ordered[: spec.per_page]
        cursor_out = None
        if has_more and novels:
            last = novels[-1]
            cursor_out = {"id": last.id, order_by: getattr(last, order_by, None)}
        return {"novels": novels, "cursor": cursor_out}

    def get_novels(self, spec: QuerySpec, *, only_blocked: bool = False) -> dict:
        """Retrieve a paginated, filtered list of novels per *spec*."""
        if spec.order_by and spec.order_by not in _SORTABLE_NOVEL_FIELDS:
            raise ValidationError(f"Invalid order_by field: {spec.order_by}")
        self._validate_order_direction(spec.order_direction)

        explicit_sid = self._extract_explicit_series_id(spec)
        if explicit_sid is not None:
            matched = self._query_series_chapters_pg(spec, explicit_sid, only_blocked=only_blocked)
            return self._paginate_series_chapters(matched, spec)

        eng = self._require_engine()
        query = self._compile_engine_query(spec, count_blocked=only_blocked)
        order_by = spec.order_by or "id"
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
            if order_by == "random":
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
        self._validate_order_direction(spec.order_direction)
        explicit_sid = self._extract_explicit_series_id(spec)
        if explicit_sid is not None:
            total = len(self._query_series_chapters_pg(spec, explicit_sid))
            return NovelCount(min(total, COUNT_CAP), truncated=total > COUNT_CAP)
        eng = self._require_engine()
        res = eng.count_novels(self._compile_engine_query(spec), cap=COUNT_CAP)
        return NovelCount(res.count, truncated=res.truncated)

    def has_excluded_novels(self, spec: QuerySpec) -> bool:
        """Whether any novel matching *spec* carries blocked tags."""
        explicit_sid = self._extract_explicit_series_id(spec)
        if explicit_sid is not None:
            return bool(self._query_series_chapters_pg(spec, explicit_sid, only_blocked=True))
        eng = self._require_engine()
        return eng.has_match(self._compile_engine_query(spec, count_blocked=True))

    def list_matching_ids(self, spec: QuerySpec) -> list[int]:
        """Return every VISIBLE novel ID matching *spec*, unpaginated."""
        explicit_sid = self._extract_explicit_series_id(spec)
        if explicit_sid is not None:
            return sorted(c.id for c in self._query_series_chapters_pg(spec, explicit_sid))
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
        explicit_sid = self._extract_explicit_series_id(spec)
        if explicit_sid is not None:
            return sorted(
                c.id for c in self._query_series_chapters_pg(spec, explicit_sid, restrict_ids=novel_ids)
            )
        eng = self._require_engine()
        return eng.list_matching_ids(self._compile_engine_query(spec, restrict_ids=novel_ids))

    def get_novel_cards_by_ids(self, novel_ids: list[int]) -> list[NovelBase]:
        """Return card-projection novel models for the given IDs, in caller order."""
        if not novel_ids:
            return []
        eng = self._require_engine()
        by_id: dict[int, NovelBase] = {
            rec.id: _novel_from_record(rec) for rec in eng.get_novel_rows_by_ids(novel_ids)
        }
        missing_ids = list(dict.fromkeys(int(nid) for nid in novel_ids if int(nid) not in by_id))
        if missing_ids:
            rows = self.session.execute(
                select(models.Novel, func.coalesce(models.Author.is_special_follow, False))
                .outerjoin(models.Author, models.Author.author_id == models.Novel.author_id)
                .where(models.Novel.id == any_(cast(Any, missing_ids)))
            ).all()
            for obj, is_sf in rows:
                by_id[int(obj.id)] = _novel_base_from_orm(obj, is_special_follow=bool(is_sf))
        return [by_id[int(nid)] for nid in novel_ids if int(nid) in by_id]

    def get_series_chapters(self, series_id: int) -> list[StoredNovel]:
        """Return all chapters of *series_id* ordered by chapter index then ID."""
        rows = self.session.execute(
            select(models.Novel, func.coalesce(models.Author.is_special_follow, False))
            .outerjoin(models.Author, models.Author.author_id == models.Novel.author_id)
            .where(models.Novel.series_id == series_id)
            .order_by(
                models.Novel.series_index.asc().nulls_last(),
                func.abs(models.Novel.id).asc(),
                models.Novel.id.asc(),
            )
        ).all()
        return [_novel_from_orm(obj, is_special_follow=bool(is_sf)) for obj, is_sf in rows]

    def get_series_book_bundle(self, series_id: int) -> tuple[dict[str, Any] | None, list[StoredNovel]]:
        """Return ``(series_meta, ordered_chapters)`` for *series_id*."""
        srow = self.session.execute(
            select(models.Series, models.Author.author_name)
            .outerjoin(models.Author, models.Author.author_id == models.Series.author_id)
            .where(models.Series.series_id == series_id)
        ).one_or_none()
        series_meta: dict[str, Any] | None = None
        if srow is not None:
            s_obj, author_name = srow
            series_meta = {
                "series_id": int(s_obj.series_id),
                "series_name": s_obj.series_name,
                "source": str(s_obj.source),
                "source_key": str(s_obj.source_key) if s_obj.source_key else None,
                "author_id": int(s_obj.author_id) if s_obj.author_id is not None else None,
                "author_name": author_name,
            }
        chapters = self.get_series_chapters(series_id)
        return series_meta, chapters

    def get_folded_series_card(self, series_id: int) -> NovelBase | None:
        """Return the folded ``NovelBase`` card for *series_id* directly from PostgreSQL."""
        srow = self.session.execute(
            select(
                models.Series.series_name,
                models.Series.author_id,
                models.Author.author_name,
                func.coalesce(models.Author.is_special_follow, False),
            )
            .select_from(models.Series)
            .outerjoin(models.Author, models.Author.author_id == models.Series.author_id)
            .where(models.Series.series_id == series_id)
        ).one_or_none()

        rows = self.session.execute(
            select(
                models.Novel.id,
                models.Novel.title,
                models.Novel.author_id,
                models.Novel.author_name,
                models.Novel.series_id,
                models.Novel.series_name,
                models.Novel.series_index,
                models.Novel.text,
                models.Novel.like,
                models.Novel.view,
                models.Novel.has_epub,
                models.Novel.create_time,
                models.Novel.source,
                models.Novel.is_favourite,
                func.coalesce(models.Author.is_special_follow, False),
                models.Novel.tags,
            )
            .select_from(models.Novel)
            .outerjoin(models.Author, models.Author.author_id == models.Novel.author_id)
            .where(models.Novel.series_id == series_id)
            .order_by(models.Novel.id)
        ).all()
        if not rows:
            return None

        chapter_tuples = [
            SlimNovelRecord(
                int(r[0]),
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                r[6],
                int(r[7]),
                int(r[8]),
                int(r[9]),
                int(r[10]),
                datetime_to_us(r[11]),
                r[12],
                bool(r[13]),
                bool(r[14]),
                list(r[15]) if r[15] else [],
                1,
            )
            for r in rows
        ]
        folded = fold_series_rows(
            series_id,
            chapter_tuples,
            series_name=srow[0] if srow is not None else None,
            series_author_id=srow[1] if srow is not None else None,
            series_author_name=srow[2] if srow is not None else None,
            series_author_sf=bool(srow[3]) if srow is not None else None,
        )
        return _novel_from_record(folded)

    def get_novels_by_ids(self, novel_ids: list[int]) -> list[StoredNovel]:
        """Return stored novel projections for the given IDs, in the given order."""
        if not novel_ids:
            return []
        rows = (
            self.session.execute(select(models.Novel).where(models.Novel.id.in_(novel_ids))).scalars().all()
        )
        novels = [_novel_from_orm(n) for n in rows]
        by_id = {n.id: n for n in novels}
        return [by_id[nid] for nid in novel_ids if nid in by_id]

    def get_latest_novel_by_ids(self, novel_ids: list[int]) -> StoredNovel | None:
        """Return the highest-ID stored novel among *novel_ids* (without ``is_special_follow``)."""
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
        """Return one keyset page of ``(id, path, has_epub, source)`` ordered by ``id``."""
        stmt = select(models.Novel.id, models.Novel.path, models.Novel.has_epub, models.Novel.source)
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
            models.FailedNovel.source == SOURCE_PIXIV,
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
