"""Canonical novel-persist helper — shared by the ingest pipeline and the
single-novel task.

Owns the invariant that every novel upsert is accompanied by author/series
placeholder rows (FK constraints) and refreshed aggregate summaries.

Callers must wrap this in a short write transaction (e.g.
``run_write_transaction(uow, ...)``) — this helper never starts or commits
a transaction itself.
"""

from __future__ import annotations

from copixiv.core.draft import NovelDraft
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import (
    SQLAlchemyNovelRepository,
    SQLAlchemySeriesRepository,
)
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository


async def persist_novels(
    uow: SqlUnitOfWork,
    novels: list[NovelDraft],
    force_update: list[str] | None = None,
) -> int:
    """Upsert *novels* and refresh author/series summaries.

    Ensures author + series placeholder rows exist before the novel
    insert so FK constraints are satisfied for first-seen authors/series.

    Returns the number of newly-inserted novels.
    """
    novels = [n for n in novels if n]
    if not novels:
        return 0

    author_ids = {n.author_id for n in novels}
    series_ids = {n.series_id for n in novels if n.series_id}

    SQLAlchemyAuthorRepository(uow.session).ensure_exists(author_ids)
    SQLAlchemySeriesRepository(uow.session).ensure_exists(series_ids)

    count = await SQLAlchemyNovelRepository(uow.session).upsert_novels(novels, force_update or [])
    await SQLAlchemyAuthorRepository(uow.session).update_summary(author_ids)
    await SQLAlchemySeriesRepository(uow.session).update_summary(series_ids)

    return count
