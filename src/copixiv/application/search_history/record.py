"""Service for recording search history in background tasks.

Extracted from ``web_api/endpoints/novels.py`` to keep business logic out of
the HTTP layer.  Lives in the application layer: it constructs concrete
repositories (the composition root can't reach into FastAPI
``BackgroundTasks`` callbacks), which is an accepted compromise.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from copixiv.infrastructure.repositories.search_history import SQLAlchemySearchHistoryRepository
from copixiv.infrastructure.repositories.author import SQLAlchemyAuthorRepository
from copixiv.infrastructure.repositories.series import SQLAlchemySeriesRepository


def record_search_history(
    queries_dict: dict[str, str],
    session_factory: Callable[[], Session],
) -> None:
    """Record search history entries for a set of query parameters.

    Designed to run inside a FastAPI ``BackgroundTasks`` callback so it
    creates its own short-lived session (the request-scoped session is
    already closed by the time the background task fires).

    Args:
        queries_dict: Mapping of query value → query type (e.g.
            ``{"12345": "author_id", "keyword": "keyword"}``).
        session_factory: A callable that returns a new SQLAlchemy
            ``Session`` (typically ``app.state.session_factory``).
    """
    import asyncio as a

    from copixiv.app.logger import logger

    session = session_factory()
    try:
        history_repo = SQLAlchemySearchHistoryRepository(session)
        author_repo = SQLAlchemyAuthorRepository(session)
        series_repo = SQLAlchemySeriesRepository(session)

        async def _run() -> None:
            # BackgroundTasks runs in a worker thread with no running event
            # loop, so a single asyncio.run() around the whole batch is the
            # correct way to drive the async repository methods here.
            for value, qtype in queries_dict.items():
                display_value = None
                if qtype == "author_id":
                    author = await author_repo.get_by_id(int(value))
                    if author:
                        display_value = author.get("author_name")
                elif qtype == "series_id":
                    series = await series_repo.get_by_id(int(value))
                    if series:
                        display_value = series.get("series_name")
                await history_repo.add_or_update(qtype, value, display_value)

        a.run(_run())
        session.commit()
    except Exception:
        logger.exception("Failed to record search history")
    finally:
        session.close()
