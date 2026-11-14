"""Canonical novel-persist helper — shared by the ingest pipeline and the
single-novel task.

Owns the invariant that every novel upsert is accompanied by author/series
placeholder rows (FK constraints) and refreshed aggregate summaries.

Callers must wrap this in a short write transaction (e.g.
``run_write_transaction(uow, ...)``) — this helper never starts or commits
a transaction itself.

Synchronous on purpose: it is pure database work and runs as the *callback*
inside :func:`copixiv.db.write_lock.run_write_transaction`, which owns the
single ``asyncio.to_thread`` hop.  A caller who is already in a transaction
(``with uow.begin()``) calls it directly.
"""

from __future__ import annotations

from copixiv.core.draft import NovelDraft
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.novels.repo import (
    SQLAlchemyNovelRepository,
    SQLAlchemySeriesRepository,
)


def persist_novels(
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

    from copixiv.db.base import refresh_parent_summaries

    author_ids = {n.author_id for n in novels if n.author_id}
    series_ids = {n.series_id for n in novels if n.series_id}

    SQLAlchemyAuthorRepository(uow.session).ensure_exists(author_ids)
    SQLAlchemySeriesRepository(uow.session).ensure_exists(series_ids)

    novel_repo = SQLAlchemyNovelRepository(uow.session)
    old_authors, old_series = novel_repo.get_parent_ids(
        {n.id for n in novels if n.id is not None}
    )
    count = novel_repo.upsert_novels(novels, force_update or [])
    refresh_parent_summaries(
        uow.session,
        author_ids=author_ids | old_authors,
        series_ids=series_ids | old_series,
    )

    return count
