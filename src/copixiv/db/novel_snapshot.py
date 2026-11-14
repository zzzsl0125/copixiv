"""Slim PostgreSQL snapshot/delta projection helpers for the in-memory query engine.

Stays inside ``copixiv.db`` so SQLAlchemy queries live strictly in the data layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from copixiv.core.search_text import SlimNovelTuple, datetime_to_us
from copixiv.core.services import EXCLUDE_BLOCKED_SETTING_KEY, resolve_active
from copixiv.db import delta as engine_delta
from copixiv.db import models

__all__ = [
    "PgSnapshotStore",
    "SlimNovelTuple",
    "datetime_to_us",
    "fetch_blocked_tag_names",
    "fetch_engine_metadata",
    "fetch_exclude_blocked_default",
    "fetch_novel_rows_by_ids",
    "fetch_special_follow_author_ids",
    "fetch_total_novel_count",
    "iter_all_novel_row_batches",
]


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


def _row_to_tuple(row) -> SlimNovelTuple:
    return (
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
    )


def fetch_total_novel_count(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(models.Novel)).scalar() or 0)


def iter_all_novel_row_batches(
    session: Session,
    batch_size: int = 25_000,
) -> Iterator[list[SlimNovelTuple]]:
    """Stream all novels (without ``content`` / ``caption``) in consistent batches."""
    stmt = (
        _slim_novel_select()
        .order_by(models.Novel.id)
        .execution_options(stream_results=True, yield_per=batch_size)
    )
    result = session.execute(stmt)
    for partition in result.partitions(batch_size):
        yield [_row_to_tuple(row) for row in partition]


def fetch_novel_rows_by_ids(
    session: Session,
    novel_ids: Sequence[int],
    chunk_size: int = 5_000,
) -> list[SlimNovelTuple]:
    """Fetch slim rows for a list of novel IDs."""
    if not novel_ids:
        return []
    out: list[SlimNovelTuple] = []
    base = _slim_novel_select()
    for i in range(0, len(novel_ids), chunk_size):
        chunk = list(novel_ids[i : i + chunk_size])
        rows = session.execute(base.where(models.Novel.id.in_(chunk))).fetchall()
        out.extend(_row_to_tuple(row) for row in rows)
    return out


def fetch_blocked_tag_names(session: Session) -> list[str]:
    rows = session.execute(
        select(models.TagPreference.tag).where(
            models.TagPreference.preference == models.TagPreferenceORM.blocked
        )
    ).scalars()
    return list(rows)


def fetch_exclude_blocked_default(session: Session) -> bool:
    row = session.execute(
        select(models.Setting).where(
            models.Setting.key == EXCLUDE_BLOCKED_SETTING_KEY
        )
    ).scalar_one_or_none()
    return resolve_active(None, row.value if row is not None else None)


def fetch_special_follow_author_ids(session: Session) -> list[int]:
    rows = session.execute(
        select(models.Author.author_id).where(
            models.Author.is_special_follow == True  # noqa: E712
        )
    ).scalars()
    return [int(aid) for aid in rows]


def fetch_engine_metadata(
    session: Session,
) -> tuple[list[str], bool, list[int]]:
    """Fetch ``(blocked_tags, exclude_blocked_default, special_follow_authors)``."""
    return (
        fetch_blocked_tag_names(session),
        fetch_exclude_blocked_default(session),
        fetch_special_follow_author_ids(session),
    )


class PgSnapshotStore:
    """SQLAlchemy snapshot/delta store passed to ``copixiv.query_engine.sync``."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def load_snapshot(
        self,
        batch_size: int = 25_000,
    ) -> tuple[int, list[list[SlimNovelTuple]], tuple[list[str], bool, list[int]]]:
        """Unused shortcut when streaming is desired; see :meth:`stream_into`."""
        with self._session_factory() as session:
            total = fetch_total_novel_count(session)
            batches = list(iter_all_novel_row_batches(session, batch_size=batch_size))
            meta = fetch_engine_metadata(session)
            return total, batches, meta

    def stream_snapshot(
        self,
        consume_total: Callable[[int], None],
        consume_batch: Callable[[list[SlimNovelTuple]], None],
        batch_size: int = 25_000,
    ) -> tuple[list[str], bool, list[int]]:
        with self._session_factory() as session:
            consume_total(fetch_total_novel_count(session))
            for batch in iter_all_novel_row_batches(session, batch_size=batch_size):
                consume_batch(batch)
            return fetch_engine_metadata(session)

    def fetch_delta_payload(
        self,
        *,
        upsert_ids: Sequence[int],
        fetch_blocked: bool,
        fetch_exclude: bool,
    ) -> tuple[list[SlimNovelTuple], list[str] | None, bool | None]:
        with self._session_factory() as session:
            rows = fetch_novel_rows_by_ids(session, upsert_ids) if upsert_ids else []
            blocked = fetch_blocked_tag_names(session) if fetch_blocked else None
            exclude = fetch_exclude_blocked_default(session) if fetch_exclude else None
            return rows, blocked, exclude

    def attach_runtime(
        self,
        engine: Any,
        hook: Callable[[engine_delta.PendingEngineDelta], None],
    ) -> None:
        engine_delta.attach_engine_runtime(self._session_factory, engine, hook)

    def detach_runtime(self) -> None:
        engine_delta.detach_engine_runtime(self._session_factory)
