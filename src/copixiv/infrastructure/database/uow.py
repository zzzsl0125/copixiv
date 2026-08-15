"""Unit of Work — transaction boundary for background tasks."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository
from copixiv.infrastructure.repositories.author import SQLAlchemyAuthorRepository
from copixiv.infrastructure.repositories.series import SQLAlchemySeriesRepository
from copixiv.infrastructure.repositories.tag import SQLAlchemyTagRepository
from copixiv.infrastructure.repositories.token import SQLAlchemyTokenRepository
from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository
from copixiv.infrastructure.repositories.failed_novel import FailedNovelRepository
from copixiv.infrastructure.repositories.search_history import (
    SQLAlchemySearchHistoryRepository,
)


class SqlUnitOfWork:
    """Manages a transactional boundary around repository operations.

    Usage in background tasks::

        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await uow.novels.upsert_novels([...])
        # begin() commits on clean exit / rolls back on exception

    Usage in FastAPI endpoints (session lifecycle via Depends(get_db))::

        uow = SqlUnitOfWork(db_session)
        async with uow.begin():
            results = await uow.novels.get_novels(...)
        # commit/rollback handled by begin(); the session itself stays
        # owned by the FastAPI dependency (closed by get_db after the request)
    """

    def __init__(self, session_factory_or_session):
        """*session_factory_or_session* may be a ``sessionmaker`` or a ``Session``.

        When a ``sessionmaker`` is passed, sessions are created and closed
        inside ``begin()``.  When a ``Session`` is passed directly (FastAPI
        Depends), the session lifecycle is managed externally.
        """
        from sqlalchemy.orm import sessionmaker

        if isinstance(session_factory_or_session, sessionmaker):
            self._session_factory = session_factory_or_session
            self._session: Session | None = None
            self._owns_session = True
        else:
            self._session_factory = None
            self._session = session_factory_or_session
            self._owns_session = False

        # Repositories are populated lazily via properties
        self._novels: SQLAlchemyNovelRepository | None = None
        self._authors: SQLAlchemyAuthorRepository | None = None
        self._series: SQLAlchemySeriesRepository | None = None
        self._tags: SQLAlchemyTagRepository | None = None
        self._tokens: SQLAlchemyTokenRepository | None = None
        self._tasks: SQLAlchemyTaskRepository | None = None
        self._search_history: SQLAlchemySearchHistoryRepository | None = None
        self._failed_novels: FailedNovelRepository | None = None

    # -- repositories as properties (lazy) -----------------------------------

    @property
    def novels(self) -> SQLAlchemyNovelRepository:
        if self._novels is None:
            self._novels = SQLAlchemyNovelRepository(self.session)
        return self._novels

    @property
    def authors(self) -> SQLAlchemyAuthorRepository:
        if self._authors is None:
            self._authors = SQLAlchemyAuthorRepository(self.session)
        return self._authors

    @property
    def series(self) -> SQLAlchemySeriesRepository:
        if self._series is None:
            self._series = SQLAlchemySeriesRepository(self.session)
        return self._series

    @property
    def tags(self) -> SQLAlchemyTagRepository:
        if self._tags is None:
            self._tags = SQLAlchemyTagRepository(self.session)
        return self._tags

    @property
    def tokens(self) -> SQLAlchemyTokenRepository:
        if self._tokens is None:
            self._tokens = SQLAlchemyTokenRepository(self.session)
        return self._tokens

    @property
    def tasks(self) -> SQLAlchemyTaskRepository:
        if self._tasks is None:
            self._tasks = SQLAlchemyTaskRepository(self.session)
        return self._tasks

    @property
    def search_history(self) -> SQLAlchemySearchHistoryRepository:
        if self._search_history is None:
            self._search_history = SQLAlchemySearchHistoryRepository(self.session)
        return self._search_history

    @property
    def failed_novels(self) -> FailedNovelRepository:
        if self._failed_novels is None:
            self._failed_novels = FailedNovelRepository(self.session)
        return self._failed_novels

    @property
    def session_factory(self):
        """The session factory this UoW was created from (None when a
        ready-made Session was injected)."""
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
                from copixiv.app.logger import logger
                logger.exception("Rollback failed")
            raise
        finally:
            if self._owns_session and self._session is not None:
                self._session.close()
                self._session = None
                # Clear cached repositories — they hold a reference to the
                # now-closed session and must be re-created for the next
                # begin() cycle.
                self._novels = None
                self._authors = None
                self._series = None
                self._tags = None
                self._tokens = None
                self._tasks = None
                self._search_history = None
                self._failed_novels = None

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
