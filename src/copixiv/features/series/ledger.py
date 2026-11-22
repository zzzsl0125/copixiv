"""Dead-series ledger — retires permanently missing (404/410) Pixiv series."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import case, func
from sqlalchemy import delete as _delete
from sqlalchemy import select as _select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from copixiv.db import models
from copixiv.log import logger
from copixiv.pixiv.errors import PixivApiError

RETIRE_AFTER_FAILURES = 3
DEFINITIVE_STATUSES = frozenset({404, 410})


def is_definitive_error(exc: BaseException | None) -> bool:
    """True when *exc* is pixiv definitively saying the series is gone."""
    if exc is None or not isinstance(exc, PixivApiError):
        return False
    return getattr(exc, "status_code", None) in DEFINITIVE_STATUSES


def note_failure(session, series_id: int, exc: BaseException | None) -> bool:
    """Record one failed roster fetch; return True when this retires the series."""
    if not is_definitive_error(exc):
        return False

    status = getattr(exc, "status_code", None)
    detail = f"HTTP {status}: {exc}"
    now = datetime.now(UTC)

    stmt = (
        pg_insert(models.SeriesDeadLetter)
        .values(
            series_id=series_id,
            consecutive_failures=1,
            last_error=detail,
            last_failed_at=now,
        )
        .on_conflict_do_update(
            index_elements=[models.SeriesDeadLetter.series_id],
            set_={
                "consecutive_failures": (models.SeriesDeadLetter.consecutive_failures + 1),
                "last_error": detail,
                "last_failed_at": now,
                "retired_at": _case_retire(models.SeriesDeadLetter.consecutive_failures),
            },
        )
        .returning(models.SeriesDeadLetter.consecutive_failures)
    )
    count = int(session.execute(stmt).scalar_one())

    if count >= RETIRE_AFTER_FAILURES:
        logger.warning(
            f"fix_series_index: 系列 {series_id} 连续 {count} 轮 404，"
            "已判死并从候选集移除（恢复见 series_dead_letter 文档）"
        )
        return True
    return False


def _case_retire(prior_failures):
    return case(
        (prior_failures + 1 >= RETIRE_AFTER_FAILURES, func.now()),
        else_=models.SeriesDeadLetter.retired_at,
    )


def retire_series(session, series_id: int, reason: str) -> None:
    """Immediately retire *series_id* in ``series_dead_letter`` (idempotent)."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(models.SeriesDeadLetter)
        .values(
            series_id=series_id,
            consecutive_failures=RETIRE_AFTER_FAILURES,
            last_error=reason,
            last_failed_at=now,
            retired_at=now,
        )
        .on_conflict_do_update(
            index_elements=[models.SeriesDeadLetter.series_id],
            set_={
                "consecutive_failures": RETIRE_AFTER_FAILURES,
                "last_error": reason,
                "last_failed_at": now,
                "retired_at": now,
            },
        )
    )
    session.execute(stmt)


def note_success(session, series_id: int) -> None:
    """Clear a series' ledger entry after a successful roster fetch."""
    session.execute(_delete(models.SeriesDeadLetter).where(models.SeriesDeadLetter.series_id == series_id))


def tracked_series_ids(session) -> set[int]:
    """Series IDs currently present in ``series_dead_letter``."""
    return set(session.execute(_select(models.SeriesDeadLetter.series_id)).scalars().all())


__all__ = [
    "DEFINITIVE_STATUSES",
    "RETIRE_AFTER_FAILURES",
    "is_definitive_error",
    "note_failure",
    "note_success",
    "retire_series",
    "tracked_series_ids",
]
