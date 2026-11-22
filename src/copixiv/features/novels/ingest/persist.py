"""Canonical novel-persist helper — shared by the ingest pipeline and the
single-novel task.

Owns the invariant that every novel upsert is accompanied by author/series
placeholder rows (FK constraints) and refreshed aggregate summaries.

Callers must wrap this in a short write transaction (e.g.
``run_write_transaction(uow, ...)``) — this helper never starts or commits
a transaction itself.

Synchronous on purpose: it is pure database work and runs as the *callback*
inside :func:`copixiv.db.tx.run_write_transaction`, which owns the
single ``asyncio.to_thread`` hop.  A caller who is already in a transaction
(``with uow.begin()``) calls it directly.
"""

from __future__ import annotations

from sqlalchemy import update

from copixiv.core.draft import NovelDraft
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


def persist_novels(
    uow: SqlUnitOfWork,
    novels: list[NovelDraft],
    force_update: list[str] | None = None,
) -> int:
    """Upsert *novels* and sync author/series display names.

    Returns the number of newly-inserted novels.
    """
    novels = [n for n in novels if n]
    if not novels:
        return 0

    novel_repo = SQLAlchemyNovelRepository(uow.session)
    count = novel_repo.upsert_novels(novels, force_update or [])

    author_names: dict[int, str] = {}
    series_names: dict[int, str] = {}
    series_authors: dict[int, int] = {}
    for n in novels:
        if n.author_id and n.author_name:
            author_names[n.author_id] = n.author_name
        if n.series_id:
            if n.series_name:
                series_names[n.series_id] = n.series_name
            if n.author_id:
                series_authors[n.series_id] = n.author_id

    for aid, name in author_names.items():
        uow.session.execute(
            update(models.Author)
            .where(
                models.Author.author_id == aid,
                models.Author.author_name.is_distinct_from(name),
            )
            .values(author_name=name)
        )
    for sid, name in series_names.items():
        uow.session.execute(
            update(models.Series)
            .where(
                models.Series.series_id == sid,
                models.Series.series_name.is_distinct_from(name),
            )
            .values(series_name=name)
        )
    for sid, aid in series_authors.items():
        uow.session.execute(
            update(models.Series)
            .where(
                models.Series.series_id == sid,
                models.Series.author_id.is_distinct_from(aid),
            )
            .values(author_id=aid)
        )

    return count
