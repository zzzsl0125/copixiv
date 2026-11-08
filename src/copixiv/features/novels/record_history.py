"""Service for recording search history in background tasks.

Runs inside a FastAPI ``BackgroundTasks`` callback and drives the
repositories through a :class:`~copixiv.db.uow.SqlUnitOfWork` built from an
injected UoW factory — the endpoint (composition edge) constructs the
concrete ``SqlUnitOfWork``, so this module has zero infrastructure imports
(docs/MODULARITY.md §2.1).

**Plain ``def`` on purpose.**  Starlette's ``BackgroundTasks`` runs a *sync*
callable in a worker thread and only a coroutine on the event loop; this is
pure database work, so a thread is exactly where it belongs.  Before the
sync data layer it had to wrap the whole batch in ``asyncio.run`` because
every repository method was a coroutine — that hop is gone, and declaring
this ``async def`` would now put the blocking session work back on the loop
(what ``uow.py``'s docstring warns about).
"""

from __future__ import annotations

from collections.abc import Callable

from copixiv.db.uow import SqlUnitOfWork
from copixiv.core.services import SearchConditions
from copixiv.log import logger
from copixiv.features.novels.history_repo import SQLAlchemySearchHistoryRepository
from copixiv.features.novels.repo import SQLAlchemySeriesRepository
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository


def record_search_history(
    conditions: SearchConditions,
    uow_factory: Callable[[], SqlUnitOfWork],
) -> None:
    """Record search history entries for a list of search conditions.

    Creates its own short-lived UoW/session (the request-scoped session is
    already closed by the time the background task fires).  Bad entries
    are skipped individually — one malformed value must not discard the
    whole batch.

    Args:
        conditions: Ordered ``(type, value)`` pairs from
            :func:`parse_search_keyword` (e.g.
            ``[("author_id", "12345"), ("keyword", "R-18")]``).
        uow_factory: Zero-argument callable returning a new UnitOfWork
            (the endpoint passes ``lambda: SqlUnitOfWork(session_factory)``).
    """
    uow = uow_factory()

    try:
        _record_in_transaction(uow, conditions)
    except Exception:
        # The batch-level guard that survived the old ``asyncio.run`` wrapper:
        # a transaction-level failure (commit / connection) must not escape
        # into the BackgroundTasks runner and get logged as a request error.
        logger.exception("Failed to record search history")


def _record_in_transaction(uow: SqlUnitOfWork, conditions: SearchConditions) -> None:
    """One synchronous transaction around the whole batch; every repository
    call below is a plain blocking call on this thread's session."""
    with uow.begin():
        for qtype, value in conditions:
            try:
                display_value = None
                if qtype == "author_id" and value.isdigit():
                    author = SQLAlchemyAuthorRepository(uow.session).get_by_id(int(value))
                    if author:
                        display_value = author.get("author_name")
                elif qtype == "series_id" and value.isdigit():
                    series = SQLAlchemySeriesRepository(uow.session).get_by_id(int(value))
                    if series:
                        display_value = series.get("series_name")
                SQLAlchemySearchHistoryRepository(uow.session).add_or_update(
                    qtype, value, display_value,
                )
            except Exception:
                # Per-item guard: skip the bad entry, keep the batch.
                logger.exception(
                    "Failed to record one search-history entry (%s=%r)",
                    qtype, value,
                )
