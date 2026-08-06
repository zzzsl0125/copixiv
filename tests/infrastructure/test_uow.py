"""Tests for SqlUnitOfWork transaction semantics (in-memory SQLite)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Author
from copixiv.infrastructure.database.uow import SqlUnitOfWork


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return create_session_factory(engine)


class TestBeginTransactionSemantics:
    """begin() must commit on clean exit and roll back on exception."""

    async def test_clean_exit_commits(self, session_factory):
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            uow.session.add(Author(author_id=1, author_name="alice"))

        with session_factory() as s:
            author = s.get(Author, 1)
            assert author is not None
            assert author.author_name == "alice"

    async def test_exception_rolls_back_and_reraises(self, session_factory):
        uow = SqlUnitOfWork(session_factory)
        with pytest.raises(RuntimeError, match="boom"):
            async with uow.begin():
                uow.session.add(Author(author_id=2, author_name="bob"))
                raise RuntimeError("boom")

        with session_factory() as s:
            assert s.get(Author, 2) is None

    async def test_external_session_not_closed(self, session_factory):
        """Depends(get_db) owns the session — begin() must not close it."""
        session = session_factory()
        uow = SqlUnitOfWork(session)  # owns_session=False
        async with uow.begin():
            uow.session.add(Author(author_id=3, author_name="carol"))

        # Session still usable after begin(): committed row is visible.
        assert session.get(Author, 3) is not None
        session.close()

    async def test_owned_session_closed_after_begin(self, session_factory):
        """When UoW created the session itself, begin() closes it on exit."""
        uow = SqlUnitOfWork(session_factory)  # owns_session=True
        async with uow.begin():
            uow.session.add(Author(author_id=4, author_name="dave"))

        assert uow._session is None  # closed and reset for the next cycle
        with session_factory() as s:
            assert s.get(Author, 4) is not None
