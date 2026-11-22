"""Service for recording search history in FastAPI background tasks."""

from __future__ import annotations

from collections.abc import Callable

from copixiv.core.search import SearchConditions
from copixiv.db.tx import run_write_transaction_sync
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
    if not conditions:
        return
    uow = uow_factory()
    try:
        run_write_transaction_sync(uow, lambda tx: _record_in_transaction(tx, conditions))
    except Exception:
        logger.exception("Failed to record search history")


def _record_in_transaction(uow: SqlUnitOfWork, conditions: SearchConditions) -> None:
    author_repo = SQLAlchemyAuthorRepository(uow.session)
    series_repo = SQLAlchemySeriesRepository(uow.session)
    history_repo = SQLAlchemySearchHistoryRepository(uow.session)
    for qtype, value in conditions:
        display_value = None
        if qtype == "author_id" and value.isdigit():
            author = author_repo.get_by_id(int(value))
            if author:
                display_value = author.get("author_name")
        elif qtype == "series_id" and value.isdigit():
            series = series_repo.get_by_id(int(value))
            if series:
                display_value = series.get("series_name")
        history_repo.add_or_update(qtype, value, display_value)
    history_repo.prune_old(keep=50)
