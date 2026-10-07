"""Author name resolution service (two-phase).

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

Lives in the application layer (not domain) because it orchestrates
I/O through the Pixiv client and the unit of work.
"""

import asyncio
from dataclasses import dataclass

from sqlalchemy.orm import Session

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
    silently omitted — the name stays a cache, and what re-resolves it is a
    **later fetch of the same work / author**.  The ``sync_empty_name``
    task scans that shape too, but it ships **disabled**
    (``scheduled_task.is_enabled`` defaults to false), so it is not a
    guarantee (LIBRARY_DESIGN.md §3.3).

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
    # 丢掉空的 author_id：``sync_empty_name`` 的输入集来自 ``novel.author_id``，
    # 而"行有 NULL 名"并不保证"行有 id"。没有 id 就没有可查的东西，
    # ``sorted()`` 混进 None 还会直接 TypeError，把一个可跳过的行变成整任务失败。
    #
    # 这是**纵深防御**：不变量由承诺 + 编辑器补写共同守（LIBRARY_DESIGN.md §3.3
    # 「每本书都有作者名可显示」），这里只是不让越界的输入把任务打崩。
    missing = sorted(aid for aid in author_ids - set(resolved.keys()) if aid)
    api_names: dict[int, str] = {}
    if missing:
        results = await asyncio.gather(
            *[fetch_author_name(client, aid) for aid in missing],
        )
        for aid, name in zip(missing, results):
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
    source: str  # 'local' | 'pixiv' | 'unknown'


async def lookup_author_profile(
    author_id: int,
    *,
    session: Session,
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
    known = await SQLAlchemyAuthorRepository(session).get_names_by_ids({author_id})
    if author_id in known:
        return AuthorProfile(author_id, known[author_id], "local")

    name = await fetch_author_name(client, author_id)
    if name:
        return AuthorProfile(author_id, name, "pixiv")
    return AuthorProfile(author_id, None, "unknown")


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
