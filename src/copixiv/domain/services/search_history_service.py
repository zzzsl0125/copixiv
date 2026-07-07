"""Service for recording search history in background tasks.

Extracted from ``web_api/endpoints/novels.py`` to keep business logic out of
the HTTP layer.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from copixiv.infrastructure.repositories.search_history import SearchHistoryRepository
from copixiv.infrastructure.repositories.author import AuthorRepository
from copixiv.infrastructure.repositories.series import SeriesRepository


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
        history_repo = SearchHistoryRepository(session)
        author_repo = AuthorRepository(session)
        series_repo = SeriesRepository(session)

        for value, qtype in queries_dict.items():
            display_value = None
            if qtype == "author_id":
                author = a.run(author_repo.get_by_id(int(value)))
                if author:
                    display_value = author.get("author_name")
            elif qtype == "series_id":
                series = a.run(series_repo.get_by_id(int(value)))
                if series:
                    display_value = series.get("series_name")
            a.run(history_repo.add_or_update(qtype, value, display_value))

        session.commit()
    except Exception:
        logger.exception("Failed to record search history")
    finally:
        session.close()
