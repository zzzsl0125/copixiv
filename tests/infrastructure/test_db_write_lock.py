"""Concurrency regression tests for the global SQLite write lock.

Before the ``db_write()`` refactor, concurrent fan-out tasks (each
writing through its own session but outside the lock) collided with
"database is locked" because SQLite allows only one writer at a time.
These tests pin down the two invariants that replaced the bug:

1. ``db_write()`` strictly serializes writers.
2. Concurrent coroutines writing through ``db_write()`` + their own
   UoW never raise ``OperationalError: database is locked``.
"""

import asyncio

import pytest
from sqlalchemy import select

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Author
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database.write_lock import db_write

# file_engine comes from tests/conftest.py (file-backed WAL engine —
# a real file is required to reproduce cross-connection write contention).


class TestDbWriteSerializes:
    async def test_db_write_serializes_writers(self):
        """Writers enter and exit the lock strictly one at a time."""
        order: list[str] = []

        async def writer(name: str, delay: float) -> None:
            async with db_write():
                order.append(f"{name}-enter")
                await asyncio.sleep(delay)
                order.append(f"{name}-exit")

        await asyncio.gather(writer("A", 0.05), writer("B", 0), writer("C", 0))

        assert order == [
            "A-enter", "A-exit",
            "B-enter", "B-exit",
            "C-enter", "C-exit",
        ]


@pytest.mark.slow
class TestConcurrentWrites:
    """Fan-out pattern: N coroutines, each with its own UoW, all writing."""

    N_WRITERS = 16

    async def test_concurrent_writes_no_lock_error(self, file_engine):
        """Mirrors _fan_out_author_fetch: per-coroutine UoW + db_write()."""
        session_factory = create_session_factory(file_engine)

        async def worker(i: int) -> None:
            uow = SqlUnitOfWork(session_factory)
            # Write — serialized by db_write(), own transaction each.
            async with db_write():
                async with uow.begin():
                    uow.authors.ensure_exists({i})
                    await uow.authors.update_last_update(i)
            # Read back — no lock needed (WAL allows concurrent readers).
            async with uow.begin():
                author = await uow.authors.get_by_id(i)
            assert author is not None

        await asyncio.gather(*[worker(i) for i in range(self.N_WRITERS)])

        with session_factory() as session:
            rows = session.execute(select(Author)).scalars().all()
        assert len(rows) == self.N_WRITERS
