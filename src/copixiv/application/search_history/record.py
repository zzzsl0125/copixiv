"""Service for recording search history in background tasks.

Runs inside a FastAPI ``BackgroundTasks`` callback (worker thread) and
drives the repositories through a ``SqlUnitOfWork`` built from the
injected session factory — one documented infrastructure compromise
(the composition root can't reach into BackgroundTasks callbacks), but
no per-repository concrete imports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy.orm import Session


def record_search_history(
    queries_dict: dict[str, str],
    session_factory: Callable[[], Session],
) -> None:
    """Record search history entries for a set of query parameters.

    Creates its own short-lived UoW/session (the request-scoped session is
    already closed by the time the background task fires).  Bad entries
    are skipped individually — one malformed value must not discard the
    whole batch.

    Args:
        queries_dict: Mapping of query value → query type (e.g.
            ``{"12345": "author_id", "keyword": "keyword"}``).
        session_factory: A callable that returns a new SQLAlchemy
            ``Session`` (typically ``app.state.session_factory``).
    """
    from copixiv.app.logger import logger
    from copixiv.infrastructure.database.uow import SqlUnitOfWork

    uow = SqlUnitOfWork(session_factory)

    async def _run() -> None:
        # A single asyncio.run() around the whole batch drives the async
        # repository methods (BackgroundTasks threads have no event loop).
        async with uow.begin():
            for value, qtype in queries_dict.items():
                try:
                    display_value = None
                    if qtype == "author_id" and value.isdigit():
                        author = await uow.authors.get_by_id(int(value))
                        if author:
                            display_value = author.get("author_name")
                    elif qtype == "series_id" and value.isdigit():
                        series = await uow.series.get_by_id(int(value))
                        if series:
                            display_value = series.get("series_name")
                    await uow.search_history.add_or_update(
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
