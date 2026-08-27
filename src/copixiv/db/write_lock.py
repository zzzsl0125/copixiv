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
            await SQLAlchemyNovelRepository(uow.session).upsert_novels([...])
            await SQLAlchemyAuthorRepository(uow.session).update_last_update(author_id)

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
    the single-process uvicorn deployment (systemd runs one worker, and
    ``COPIXIV_RELOAD=1`` is dev-only).  If the service ever moves to
    multiple uvicorn workers, this lock silently stops covering the other
    processes — SQLite's own write serialization (busy_timeout) would
    still prevent corruption, but "database is locked" errors become
    possible.  A file lock (``fcntl.flock`` on a lockfile) or an
    externalized lock would be required then.
    """
    async with _db_write_lock:
        yield


class DbWriteLock:
    """Callable wrapping ``db_write()`` as a serialized-write context manager.

    Injected into application-layer use cases by the task runner so they
    acquire the write lock through ``db_write()`` (compat: this was the
    one concrete adapter behind the now-removed ``WriteLockPort`` protocol).
    """

    def __call__(self) -> AsyncIterator[None]:
        return db_write()
