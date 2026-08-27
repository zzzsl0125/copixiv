"""Unit of Work — transaction boundary for background tasks."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session


class SqlUnitOfWork:
    """A pure transaction boundary around a SQLAlchemy session.

    The UoW owns its session lifecycle: a session is created lazily from
    ``session_factory`` on first access and closed at the end of every
    ``begin()`` cycle, so each transaction gets a fresh session.

    It carries no repositories — callers construct whatever repository
    they need directly against ``uow.session``, keeping this class free
    of any feature/tasks import.

    Usage::

        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await SQLAlchemyNovelRepository(uow.session).upsert_novels([...])
        # begin() commits on clean exit / rolls back on exception
    """

    def __init__(self, session_factory):
        """*session_factory* is a ``sessionmaker``.

        Sessions are created lazily on first access and closed inside
        ``begin()``, so the UoW manages the full lifecycle.
        """
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session_factory(self):
        """The session factory this UoW was created from."""
        return self._session_factory

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    # -- async context manager -----------------------------------------------

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        """Enter a transactional scope.  Commits on clean exit, rolls back on exception.

        Note: the exit path already commits — callers should NOT call
        ``commit()`` explicitly inside the ``async with`` block.

        ``BaseException`` (not just ``Exception``) is caught so that
        cancellation (``CancelledError``) and generator shutdown
        (``GeneratorExit``, e.g. when a FastAPI dependency is closed
        after a handler error) still roll back explicitly.
        """
        try:
            yield
            await self.commit()
        except BaseException:
            try:
                await self.rollback()
            except BaseException:
                # Never let a rollback failure mask the original error.
                from copixiv.log import logger
                logger.exception("Rollback failed")
            raise
        finally:
            if self._session is not None:
                self._session.close()
                self._session = None

    async def commit(self) -> None:
        if self._session is not None:
            self._session.commit()

    async def flush(self) -> None:
        """Force pending changes into the database, inside the current transaction.

        Needed when the same transaction reads a table it just wrote to:
        the session factory uses ``autoflush=False`` (see
        ``engine.create_session_factory``), so a SELECT does NOT flush
        pending INSERTs automatically — without an explicit flush you
        would read stale rows and get no error.
        """
        if self._session is not None:
            self._session.flush()

    async def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()
