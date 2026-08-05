"""Global serialization for SQLite writes.

SQLite allows only one writer at a time (even in WAL mode).  All
database writes in this application must happen inside ``db_write()``
so that write transactions from concurrent tasks never collide with
"database is locked" errors.

Invariant: inside ``db_write()`` you may hold at most one write
transaction, and it must be committed before the lock is released —
the lock covers both the writes AND the commit, otherwise the next
writer could start while SQLite's write lock is still held.

Usage::

    async with db_write():
        async with uow.begin():
            await uow.novels.upsert_novels([...])
            await uow.authors.update_last_update(author_id)

Read-only queries never need the lock (WAL supports concurrent reads).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_db_write_lock = asyncio.Lock()


@asynccontextmanager
async def db_write() -> AsyncIterator[None]:
    """Serialize SQLite write transactions across all tasks.

    The lock is process-wide (module-level ``asyncio.Lock``), matching
    the single-process uvicorn deployment.
    """
    async with _db_write_lock:
        yield
