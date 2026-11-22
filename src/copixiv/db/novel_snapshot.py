"""Slim PostgreSQL snapshot/delta projection helpers for the in-memory query engine.

Stays inside ``copixiv.db`` so SQLAlchemy queries live strictly in the data layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from copixiv.core.search import EXCLUDE_BLOCKED_SETTING_KEY, resolve_exclude_blocked
from copixiv.core.snapshot_types import SlimNovelRecord
from copixiv.core.time import datetime_to_us
from copixiv.db import delta as engine_delta
from copixiv.db import models

__all__ = [
    "PgSnapshotStore",
    "SnapshotDeltaPayload",
]


@dataclass(frozen=True)
class SnapshotDeltaPayload:
    delete_ids: list[int]
    upsert_rows: list[SlimNovelRecord]
    blocked_tags: list[str] | None
    exclude_blocked_default: bool | None
    special_follow_changes: list[tuple[int, bool]]
    aliased_tags: list[str] | None


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
    )


def _fetch_total_novel_count(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(models.Novel)).scalar() or 0)


def _iter_all_novel_row_batches(
    session: Session,
    batch_size: int = 2_000,
) -> Iterator[list[SlimNovelRecord]]:
    """Stream all novels (without ``content`` / ``caption``) in consistent batches."""
    stmt = (
        _slim_novel_select()
        .order_by(models.Novel.id)
        .execution_options(stream_results=True, yield_per=batch_size)
    )
    result = session.execute(stmt)
    for partition in result.partitions(batch_size):
        yield [_row_to_tuple(row) for row in partition]


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
            upsert_rows = _fetch_novel_rows_by_ids(session, unique_nids) if unique_nids else []
            found_nids = {row[0] for row in upsert_rows}
            delete_ids = [nid for nid in unique_nids if nid not in found_nids]

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
