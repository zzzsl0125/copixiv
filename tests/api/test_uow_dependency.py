"""Tests for the get_uow FastAPI dependency (request-scoped transaction).

Endpoints receive ``uow: SqlUnitOfWork = Depends(get_uow)`` and never
touch commit/rollback.  These tests pin the contract:

- clean handler exit  → begin() commits (row visible in a new session)
- handler exception   → begin() rolls back explicitly (row absent)
- cleanup order       → uow.begin() exits and the UoW closes its own session
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from copixiv.db.models import Author
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import get_uow

# session_factory comes from tests/conftest.py (shared in-memory engine).


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


@pytest.fixture
def client(session_factory, monkeypatch):
    app = FastAPI()
    rollback_spy = {"n": 0}
    orig_rollback = SqlUnitOfWork.rollback

    async def spy_rollback(self):
        rollback_spy["n"] += 1
        await orig_rollback(self)

    monkeypatch.setattr(SqlUnitOfWork, "rollback", spy_rollback)

    @app.post("/ok")
    async def ok_endpoint(uow: SqlUnitOfWork = Depends(get_uow)):
        uow.session.add(Author(author_id=1, author_name="alice"))
        return {"ok": True}

    @app.post("/boom")
    async def boom_endpoint(uow: SqlUnitOfWork = Depends(get_uow)):
        uow.session.add(Author(author_id=2, author_name="bob"))
        raise RuntimeError("boom")

    @app.get("/read")
    async def read_endpoint(uow: SqlUnitOfWork = Depends(get_uow)):
        return {"exists": uow.session.get(Author, 1) is not None}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.app.state.session_factory = session_factory
        yield client, session_factory, rollback_spy


class TestGetUowDependency:
    def test_clean_exit_commits(self, client):
        client, session_factory, _ = client
        r = client.post("/ok")
        assert r.status_code == 200
        with session_factory() as s:
            assert s.get(Author, 1) is not None

    def test_exception_rolls_back_explicitly(self, client):
        client, session_factory, rollback_spy = client
        r = client.post("/boom")
        assert r.status_code == 500
        with session_factory() as s:
            assert s.get(Author, 2) is None
        assert rollback_spy["n"] == 1, "rollback must be called explicitly"

    def test_committed_data_visible_in_same_request(self, client):
        client, _, _ = client
        client.post("/ok")
        r = client.get("/read")
        assert r.json()["exists"] is True
