"""Service for recording search history in FastAPI background tasks."""

from __future__ import annotations

from collections.abc import Callable

from copixiv.core.search import SearchConditions
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.search_history.repo import SQLAlchemySearchHistoryRepository
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.log import logger


def record_search_history(
    conditions: SearchConditions,
    uow_factory: Callable[[], SqlUnitOfWork],
) -> None:
    """Record search history entries for a list of search conditions."""
    uow = uow_factory()
    try:
        _record_in_transaction(uow, conditions)
    except Exception:
        logger.exception("Failed to record search history")


def _record_in_transaction(uow: SqlUnitOfWork, conditions: SearchConditions) -> None:
    with uow.begin():
        for qtype, value in conditions:
            try:
                with uow.session.begin_nested():
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
                        qtype,
                        value,
                        display_value,
                    )
            except Exception:
                logger.exception(
                    "Failed to record one search-history entry (%s=%r)",
                    qtype,
                    value,
                )
