"""Service for recording search history in background tasks.

Runs inside a FastAPI ``BackgroundTasks`` callback (worker thread) and
drives the repositories through a :class:`~copixiv.db.uow.SqlUnitOfWork`
built from an injected UoW factory — the endpoint (composition edge)
constructs the concrete ``SqlUnitOfWork``, so this module has zero
infrastructure imports (docs/MODULARITY.md §2.1).
"""

from __future__ import annotations

import asyncio
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

    async def _run() -> None:
        # A single asyncio.run() around the whole batch drives the async
        # repository methods (BackgroundTasks threads have no event loop).
        async with uow.begin():
            for qtype, value in conditions:
                try:
                    display_value = None
                    if qtype == "author_id" and value.isdigit():
                        author = await SQLAlchemyAuthorRepository(uow.session).get_by_id(int(value))
                        if author:
                            display_value = author.get("author_name")
                    elif qtype == "series_id" and value.isdigit():
                        series = await SQLAlchemySeriesRepository(uow.session).get_by_id(int(value))
                        if series:
                            display_value = series.get("series_name")
                    await SQLAlchemySearchHistoryRepository(uow.session).add_or_update(
                        qtype, value, display_value,
                    )
                except Exception:
                    # Per-item guard: skip the bad entry, keep the batch.
                    logger.exception(
                        "Failed to record one search-history entry (%s=%r)",
                        qtype, value,
                    )

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("Failed to record search history")
