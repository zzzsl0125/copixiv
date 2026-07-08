"""FailedNovel repository — track novel download failures for later retry."""

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..database import models


_MAX_RETRIES = 3


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

    def record(self, novel_id: int, failure_type: str, error_message: str) -> None:
        """Record (or increment) a failure for *novel_id*.

        Uses SQLite ``ON CONFLICT DO UPDATE`` so the whole operation is
        a single atomic statement — no read-then-write race.
        """
        stmt = (
            sqlite_insert(models.FailedNovel)
            .values(
                novel_id=novel_id,
                failure_type=failure_type,
                error_message=str(error_message)[:1000],
                failed_times=1,
            )
            .on_conflict_do_update(
                index_elements=["novel_id"],
                set_={
                    "failure_type": failure_type,
                    "error_message": str(error_message)[:1000],
                    "failed_times": models.FailedNovel.failed_times + 1,
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
