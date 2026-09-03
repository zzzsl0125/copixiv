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

For writes that must not fail on the rare hot-row lock conflict (fan-out
cron tasks), :func:`run_write_transaction` wraps ``db_write()`` +
``uow.begin()`` and retries ``LockNotAvailable`` on a fresh transaction
with exponential backoff + jitter.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.exc import OperationalError

from copixiv.log import logger


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


def _is_lock_not_available(exc: BaseException) -> bool:
    """True for PostgreSQL ``LockNotAvailable`` lock-conflict errors.

    A blocked write surfaces as ``OperationalError`` with the psycopg2
    SQLSTATE ``55P03`` (``could not obtain lock ...``).  The failed
    transaction is already aborted, so the caller must retry with a
    **fresh** transaction/session — exactly what
    :func:`run_write_transaction` does.
    """
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) == "55P03"


async def run_write_transaction(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Any:
    """Run ``fn(uow)`` inside ``db_write()`` + ``uow.begin()`` with retry.

    PostgreSQL is an MVCC multi-writer engine: a write transaction is
    short, and two concurrent fan-out writes may still briefly contend on
    the same hot ``tag`` row.  Rather than serializing every write (the
    old SQLite global lock) or failing the cron loudly, this helper
    retries a lock conflict with exponential backoff + random jitter.

    Retries use the *same* ``uow`` object but a **fresh** session/transaction
    on every attempt: ``uow.begin()`` rolls back and closes the failed
    session before re-raising, so the next attempt starts clean.

    Only ``LockNotAvailable`` (SQLSTATE 55P03) is retried; real SQL/编程
    错误 bubble up immediately.
    """
    for attempt in range(max_attempts):
        try:
            async with db_write():
                async with uow.begin():
                    return await fn(uow)
        except OperationalError as exc:
            if not _is_lock_not_available(exc) or attempt == max_attempts - 1:
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            delay *= random.uniform(0.5, 1.5)
            logger.warning(
                "写事务撞锁（第 {} / {} 次尝试），{:.3f}s 后重试：{}",
                attempt + 1, max_attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable: retry loop must return or raise")


class DbWriteLock:
    """Callable wrapping ``db_write()`` as a write-boundary context manager.

    Injected into application-layer use cases by the task runner so they
    acquire the write boundary through ``db_write()`` (compat: this was the
    one concrete adapter behind the now-removed ``WriteLockPort`` protocol).
    """

    def __call__(self) -> AsyncIterator[None]:
        return db_write()
