"""Unit of Work — a synchronous transaction boundary around a Session.

Why synchronous
---------------
SQLAlchemy's ``Session`` is blocking: every ``execute`` / ``flush`` /
``commit`` is a synchronous call into psycopg2.  Wrapping those calls in
``async def`` does not make them async — it only moves the blocking work
onto the **event loop thread**, where it stalls every other request.

The rule this module encodes:

* the data layer is **plain synchronous Python**;
* the async world (FastAPI endpoints, task coroutines) uses it through
  ``get_uow`` for requests (endpoints open explicit ``with uow.begin():`` or
  use :func:`copixiv.db.tx.read_transaction` / ``run_write_transaction``) and
  :func:`copixiv.db.tx.run_write_transaction` for tasks
  (``asyncio.to_thread`` once around the whole transaction).

Nothing in ``features/**`` / ``tasks/**`` should call ``asyncio.to_thread``
for a database operation again: one hop per request is enough, and 62
per-method hops were what made the old code both slower and unreadable.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


class SqlUnitOfWork:
    """A pure transaction boundary around a SQLAlchemy session.

    The session is created lazily on first access and closed at the end of
    every :meth:`begin` cycle, so each transaction gets a fresh one.

    It carries no repositories — callers construct whatever repository
    they need directly against ``uow.session``, keeping this class free of
    any feature/tasks import.

    Usage::

        uow = SqlUnitOfWork(session_factory)
        with uow.begin():
            SQLAlchemyNovelRepository(uow.session).upsert_novels([...])
        # begin() commits on clean exit / rolls back on exception
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._session: Session | None = None
        self._in_tx: bool = False

    @property
    def session_factory(self):
        """The session factory this UoW was created from."""
        return self._session_factory

    @property
    def session(self) -> Session:
        if not self._in_tx:
            raise RuntimeError("Session accessed outside uow.begin()")
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    @contextmanager
    def begin(self, *, read_only: bool = False) -> Iterator[None]:
        """Enter a transactional scope.

        Commits on clean exit unless *read_only* is True (in which case any
        transaction is rolled back instead of committed), and rolls back on
        any exception.
        """
        if self._in_tx or self._session is not None:
            raise RuntimeError("Nested SqlUnitOfWork.begin() is not allowed")
        self._in_tx = True
        try:
            yield
        except BaseException:
            try:
                self.rollback()
            except BaseException:
                # Never let a rollback failure mask the original error.
                from copixiv.log import logger

                logger.exception("Rollback failed")
            raise
        else:
            if read_only:
                self.rollback()
            else:
                self.commit()
        finally:
            self._in_tx = False
            if self._session is not None:
                self._session.close()
                self._session = None

    def commit(self) -> None:
        if self._session is not None:
            self._session.commit()

    def flush(self) -> None:
        """Force pending changes into the database, inside the current transaction.

        Needed when the same transaction reads a table it just wrote to:
        the session factory uses ``autoflush=False`` (see
        ``engine.create_session_factory``), so a SELECT does NOT flush
        pending INSERTs automatically — without an explicit flush you
        would read stale rows and get no error.
        """
        if self._session is not None:
            self._session.flush()

    def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()
