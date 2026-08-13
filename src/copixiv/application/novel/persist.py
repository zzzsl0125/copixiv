"""Canonical novel-persist helper — shared by the single-novel use case
and the batch task pipeline.

Owns the invariant that every novel upsert is accompanied by author/series
placeholder rows (FK constraints) and refreshed aggregate summaries.

Callers must hold ``db_write()`` and ``uow.begin()`` — this helper never
starts or commits a transaction itself.
"""

from __future__ import annotations

from copixiv.domain.ports.unit_of_work import UnitOfWork


async def persist_novels(
    uow: UnitOfWork,
    novels: list[dict],
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

    author_ids = {n["author_id"] for n in novels}
    series_ids = {sid for n in novels if (sid := n.get("series_id"))}

    uow.authors.ensure_exists(author_ids)
    uow.series.ensure_exists(series_ids)

    count = await uow.novels.upsert_novels(novels, force_update or [])
    await uow.authors.update_summary(author_ids)
    await uow.series.update_summary(series_ids)

    return count
