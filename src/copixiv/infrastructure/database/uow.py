"""Unit of Work — transaction boundary for background tasks."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.repositories.author import AuthorRepository
from copixiv.infrastructure.repositories.series import SeriesRepository
from copixiv.infrastructure.repositories.tag import TagRepository
from copixiv.infrastructure.repositories.token import TokenRepository
from copixiv.infrastructure.repositories.task import TaskRepository
from copixiv.infrastructure.repositories.search_history import (
    SearchHistoryRepository,
)


class SqlUnitOfWork:
    """Manages a transactional boundary around repository operations.

    Usage in background tasks::

        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await uow.novels.upsert_novels([...])
            await uow.commit()

    Usage in FastAPI endpoints (unit-of-work is implicit via Depends(get_db))::

        uow = SqlUnitOfWork(SessionLocal)
        results = await uow.novels.get_novels(...)
        # Commit/rollback handled by the FastAPI dependency
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
        self._novels: NovelRepository | None = None
        self._authors: AuthorRepository | None = None
        self._series: SeriesRepository | None = None
        self._tags: TagRepository | None = None
        self._tokens: TokenRepository | None = None
        self._tasks: TaskRepository | None = None
        self._search_history: SearchHistoryRepository | None = None

    # -- repositories as properties (lazy) -----------------------------------

    @property
    def novels(self) -> NovelRepository:
        if self._novels is None:
            self._novels = NovelRepository(self.session)
        return self._novels

    @property
    def authors(self) -> AuthorRepository:
        if self._authors is None:
            self._authors = AuthorRepository(self.session)
        return self._authors

    @property
    def series(self) -> SeriesRepository:
        if self._series is None:
            self._series = SeriesRepository(self.session)
        return self._series

    @property
    def tags(self) -> TagRepository:
        if self._tags is None:
            self._tags = TagRepository(self.session)
        return self._tags

    @property
    def tokens(self) -> TokenRepository:
        if self._tokens is None:
            self._tokens = TokenRepository(self.session)
        return self._tokens

    @property
    def tasks(self) -> TaskRepository:
        if self._tasks is None:
            self._tasks = TaskRepository(self.session)
        return self._tasks

    @property
    def search_history(self) -> SearchHistoryRepository:
        if self._search_history is None:
            self._search_history = SearchHistoryRepository(self.session)
        return self._search_history

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
        """
        try:
            yield
            await self.commit()
        except Exception:
            await self.rollback()
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

    async def commit(self) -> None:
        if self._session is not None:
            self._session.commit()

    async def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()
