"""Slim PostgreSQL snapshot/delta projection helpers for the in-memory query engine.

Stays inside ``copixiv.db`` so SQLAlchemy queries live strictly in the data layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, distinct, func, select
from sqlalchemy.orm import Session, aliased

from copixiv.core.models import EpubStatus
from copixiv.core.search import EXCLUDE_BLOCKED_SETTING_KEY, resolve_exclude_blocked
from copixiv.core.snapshot_types import SlimNovelRecord
from copixiv.core.time import datetime_to_us
from copixiv.db import delta as engine_delta
from copixiv.db import models

_I32_MAX = 2_147_483_647

__all__ = [
    "PgSnapshotStore",
    "SnapshotDeltaPayload",
    "fold_series_rows",
]


@dataclass(frozen=True)
class SnapshotDeltaPayload:
    delete_ids: list[int]
    upsert_rows: list[SlimNovelRecord]
    blocked_tags: list[str] | None
    exclude_blocked_default: bool | None
    special_follow_changes: list[tuple[int, bool]]
    aliased_tags: list[str] | None
    delete_series_ids: list[int] = field(default_factory=list)


def _slim_novel_select():
    return (
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
        .outerjoin(
            models.Author,
            models.Author.author_id == models.Novel.author_id,
        )
    )


def _series_slim_select():
    series_author = aliased(models.Author)
    return (
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
            models.Series.series_name,
            models.Series.author_id,
            series_author.author_name,
            func.coalesce(series_author.is_special_follow, False),
        )
        .select_from(models.Novel)
        .outerjoin(
            models.Author,
            models.Author.author_id == models.Novel.author_id,
        )
        .outerjoin(
            models.Series,
            models.Series.series_id == models.Novel.series_id,
        )
        .outerjoin(
            series_author,
            series_author.author_id == models.Series.author_id,
        )
    )


def _row_to_tuple(row) -> SlimNovelRecord:
    return SlimNovelRecord(
        int(row[0]),
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
        int(row[7]),
        int(row[8]),
        int(row[9]),
        int(row[10]),
        datetime_to_us(row[11]),
        row[12],
        bool(row[13]),
        bool(row[14]),
        list(row[15]) if row[15] else [],
        1,
    )


def fold_series_rows(
    series_id: int,
    chapters: Sequence[SlimNovelRecord],
    *,
    series_name: str | None = None,
    series_author_id: int | None = None,
    series_author_name: str | None = None,
    series_author_sf: bool | None = None,
) -> SlimNovelRecord:
    """Fold ordered/unordered chapter slim tuples of *series_id* into one logical book tuple."""
    if not chapters:
        raise ValueError(f"fold_series_rows requires at least one chapter (series_id={series_id})")

    rep = max(chapters, key=lambda r: (abs(r[0]), r[0]))
    first_ch_series_name = next((ch[5] for ch in chapters if ch[5]), None)
    book_series_name = (series_name or "").strip() or first_ch_series_name or None
    book_title = book_series_name or rep[1]

    if series_author_id is not None:
        author_id: int | None = int(series_author_id)
        author_name = (
            (series_author_name or "").strip() or rep[3] or next((ch[3] for ch in chapters if ch[3]), None)
        )
        is_sf = bool(series_author_sf)
    else:
        author_ch = rep if rep[2] is not None else next((ch for ch in chapters if ch[2] is not None), rep)
        author_id = author_ch[2]
        author_name = author_ch[3]
        is_sf = bool(author_ch[14])

    total_text = min(sum(max(0, int(ch[7])) for ch in chapters), _I32_MAX)
    max_like = max(int(ch[8]) for ch in chapters)
    max_view = max(int(ch[9]) for ch in chapters)

    statuses = [int(ch[10]) for ch in chapters]
    if EpubStatus.DONE in statuses:
        agg_has_epub = int(EpubStatus.DONE)
    elif all(s == EpubStatus.MISSING_SOURCE for s in statuses):
        agg_has_epub = int(EpubStatus.MISSING_SOURCE)
    elif EpubStatus.PENDING in statuses:
        agg_has_epub = int(EpubStatus.PENDING)
    elif all(s == EpubStatus.NO_IMAGES for s in statuses):
        agg_has_epub = int(EpubStatus.NO_IMAGES)
    else:
        agg_has_epub = int(EpubStatus.NO)

    max_create_time_us = max((ch[11] for ch in chapters if ch[11] is not None), default=None)
    any_fav = any(bool(ch[13]) for ch in chapters)
    union_tags = sorted({tag for ch in chapters for tag in ch[15]})

    return SlimNovelRecord(
        rep[0],
        book_title,
        author_id,
        author_name,
        series_id,
        book_series_name,
        None,
        total_text,
        max_like,
        max_view,
        agg_has_epub,
        max_create_time_us,
        rep[12],
        any_fav,
        is_sf,
        union_tags,
        len(chapters),
    )


def _fetch_total_novel_count(session: Session) -> int:
    standalone = int(
        session.execute(
            select(func.count()).select_from(models.Novel).where(models.Novel.series_id.is_(None))
        ).scalar()
        or 0
    )
    series_count = int(
        session.execute(
            select(func.count(distinct(models.Novel.series_id)))
            .select_from(models.Novel)
            .where(models.Novel.series_id.isnot(None))
        ).scalar()
        or 0
    )
    return standalone + series_count


def _iter_folded_series_rows(rows: Iterable[Any]) -> Iterator[SlimNovelRecord]:
    """Group pre-ordered ``_series_slim_select`` rows by ``series_id`` and yield folded records."""
    current_sid: int | None = None
    group_rows: list[SlimNovelRecord] = []
    group_meta: tuple[str | None, int | None, str | None, bool] = (None, None, None, False)

    for row in rows:
        sid = int(row[4])
        if current_sid is not None and sid != current_sid:
            yield fold_series_rows(
                current_sid,
                group_rows,
                series_name=group_meta[0],
                series_author_id=group_meta[1],
                series_author_name=group_meta[2],
                series_author_sf=group_meta[3],
            )
            group_rows.clear()
        if not group_rows:
            current_sid = sid
            group_meta = (row[16], row[17], row[18], bool(row[19]))
        group_rows.append(_row_to_tuple(row))

    if current_sid is not None and group_rows:
        yield fold_series_rows(
            current_sid,
            group_rows,
            series_name=group_meta[0],
            series_author_id=group_meta[1],
            series_author_name=group_meta[2],
            series_author_sf=group_meta[3],
        )


def _iter_all_novel_row_batches(
    session: Session,
    batch_size: int = 2_000,
) -> Iterator[list[SlimNovelRecord]]:
    """Stream standalone novels + folded series books in consistent batches."""
    standalone_stmt = (
        _slim_novel_select()
        .where(models.Novel.series_id.is_(None))
        .order_by(models.Novel.id)
        .execution_options(stream_results=True, yield_per=batch_size)
    )
    standalone_res = session.execute(standalone_stmt)
    for partition in standalone_res.partitions(batch_size):
        batch = [_row_to_tuple(row) for row in partition]
        if batch:
            yield batch

    series_stmt = (
        _series_slim_select()
        .where(models.Novel.series_id.isnot(None))
        .order_by(models.Novel.series_id, models.Novel.id)
        .execution_options(stream_results=True, yield_per=batch_size)
    )
    series_res = session.execute(series_stmt)
    series_row_stream = (row for partition in series_res.partitions(batch_size) for row in partition)
    folded_buf: list[SlimNovelRecord] = []
    for folded in _iter_folded_series_rows(series_row_stream):
        folded_buf.append(folded)
        if len(folded_buf) >= batch_size:
            yield folded_buf
            folded_buf = []
    if folded_buf:
        yield folded_buf


def _fetch_novel_rows_by_ids(
    session: Session,
    novel_ids: Sequence[int],
    chunk_size: int = 5_000,
) -> list[SlimNovelRecord]:
    """Fetch slim rows for a list of novel IDs."""
    if not novel_ids:
        return []
    out: list[SlimNovelRecord] = []
    base = _slim_novel_select()
    for i in range(0, len(novel_ids), chunk_size):
        chunk = list(novel_ids[i : i + chunk_size])
        rows = session.execute(base.where(models.Novel.id.in_(chunk))).fetchall()
        out.extend(_row_to_tuple(row) for row in rows)
    return out


def _fetch_folded_series_rows(
    session: Session,
    series_ids: Sequence[int],
    chunk_size: int = 2_000,
) -> list[SlimNovelRecord]:
    """Fetch and fold chapters for each existing series in *series_ids*."""
    if not series_ids:
        return []
    out: list[SlimNovelRecord] = []
    base = _series_slim_select()
    for i in range(0, len(series_ids), chunk_size):
        chunk = list(series_ids[i : i + chunk_size])
        rows = session.execute(
            base.where(models.Novel.series_id.in_(chunk)).order_by(models.Novel.series_id, models.Novel.id)
        ).fetchall()
        out.extend(_iter_folded_series_rows(rows))
    return out


def _fetch_blocked_tag_names(session: Session) -> list[str]:
    rows = session.execute(
        select(models.TagPreference.tag).where(
            models.TagPreference.preference == models.TagPreferenceORM.blocked
        )
    ).scalars()
    return list(rows)


def _fetch_exclude_blocked_default(session: Session) -> bool:
    row = session.execute(
        select(models.Setting).where(models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY)
    ).scalar_one_or_none()
    return resolve_exclude_blocked(None, row.value if row is not None else None)


def _fetch_author_sf_flags(
    session: Session,
    author_ids: Sequence[int],
    chunk_size: int = 2_000,
) -> list[tuple[int, bool]]:
    unique = sorted({int(aid) for aid in author_ids})
    if not unique:
        return []
    sf_map: dict[int, bool] = {}
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        rows = session.execute(
            select(models.Author.author_id, models.Author.is_special_follow).where(
                models.Author.author_id.in_(chunk)
            )
        ).all()
        for aid, sf in rows:
            sf_map[int(aid)] = bool(sf)
    return [(aid, sf_map.get(aid, False)) for aid in unique]


def _fetch_aliased_tags(session: Session) -> list[str]:
    rows = session.execute(select(models.TagAlias.source, models.TagAlias.target)).all()
    names: set[str] = set()
    for src, tgt in rows:
        names.add(str(src))
        names.add(str(tgt))
    return sorted(names)


class PgSnapshotStore:
    """SQLAlchemy snapshot/delta store passed to ``copixiv.query_engine.sync``."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _snapshot_session(self) -> Iterator[Session]:
        with self._session_factory() as session:
            bind = self._session_factory.kw.get("bind")
            if isinstance(bind, Engine) and bind.dialect.name == "postgresql":
                session.connection(
                    execution_options={
                        "isolation_level": "REPEATABLE READ",
                        "postgresql_readonly": True,
                    }
                )
            yield session

    def stream_snapshot(
        self,
        consume_total: Callable[[int], None],
        consume_batch: Callable[[list[SlimNovelRecord]], None],
        batch_size: int = 2_000,
    ) -> tuple[list[str], bool, list[str]]:
        """Stream all novels, metadata, and aliased tag names under one consistent snapshot."""
        with self._snapshot_session() as session:
            consume_total(_fetch_total_novel_count(session))
            for batch in _iter_all_novel_row_batches(session, batch_size=batch_size):
                consume_batch(batch)
            blocked_tags = _fetch_blocked_tag_names(session)
            exclude_default = _fetch_exclude_blocked_default(session)
            aliased_tags = _fetch_aliased_tags(session)
            return blocked_tags, exclude_default, aliased_tags

    def fetch_delta_payload(
        self,
        pending: Any,
    ) -> SnapshotDeltaPayload:
        with self._snapshot_session() as session:
            unique_nids = sorted(pending.novel_ids)
            affected_series_ids: set[int] = {int(sid) for sid in getattr(pending, "series_ids", ())}
            fetched_rows = _fetch_novel_rows_by_ids(session, unique_nids) if unique_nids else []

            standalone_upserts: list[SlimNovelRecord] = []
            for row in fetched_rows:
                if row[4] is not None:
                    affected_series_ids.add(int(row[4]))
                else:
                    standalone_upserts.append(row)

            sorted_sids = sorted(affected_series_ids)
            series_upserts = _fetch_folded_series_rows(session, sorted_sids) if sorted_sids else []
            upsert_rows = standalone_upserts + series_upserts
            upserted_ids = {row[0] for row in upsert_rows}
            delete_ids = [nid for nid in unique_nids if nid not in upserted_ids]

            sf_changes = (
                _fetch_author_sf_flags(session, sorted(pending.author_ids)) if pending.author_ids else []
            )
            blocked_tags = _fetch_blocked_tag_names(session) if pending.blocked_tags_dirty else None
            exclude_default = (
                _fetch_exclude_blocked_default(session) if pending.exclude_setting_dirty else None
            )
            aliased_tags = _fetch_aliased_tags(session) if pending.aliases_dirty else None
            return SnapshotDeltaPayload(
                delete_ids=delete_ids,
                delete_series_ids=sorted_sids,
                upsert_rows=upsert_rows,
                blocked_tags=blocked_tags,
                exclude_blocked_default=exclude_default,
                special_follow_changes=sf_changes,
                aliased_tags=aliased_tags,
            )

    def attach_runtime(
        self,
        engine: Any,
        hook: Callable[[Any], None],
    ) -> None:
        engine_delta.attach_engine_runtime(self._session_factory, engine, hook)

    def detach_runtime(self) -> None:
        engine_delta.detach_engine_runtime(self._session_factory)
