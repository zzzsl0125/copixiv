"""Author application services: name resolution and author deletion.

Resolves Pixiv author names in two independent phases:

1. ``collect_author_names`` — lock-free read + network gather: check the
   local ``author`` table first, then fall back to the Pixiv API for
   unknown authors.  Never acquires ``db_write()`` and must NOT be called
   while holding a write transaction.

2. ``writeback_author_names`` — pure write: persist a collected mapping
   to both ``novel`` and ``author`` tables.  The caller must already hold
   ``db_write()`` and be inside a ``uow.begin()`` transaction.

3. ``lookup_author_profile`` — **single author, read-only**: the query behind
   the metadata editor's author-id field (docs/NOVEL_EDIT_DESIGN.md §5.5).
   Same local-then-Pixiv order, but it writes nothing and never raises: the
   editor's *preview* uses it (``GET /api/novels/author/{id}``), and the
   ``PATCH`` uses it to learn the name it must store alongside the id.

4. ``delete_author`` — delete an author and all their stored data across
   PostgreSQL, disk, and Pixiv follow list.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from copixiv.core.draft import safe_get
from copixiv.core.models import TaskResult
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage


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
    silently omitted — the name stays a cache, and what re-resolves it is a
    **later fetch of the same work / author**.

    This phase is lock-free: it only reads the database and performs
    network I/O.  Do **not** call this while holding a write transaction —
    persist the collected mapping separately via ``writeback_author_names``
    inside ``db_write()`` + ``uow.begin()``.
    """
    if not author_ids:
        return {}

    # -- local ----------------------------------------------------------
    # Sync DB read: ``read_transaction`` opens the short transaction and runs
    # the blocking query in a worker thread (this outer function is async
    # because of the network gather below, not because of the database).
    raw_resolved = await read_transaction(
        uow,
        lambda read_uow: SQLAlchemyAuthorRepository(read_uow.session).get_names_by_ids(author_ids),
    )
    resolved = {aid: name for aid, name in raw_resolved.items() if name}

    # -- remote ---------------------------------------------------------
    # Drop falsy/None author IDs: ``author_id`` is nullable on novels, and
    # mixing None into ``sorted()`` raises TypeError.
    missing = sorted(aid for aid in author_ids - set(resolved.keys()) if aid)
    api_names: dict[int, str] = {}
    if missing:
        results = await asyncio.gather(
            *[fetch_author_name(client, aid) for aid in missing],
        )
        for aid, name in zip(missing, results, strict=True):
            if name:
                api_names[aid] = name

    return {**resolved, **api_names}


async def fetch_author_name(
    client: PixivClient | None,
    author_id: int,
) -> str | None:
    """Ask Pixiv for one author's display name.

    ``None`` on every failure path — no client, API error, empty name.  The
    caller decides whether that is fatal; none of today's callers treat it as
    such (the name is a cache, the ``author_id`` is the identity).
    """
    if client is None:
        return None
    try:
        result = await client.user_detail(author_id)
    except Exception as exc:  # noqa: BLE001 — any transport/auth error degrades
        logger.warning(f"Failed to fetch name for author #{author_id}: {exc}")
        return None
    name = safe_get(result, "user.name", "")
    return name or None


@dataclass(frozen=True)
class AuthorProfile:
    """What the editor can say about one Pixiv author id.

    ``source`` records *where the name came from*, because the editor shows
    it: a locally known name costs nothing and is always available offline,
    a Pixiv-fetched one means a live request just happened, and ``unknown``
    means the id maps to no name we could obtain (the caller may still store
    the id — the mapping is the user's intent, the name is a cache).
    """

    author_id: int
    author_name: str | None
    source: Literal["local", "pixiv", "unknown"]


async def lookup_author_profile(
    author_id: int,
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None,
) -> AuthorProfile:
    """Resolve one author id → name, **read-only**, local table first.

    Used by both halves of the editor's author-id field:

    * ``GET /api/novels/author/{id}`` — the live preview while typing;
    * ``PATCH /api/novels/{id}`` — to learn the name to store next to the id.

    Writes nothing (the placeholder ``author`` row an id needs for its foreign
    key is ``ensure_exists``'s job, and that belongs to the write path).  Never
    raises: an unreachable Pixiv degrades to ``source='unknown'``.
    """
    known = await read_transaction(
        uow,
        lambda tx: SQLAlchemyAuthorRepository(tx.session).get_names_by_ids({author_id}),
    )
    local_name = known.get(author_id)
    if local_name:
        return AuthorProfile(author_id, local_name, "local")

    name = await fetch_author_name(client, author_id)
    if name:
        return AuthorProfile(author_id, name, "pixiv")
    return AuthorProfile(author_id, None, "unknown")


def writeback_author_names(
    mapping: dict[int, str],
    uow: SqlUnitOfWork,
) -> None:
    """Persist resolved author names to ``novel`` and ``author`` tables."""
    author_repo = SQLAlchemyAuthorRepository(uow.session)
    for aid, name in mapping.items():
        author_repo.update_author_name(aid, name)


async def delete_author(
    author_id: int,
    *,
    uow: SqlUnitOfWork,
    file_storage: FileStorage,
    client: PixivClient,
) -> TaskResult:
    """Delete an author and all their stored data from PG, disk, and Pixiv follow list."""
    paths = await run_write_transaction(
        uow,
        lambda uw: SQLAlchemyAuthorRepository(uw.session).delete_author_and_data(author_id),
    )

    for path in paths:
        await asyncio.to_thread(file_storage.delete_novel_files, path)

    async with client.account_rule(force_follow=True):
        await client.user_follow_delete(author_id)

    return TaskResult(summary=f"已删除作者 #{author_id} 及其全部小说")
