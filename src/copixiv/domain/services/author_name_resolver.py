"""Author name resolution service.

Resolves Pixiv author names by checking the local database first,
then falling back to the Pixiv API for unknown authors.
"""

import asyncio

from copixiv.domain.services.parsing import safe_get
from copixiv.app.logger import logger


async def resolve_author_names(
    author_ids: set[int],
    *,
    client,
    uow,
) -> dict[int, str]:
    """Resolve author names for the given IDs.

    Strategy:

    1. Batch-query the local ``author`` table for already-known names.
    2. For remaining unresolved IDs, call ``client.user_detail``.
    3. Persist newly-resolved names to both ``novel`` and ``author`` tables.

    Returns ``{author_id: author_name}`` for every successfully-resolved
    author.  IDs that could not be resolved (API failure, empty name) are
    silently omitted — they'll be picked up by the ``sync_empty_name``
    maintenance task later.
    """
    if not author_ids:
        return {}

    # -- local ----------------------------------------------------------
    resolved = await uow.authors.get_names_by_ids(author_ids)

    # -- remote ---------------------------------------------------------
    missing = author_ids - set(resolved.keys())
    if not missing:
        return resolved

    results = await asyncio.gather(
        *[client.user_detail(aid) for aid in missing],
        return_exceptions=True,
    )

    api_names: dict[int, str] = {}
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
    if api_names:
        async with uow.begin():
            for aid, name in api_names.items():
                await uow.authors.update_author_name(aid, name)
        resolved.update(api_names)

    return resolved
