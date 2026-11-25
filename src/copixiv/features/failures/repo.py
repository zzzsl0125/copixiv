"""FailedNovel repository — track novel download failures for later retry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

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
        novel_id: int,
        failure_type: str,
        error_message: str,
        title: str | None = None,
    ) -> None:
        """Record (or increment) a failure for *novel_id*."""
        now = _now()
        stmt = (
            pg_insert(models.FailedNovel)
            .values(
                novel_id=novel_id,
                error_message=str(error_message)[:1000],
                failed_times=1,
                title=title or None,
                last_failed_at=now,
            )
            .on_conflict_do_update(
                index_elements=["novel_id"],
                set_={
                    "error_message": str(error_message)[:1000],
                    "failed_times": models.FailedNovel.failed_times + 1,
                    "last_failed_at": now,
                    **({"title": title} if title else {}),
                },
            )
        )
        self.session.execute(stmt)

    def forget(self, novel_id: int) -> bool:
        """Remove failure record; returns True if a record was deleted."""
        stmt = delete(models.FailedNovel).where(models.FailedNovel.novel_id == novel_id)
        result = cast(CursorResult[Any], self.session.execute(stmt))
        return (result.rowcount or 0) > 0

    def forget_many(self, novel_ids: set[int]) -> None:
        """Bulk-remove failure records for successfully processed novels."""
        if not novel_ids:
            return
        stmt = delete(models.FailedNovel).where(models.FailedNovel.novel_id.in_(novel_ids))
        self.session.execute(stmt)

    def reset_count(self, novel_id: int) -> None:
        """Reset one record's failure count to 0 — the record stays."""
        self.session.execute(
            update(models.FailedNovel).where(models.FailedNovel.novel_id == novel_id).values(failed_times=0)
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

    def _ids_by_threshold(self, novel_ids: set[int], predicate) -> set[int]:
        if not novel_ids:
            return set()
        stmt = select(models.FailedNovel.novel_id).where(
            models.FailedNovel.novel_id.in_(novel_ids),
            predicate,
        )
        return set(self.session.execute(stmt).scalars().all())

    def get_skip_ids(self, novel_ids: set[int]) -> set[int]:
        """Return the subset of *novel_ids* that have failed too many times."""
        return self._ids_by_threshold(novel_ids, models.FailedNovel.failed_times >= self._max_retries)

    def get_retryable_ids(self, novel_ids: set[int]) -> set[int]:
        """Return *novel_ids* present in the ledger with ``failed_times < max_retries``."""
        return self._ids_by_threshold(novel_ids, models.FailedNovel.failed_times < self._max_retries)

    def list_failures(
        self,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return failure records — actionable first, not-found family last."""
        not_found = or_(*[models.FailedNovel.error_message.like(p) for p in self._NOT_FOUND_PATTERNS])
        stmt = (
            select(
                models.FailedNovel.novel_id,
                func.coalesce(models.FailedNovel.title, models.Novel.title).label("title"),
                models.FailedNovel.error_message,
                models.FailedNovel.failed_times,
                models.FailedNovel.last_failed_at,
            )
            .outerjoin(models.Novel, models.FailedNovel.novel_id == models.Novel.id)
            .order_by(
                case((not_found, 1), else_=0).asc(),
                models.FailedNovel.last_failed_at.desc().nulls_last(),
                models.FailedNovel.novel_id.desc(),
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
        """Every novel id in the ledger, in ledger order."""
        stmt = select(models.FailedNovel.novel_id)
        return list(self.session.execute(stmt).scalars())
