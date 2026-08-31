"""Write-scope marker (postgres-migration).

``db_write()`` was a *global* write lock that serialized all writes because
SQLite allows only one writer at a time (even in WAL mode).  PostgreSQL is a
MVCC multi-writer engine: a global mutex would only become a write
bottleneck.  Under PG the responsibility for correctness moves to the
**transaction** (a multi-statement ``begin()`` block that commits as one unit)
plus **``ON CONFLICT`` upserts** for idempotency and row-level locking for
concurrency.

What remains here is a *documented* transaction-boundary marker: ``db_write``
is still an ``asynccontextmanager`` so callers that historically wrapped
their write batch in ``async with db_write():`` keep working, but the body
now just yields — it no longer holds any lock.  ``DbWriteLock`` is kept so
the task-runner injection point (which supplied the serialized-write adapter
behind the removed ``WriteLockPort`` protocol) still type-checks.

Usage (unchanged shape, no longer mutually exclusive)::

    async with db_write():
        async with uow.begin():
            await SQLAlchemyNovelRepository(uow.session).upsert_novels([...])
            await SQLAlchemyAuthorRepository(uow.session).update_last_update(author_id)

Read-only queries never needed the lock (WAL supported concurrent reads),
and under PG they still don't.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def db_write() -> AsyncIterator[None]:
    """Mark a write transaction boundary (no global mutual exclusion).

    Under PostgreSQL MVCC there is no single-writer rule to enforce, so this
    context manager intentionally acquires **no lock**.  It exists as:

    * a place to document that a block performs writes,
    * a compatibility shim for callers that wrapped writes in ``db_write()``.

    Correctness for concurrent writers is provided by the enclosing
    transaction (commit/rollback atomicity) and by ``ON CONFLICT``
    upserts, not by a process-wide mutex.
    """
    yield


class DbWriteLock:
    """Callable wrapping ``db_write()`` as a write-boundary context manager.

    Injected into application-layer use cases by the task runner so they
    acquire the write boundary through ``db_write()`` (compat: this was the
    one concrete adapter behind the now-removed ``WriteLockPort`` protocol).
    """

    def __call__(self) -> AsyncIterator[None]:
        return db_write()
