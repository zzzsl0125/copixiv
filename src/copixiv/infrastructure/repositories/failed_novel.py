"""FailedNovel repository — track novel download failures for later retry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..database import models


_MAX_RETRIES = 3


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class FailedNovelRepository:
    """Persistently record which novels failed and how many times.

    A novel that fails *max_retries* times is skipped on subsequent runs
    (avoiding wasted API calls for permanently-dead content).  When a
    previously-failed novel is successfully downloaded, its record is
    removed.
    """

    def __init__(self, session):
        self._session = session
        self._max_retries = _MAX_RETRIES

    # -- write ------------------------------------------------------------

    def record(
        self,
        novel_id: int,
        failure_type: str,
        error_message: str,
        title: str | None = None,
    ) -> None:
        """Record (or increment) a failure for *novel_id*.

        Uses SQLite ``ON CONFLICT DO UPDATE`` so the whole operation is
        a single atomic statement — no read-then-write race.  *title* is
        only overwritten when a non-empty value is provided (the batch
        pipeline knows the title; other callers may not), and
        ``last_failed_at`` is always bumped to now.
        """
        now = _now()
        stmt = (
            sqlite_insert(models.FailedNovel)
            .values(
                novel_id=novel_id,
                failure_type=failure_type,
                error_message=str(error_message)[:1000],
                failed_times=1,
                title=title or None,
                last_failed_at=now,
            )
            .on_conflict_do_update(
                index_elements=["novel_id"],
                set_={
                    "failure_type": failure_type,
                    "error_message": str(error_message)[:1000],
                    "failed_times": models.FailedNovel.failed_times + 1,
                    "last_failed_at": now,
                    **({"title": title} if title else {}),
                },
            )
        )
        self._session.execute(stmt)

    def forget(self, novel_id: int) -> None:
        """Remove failure record — called when a novel succeeds."""
        stmt = delete(models.FailedNovel).where(
            models.FailedNovel.novel_id == novel_id
        )
        self._session.execute(stmt)

    def forget_many(self, novel_ids: set[int]) -> None:
        """Bulk-remove failure records for successfully-processed novels."""
        if not novel_ids:
            return
        stmt = delete(models.FailedNovel).where(
            models.FailedNovel.novel_id.in_(novel_ids)
        )
        self._session.execute(stmt)

    def reset_count(self, novel_id: int) -> None:
        """Reset one record's failure count to 0 — the record stays.

        Unblocks automatic retry (skip threshold is ``>= max_retries``)
        while keeping the history row (title / error / first-seen info)
        visible in the 「失败记录」 view.
        """
        self._session.execute(
            update(models.FailedNovel)
            .where(models.FailedNovel.novel_id == novel_id)
            .values(failed_times=0)
        )

    def reset_all(self) -> int:
        """Reset every failure count to 0; returns the number of rows touched."""
        result = self._session.execute(
            update(models.FailedNovel).values(failed_times=0)
        )
        return result.rowcount or 0

    def clear_all(self) -> int:
        """Remove every failure record; returns the number deleted."""
        stmt = delete(models.FailedNovel)
        result = self._session.execute(stmt)
        return result.rowcount or 0

    # -- read -------------------------------------------------------------

    def get_skip_ids(self, novel_ids: set[int]) -> set[int]:
        """Return the subset of *novel_ids* that have failed too many times."""
        if not novel_ids:
            return set()
        stmt = select(models.FailedNovel.novel_id).where(
            models.FailedNovel.novel_id.in_(novel_ids),
            models.FailedNovel.failed_times >= self._max_retries,
        )
        rows = self._session.execute(stmt).fetchall()
        return {row[0] for row in rows}

    # "Page not found" 家族——作品已删除/不可获取，重试无意义。
    # 单路径记录为 "webview_novel 返回空"，历史批量异常体含 "Page not found"。
    _NOT_FOUND_PATTERNS = (
        "%webview_novel 返回空%",
        "%Page not found%",
        "%not found%",
    )

    def list(
        self,
        offset: int = 0,
        limit: int = 100,
    ) -> list[models.FailedNovel]:
        """Return failure records — actionable first, not-found family last.

        Sort order:
        1. "Page not found" 家族（已删除/不可获取，重试无意义）排在最后；
        2. 其余记录按最近失败时间倒序（newest first）；
        3. 同时间按 novel_id 倒序。
        Legacy rows without ``last_failed_at`` sort to the end of their
        group (NULLs last in DESC).
        """
        not_found = or_(
            *[
                models.FailedNovel.error_message.like(p)
                for p in self._NOT_FOUND_PATTERNS
            ]
        )
        stmt = (
            select(models.FailedNovel)
            .order_by(
                case((not_found, 1), else_=0).asc(),
                models.FailedNovel.last_failed_at.desc().nulls_last(),
                models.FailedNovel.novel_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars())

    def count(self) -> int:
        """Total number of failure records."""
        stmt = select(func.count()).select_from(models.FailedNovel)
        return self._session.execute(stmt).scalar_one()

    def all_ids(self) -> list[int]:
        """Every novel id in the ledger, in ledger order.

        Used by ``POST /failed-novels/retry-all`` — the whole ledger is
        the payload, so pagination state on the client never matters.
        """
        stmt = select(models.FailedNovel.novel_id)
        return list(self._session.execute(stmt).scalars())
