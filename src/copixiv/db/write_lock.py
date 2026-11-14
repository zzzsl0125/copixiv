"""Write transactions — the one place a task crosses into the sync data layer.

Background tasks are coroutines (they must be: the kernel cancels them on
timeout, and they fan out network work with ``asyncio.gather``).  The
database layer is blocking Python.  Rather than sprinkling
``asyncio.to_thread`` through 62 repository methods, the hop happens **once
per transaction**, here.

Two entry points:

* :func:`run_write_transaction` — a write transaction, retried on hot-row
  lock conflicts.  Everything inside *fn* is synchronous: it receives the
  ``uow`` and calls repositories directly.
* :func:`read_transaction` — the read-only counterpart, so a task can read
  the database without blocking the loop and without opening a write
  transaction it does not need.

``db_write()`` is kept as a documented no-op boundary marker for code that
still wants to say "this block writes" without any locking.  Under
PostgreSQL MVCC there is no single-writer rule to enforce; correctness
comes from the transaction plus ``ON CONFLICT`` upserts.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from sqlalchemy.exc import OperationalError

from copixiv.log import logger


@contextmanager
def db_write() -> Any:
    """Mark a write block.  Acquires no lock (PostgreSQL is MVCC multi-writer).

    Kept as a named boundary rather than a bare ``with``-less block so the
    intent stays visible at call sites; it has no runtime effect.
    """
    yield


def _is_lock_not_available(exc: BaseException) -> bool:
    """True for PostgreSQL ``LockNotAvailable`` lock-conflict errors.

    A blocked write surfaces as ``OperationalError`` with the psycopg2
    SQLSTATE ``55P03`` (``could not obtain lock ...``).  The failed
    transaction is already aborted, so the caller must retry with a
    **fresh** transaction/session.

    Note: ``lock_timeout`` must be enabled (``SET lock_timeout``) or PG
    waits indefinitely instead of raising 55P03; do not use NOWAIT.
    """
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) == "55P03"


def _run_write_sync(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
) -> Any:
    """Synchronous body of :func:`run_write_transaction` (runs in a worker thread)."""
    for attempt in range(max_attempts):
        try:
            with db_write(), uow.begin():
                return fn(uow)
        except OperationalError as exc:
            if not _is_lock_not_available(exc) or attempt == max_attempts - 1:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            delay *= random.uniform(0.5, 1.5)
            logger.warning(
                "写事务撞锁（第 {} / {} 次尝试），{:.3f}s 后重试：{}",
                attempt + 1, max_attempts, delay, exc,
            )
            # This runs in a worker thread, so a blocking sleep is correct —
            # the event loop keeps serving requests while we back off.
            import time
            time.sleep(delay)
    raise AssertionError("unreachable: retry loop must return or raise")


def run_write_transaction_sync(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Any:
    """Synchronous variant of :func:`run_write_transaction` for sync callers."""
    return _run_write_sync(
        uow,
        fn,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
    )


async def run_write_transaction(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Any:
    """Run the **synchronous** ``fn(uow)`` in one write transaction.

    ``fn`` must be a plain ``def``: everything it does, from the ``BEGIN``
    to the ``COMMIT``, executes in a single worker thread so the session
    never crosses threads (SQLAlchemy ``Session`` is not thread-safe).

    Retries a ``LockNotAvailable`` conflict with exponential backoff +
    jitter on a fresh session; real SQL/programming errors bubble up
    immediately.
    """
    return await asyncio.to_thread(
        _run_write_sync,
        uow,
        fn,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
    )


def _run_read_sync(uow, fn: Callable[[Any], Any]) -> Any:
    with uow.begin():
        return fn(uow)


async def read_transaction(uow, fn: Callable[[Any], Any]) -> Any:
    """Run the **synchronous** ``fn(uow)`` in one read transaction, off the loop.

    A read still needs a transaction so the session is opened and closed
    around it; it just never retries.
    """
    return await asyncio.to_thread(_run_read_sync, uow, fn)
