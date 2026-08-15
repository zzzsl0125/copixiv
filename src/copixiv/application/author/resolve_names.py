"""Author name resolution service.

Resolves Pixiv author names by checking the local database first,
then falling back to the Pixiv API for unknown authors.

Lives in the application layer (not domain) because it orchestrates
I/O through the Pixiv client and the unit of work.
"""

import asyncio

from copixiv.domain.ports.pixiv import PixivNovelPort
from copixiv.domain.ports.unit_of_work import UnitOfWork
from copixiv.domain.ports.write_lock import WriteLockPort
from copixiv.domain.services.parsing import safe_get
from copixiv.app.logger import logger


async def resolve_author_names(
    author_ids: set[int],
    *,
    client: PixivNovelPort,
    uow: UnitOfWork,
    write_lock: WriteLockPort,
) -> dict[int, str]:
    """Resolve author names for the given IDs.

    Strategy:

    1. Batch-query the local ``author`` table for already-known names.
    2. For remaining unresolved IDs, call ``client.user_detail``.
    3. Persist all resolved names to both ``novel`` and ``author`` tables.

    Even locally-known names are written back to ``novel.author_name``:
    webview downloads insert new novel rows with ``author_name=NULL``, so a
    known ``author`` row alone is not enough to keep the UI from showing
    "未知".

    Returns ``{author_id: author_name}`` for every successfully-resolved
    author.  IDs that could not be resolved (API failure, empty name) are
    silently omitted — they'll be picked up by the ``sync_empty_name``
    maintenance task later.

    Note: this function acquires ``db_write()`` itself for the persist
    step — callers must NOT invoke it while already holding the write
    lock (``asyncio.Lock`` is not re-entrant).
    """
    if not author_ids:
        return {}

    # -- local ----------------------------------------------------------
    resolved = await uow.authors.get_names_by_ids(author_ids)

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

    # -- persist --------------------------------------------------------
    # Persist happens inside the global write lock (db_write) so that
    # name updates never collide with concurrent task writes.
    # Always backfill locally-known names too: new webview novels carry
    # author_name=None, and update_author_name() copies the name into both
    # the author row and every novel row for that author.
    to_persist = {**resolved, **api_names}
    if to_persist:
        async with write_lock():
            async with uow.begin():
                for aid, name in to_persist.items():
                    await uow.authors.update_author_name(aid, name)
        resolved.update(api_names)

    return resolved
