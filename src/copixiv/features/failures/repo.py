"""FailedNovel repository — track novel download failures for later retry."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from copixiv.core.models import SOURCE_PIXIV
from copixiv.db import models

MAX_RETRIES = 3


def _now() -> datetime:
    return datetime.now(UTC)


class FailedNovelRepository:
    """Persistently record which novels failed and how many times."""

    _NOT_FOUND_PATTERNS = (
        "%webview_novel 返回空%",
        "%not found%",
    )

    def __init__(self, session: Session):
        self.session = session
        self._max_retries = MAX_RETRIES

    def record(
        self,
        novel_id: int | None,
        failure_type: str,
        error_message: str,
        title: str | None = None,
        *,
        source: str = SOURCE_PIXIV,
        source_key: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record (or increment) a failure keyed by ``(source, source_key)``."""
        del failure_type
        key = source_key if source_key is not None else (str(novel_id) if novel_id is not None else "")
        now = _now()
        truncated_error = str(error_message)[:1000]
        update_set: dict[str, Any] = {
            "error_message": truncated_error,
            "failed_times": models.FailedNovel.failed_times + 1,
            "last_failed_at": now,
        }
        if title:
            update_set["title"] = title
        if novel_id is not None:
            update_set["novel_id"] = novel_id
        if extra is not None:
            update_set["extra"] = extra

        stmt = (
            pg_insert(models.FailedNovel)
            .values(
                source=source,
                source_key=key,
                novel_id=novel_id,
                error_message=truncated_error,
                failed_times=1,
                title=title or None,
                last_failed_at=now,
                extra=extra,
            )
            .on_conflict_do_update(
                index_elements=["source", "source_key"],
                set_=update_set,
            )
        )
        self.session.execute(stmt)

    def forget(
        self,
        novel_id: int,
        *,
        source: str | None = None,
        source_key: str | None = None,
    ) -> bool:
        """Remove failure record by ``(source, source_key)`` or ``novel_id``."""
        if source is not None and source_key is not None:
            cond = and_(
                models.FailedNovel.source == source,
                models.FailedNovel.source_key == source_key,
            )
        else:
            cond = models.FailedNovel.novel_id == novel_id
        stmt = delete(models.FailedNovel).where(cond)
        result = cast(CursorResult[Any], self.session.execute(stmt))
        return (result.rowcount or 0) > 0

    def forget_many(self, novel_ids: set[int]) -> None:
        """Bulk-remove failure records for successfully processed novels."""
        if not novel_ids:
            return
        stmt = delete(models.FailedNovel).where(models.FailedNovel.novel_id.in_(novel_ids))
        self.session.execute(stmt)

    def forget_by_source_keys(self, source: str, source_keys: set[str]) -> None:
        """Bulk-remove failure records matching ``(source, source_keys)``."""
        if not source_keys:
            return
        stmt = delete(models.FailedNovel).where(
            models.FailedNovel.source == source,
            models.FailedNovel.source_key.in_(source_keys),
        )
        self.session.execute(stmt)

    def reset_count(self, failed_id: int) -> None:
        """Reset one record's failure count to 0 — the record stays."""
        self.session.execute(
            update(models.FailedNovel).where(models.FailedNovel.id == failed_id).values(failed_times=0)
        )

    def reset_all(self) -> int:
        """Reset every failure count to 0; returns the number of rows touched."""
        result = cast(
            CursorResult[Any],
            self.session.execute(update(models.FailedNovel).values(failed_times=0)),
        )
        return result.rowcount or 0

    def clear_all(self) -> int:
        """Remove every failure record; returns the number deleted."""
        stmt = delete(models.FailedNovel)
        result = cast(CursorResult[Any], self.session.execute(stmt))
        return result.rowcount or 0

    def _ids_by_threshold(self, novel_ids: set[int], predicate: Any) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.FailedNovel.novel_id).where(
            models.FailedNovel.novel_id.in_(novel_ids),
            predicate,
        )
        return {int(nid) for nid in self.session.execute(stmt).scalars().all() if nid is not None}

    def _keys_by_threshold(self, source: str, source_keys: set[str], predicate: Any) -> set[str]:
        if not source_keys:
            return set()
        stmt = select(models.FailedNovel.source_key).where(
            models.FailedNovel.source == source,
            models.FailedNovel.source_key.in_(source_keys),
            predicate,
        )
        return set(self.session.execute(stmt).scalars().all())

    def get_skip_ids(self, novel_ids: set[int]) -> set[int]:
        """Return the subset of *novel_ids* that have failed too many times."""
        return self._ids_by_threshold(novel_ids, models.FailedNovel.failed_times >= self._max_retries)

    def get_retryable_ids(self, novel_ids: set[int]) -> set[int]:
        """Return *novel_ids* present in the ledger with ``failed_times < max_retries``."""
        return self._ids_by_threshold(novel_ids, models.FailedNovel.failed_times < self._max_retries)

    def get_skip_keys(self, source: str, source_keys: set[str]) -> set[str]:
        """Return the subset of *source_keys* for *source* that have failed too many times."""
        return self._keys_by_threshold(
            source,
            source_keys,
            models.FailedNovel.failed_times >= self._max_retries,
        )

    def get_retryable_keys(self, source: str, source_keys: set[str]) -> set[str]:
        """Return *source_keys* for *source* present in the ledger with ``failed_times < max_retries``."""
        return self._keys_by_threshold(
            source,
            source_keys,
            models.FailedNovel.failed_times < self._max_retries,
        )

    def list_failures(
        self,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return failure records — actionable first, not-found family last."""
        not_found = or_(*[models.FailedNovel.error_message.like(p) for p in self._NOT_FOUND_PATTERNS])
        stmt = (
            select(
                models.FailedNovel.id,
                models.FailedNovel.source,
                models.FailedNovel.source_key,
                models.FailedNovel.novel_id,
                func.coalesce(models.FailedNovel.title, models.Novel.title).label("title"),
                models.FailedNovel.error_message,
                models.FailedNovel.failed_times,
                models.FailedNovel.last_failed_at,
                models.FailedNovel.extra,
            )
            .outerjoin(models.Novel, models.FailedNovel.novel_id == models.Novel.id)
            .order_by(
                case((not_found, 1), else_=0).asc(),
                models.FailedNovel.last_failed_at.desc().nulls_last(),
                models.FailedNovel.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return [dict(row._mapping) for row in self.session.execute(stmt).all()]

    def count(self) -> int:
        """Total number of failure records."""
        stmt = select(func.count()).select_from(models.FailedNovel)
        return int(self.session.execute(stmt).scalar_one())

    def all_ids(self) -> list[int]:
        """Every failure record primary key id in the ledger, ordered by id."""
        stmt = select(models.FailedNovel.id).order_by(models.FailedNovel.id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_ids(self, failed_ids: Sequence[int]) -> list[models.FailedNovel]:
        """Return matching :class:`models.FailedNovel` rows for *failed_ids*."""
        if not failed_ids:
            return []
        stmt = (
            select(models.FailedNovel)
            .where(models.FailedNovel.id.in_(list(failed_ids)))
            .order_by(models.FailedNovel.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_retryable_rows(
        self,
        *,
        source: str | None = None,
        limit: int | None = None,
        max_retries: int | None = None,
    ) -> list[models.FailedNovel]:
        """Return failure records eligible for automatic retry (``failed_times < max_retries``)."""
        threshold = self._max_retries if max_retries is None else max_retries
        stmt = select(models.FailedNovel).where(models.FailedNovel.failed_times < threshold)
        if source:
            stmt = stmt.where(models.FailedNovel.source == source)
        stmt = stmt.order_by(models.FailedNovel.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())
