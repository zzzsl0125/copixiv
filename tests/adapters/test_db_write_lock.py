"""Write-scope marker tests — ``db_write()`` is now a no-op (postgres-migration).

The global ``db_write()`` mutex existed to serialize SQLite's single writer.
PostgreSQL is an MVCC multi-writer engine, so ``db_write()`` is now only a
transaction-boundary **marker**: it acquires no lock.  The correctness burden
moves to the enclosing transaction + ``ON CONFLICT`` upserts.  These tests pin
that ``db_write()`` is a no-op and that concurrent PG writers don't collide
(no "database is locked" — that failure mode is gone with SQLite).
"""

import asyncio
import pytest
from sqlalchemy import select

from copixiv.db.models import Author
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import DbWriteLock, db_write
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


class TestDbWriteIsNoop:
    async def test_db_write_acquires_no_lock(self):
        """The body of db_write() runs without mutual exclusion: writers can
        interleave (a second writer enters before the first exits)."""
        order: list[str] = []

        async def writer(name: str, delay: float) -> None:
            async with db_write():
                order.append(f"{name}-enter")
                await asyncio.sleep(delay)
                order.append(f"{name}-exit")

        await asyncio.gather(writer("A", 0.05), writer("B", 0), writer("C", 0))

        # Serialization would force A entirely before B; the no-op marker lets
        # B/C enter while A is still sleeping.
        assert order.index("B-enter") < order.index("A-exit")
        assert order.index("C-enter") < order.index("A-exit")

    async def test_db_write_yields_nothing(self):
        result = None

        async with db_write() as marker:
            result = marker is None

        assert result is True

    def test_db_write_lock_callable_returns_db_write(self):
        # The injected adapter is just db_write() itself (compat shim).
        assert DbWriteLock()() is not None


@pytest.mark.slow
class TestConcurrentWrites:
    """Fan-out pattern: N coroutines, each with its own UoW, all writing."""

    N_WRITERS = 16

    async def test_concurrent_writes_no_lock_error(self, session_factory):
        """Mirrors _fan_out_author_fetch: per-coroutine UoW + db_write()."""

        async def worker(i: int) -> None:
            uow = SqlUnitOfWork(session_factory)
            async with db_write():
                async with uow.begin():
                    SQLAlchemyAuthorRepository(uow.session).ensure_exists({i})
                    await SQLAlchemyAuthorRepository(uow.session).update_last_update(i)
            async with uow.begin():
                author = await SQLAlchemyAuthorRepository(uow.session).get_by_id(i)
            assert author is not None

        await asyncio.gather(*[worker(i) for i in range(self.N_WRITERS)])

        with session_factory() as session:
            rows = session.execute(select(Author)).scalars().all()
        assert len(rows) == self.N_WRITERS
