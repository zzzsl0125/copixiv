"""Author name resolution service (two-phase).

Resolves Pixiv author names in two independent phases:

1. ``collect_author_names`` — lock-free read + network gather: check the
   local ``author`` table first, then fall back to the Pixiv API for
   unknown authors.  Never acquires ``db_write()`` and must NOT be called
   while holding a write transaction.

2. ``writeback_author_names`` — pure write: persist a collected mapping
   to both ``novel`` and ``author`` tables.  The caller must already hold
   ``db_write()`` and be inside a ``uow.begin()`` transaction.

Lives in the application layer (not domain) because it orchestrates
I/O through the Pixiv client and the unit of work.
"""

import asyncio

from copixiv.db.uow import SqlUnitOfWork
from copixiv.pixiv.client import PixivClient
from copixiv.core.services import safe_get
from copixiv.log import logger
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository


async def collect_author_names(
    author_ids: set[int],
    *,
    uow: SqlUnitOfWork,
    client: PixivClient,
) -> dict[int, str]:
    """Collect author names for the given IDs without holding the write lock.

    Strategy:

    1. Batch-query the local ``author`` table for already-known names.
    2. For remaining unresolved IDs, call ``client.user_detail``.

    Returns ``{author_id: author_name}`` for every successfully-resolved
    author.  IDs that could not be resolved (API failure, empty name) are
    silently omitted — they'll be picked up by the ``sync_empty_name``
    maintenance task later.

    This phase is lock-free: it only reads the database and performs
    network I/O.  Do **not** call this while holding a write transaction —
    persist the collected mapping separately via ``writeback_author_names``
    inside ``db_write()`` + ``uow.begin()``.
    """
    if not author_ids:
        return {}

    # -- local ----------------------------------------------------------
    async with uow.begin():
        resolved = await SQLAlchemyAuthorRepository(uow.session).get_names_by_ids(author_ids)

    # -- remote ---------------------------------------------------------
    missing = sorted(author_ids - set(resolved.keys()))
    api_names: dict[int, str] = {}
    if missing:
        results = await asyncio.gather(
            *[client.user_detail(aid) for aid in missing],
            return_exceptions=True,
        )

        for aid, result in zip(missing, results):
            if isinstance(result, Exception):
                logger.warning(
                    f"Failed to fetch name for author #{aid}: {result}"
                )
                continue
            name = safe_get(result, "user.name", "")
            if name:
                api_names[aid] = name

    return {**resolved, **api_names}


async def writeback_author_names(
    mapping: dict[int, str],
    uow: SqlUnitOfWork,
) -> None:
    """Persist resolved author names to ``novel`` and ``author`` tables.

    The caller must already hold ``db_write()`` and be inside a
    ``uow.begin()`` transaction; this function performs no locking of its
    own.  ``update_author_name`` copies the name into both the author row
    and every novel row for that author (backfilling new ``author_name=NULL``
    novel rows from webview downloads).
    """
    for aid, name in mapping.items():
        await SQLAlchemyAuthorRepository(uow.session).update_author_name(aid, name)
