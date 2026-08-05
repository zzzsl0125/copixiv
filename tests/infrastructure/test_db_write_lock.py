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

from sqlalchemy import create_engine, event, select

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Author, Base
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database.write_lock import db_write


def _make_engine(path):
    """File-backed SQLite engine with WAL + busy_timeout, like production.

    A real file (not ``:memory:``) is required: in-memory databases are
    per-connection, so they cannot reproduce cross-connection write
    contention.
    """
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        pool_size=16,
        max_overflow=0,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine


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


class TestConcurrentWrites:
    """Fan-out pattern: N coroutines, each with its own UoW, all writing."""

    N_WRITERS = 16

    async def test_concurrent_writes_no_lock_error(self, tmp_path):
        """Mirrors _fan_out_author_fetch: per-coroutine UoW + db_write()."""
        engine = _make_engine(tmp_path / "concurrent.db")
        session_factory = create_session_factory(engine)

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
