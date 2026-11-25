"""Shared pytest fixtures for copixiv unit, PostgreSQL, and integration tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DB_URL = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv_test"


def _tcp_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_local_pg(host: str, port: int) -> None:
    """Start local pgserver via ``scripts/pg_dev.py`` when targeting local port 5433."""
    if _tcp_reachable(host, port):
        return
    if host in ("127.0.0.1", "localhost") and port == 5433:
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "pg_dev.py"), "start"],
            check=True,
        )


def _ensure_database_exists(test_db_url: str) -> None:
    """Create the target test database on the server if it does not yet exist."""
    url = make_url(test_db_url)
    db_name = url.database
    if not db_name:
        raise ValueError(f"Test database URL must include a database name: {test_db_url!r}")
    if not db_name.replace("_", "").isalnum():
        raise ValueError(f"Refusing non-alphanumeric database name: {db_name!r}")

    admin_url = url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()


def _truncate_all_tables(engine) -> None:
    with engine.begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
            .scalars()
            .all()
        )
        if tables:
            joined = ", ".join(f'"{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))


class FakePixivClient:
    """In-memory programmable stand-in for :class:`copixiv.pixiv.client.PixivClient`."""

    def __init__(self) -> None:
        self.webview_responses: dict[int, Any] = {}
        self.series_responses: dict[int, Any] = {}
        self.user_novels_responses: dict[int, Any] = {}
        self.user_detail_responses: dict[int, Any] = {}
        self.errors: dict[tuple[str, int], Exception] = {}
        self.calls: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def account_rule(self, **_kwargs):
        yield self

    def _maybe_raise(self, method: str, key: int) -> None:
        exc = self.errors.get((method, key))
        if exc is not None:
            raise exc

    async def webview_novel(self, novel_id: int) -> Any:
        self.calls.append(("webview_novel", novel_id))
        self._maybe_raise("webview_novel", novel_id)
        return self.webview_responses.get(novel_id)

    async def novel_series(self, series_id: int, fetch_all: bool = False) -> Any:
        self.calls.append(("novel_series", (series_id, fetch_all)))
        self._maybe_raise("novel_series", series_id)
        return self.series_responses.get(series_id, {"novels": []})

    async def user_novels(self, author_id: int, fetch_all: bool = False) -> Any:
        self.calls.append(("user_novels", (author_id, fetch_all)))
        self._maybe_raise("user_novels", author_id)
        return self.user_novels_responses.get(author_id, {"novels": []})

    async def user_detail(self, user_id: int) -> Any:
        self.calls.append(("user_detail", user_id))
        self._maybe_raise("user_detail", user_id)
        return self.user_detail_responses.get(user_id)

    async def user_follow_add(self, user_id: int) -> dict[str, Any]:
        self.calls.append(("user_follow_add", user_id))
        self._maybe_raise("user_follow_add", user_id)
        return {}


def _reset_test_schema(engine, test_db_url: str) -> None:
    """Reset the test database schema via pure ``alembic upgrade head`` (Phase 6 baseline)."""
    db_name = make_url(test_db_url).database or ""
    assert "test" in db_name.lower(), f"Refusing schema reset on non-test DB: {db_name!r}"

    from alembic import command
    from alembic.config import Config

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", test_db_url.replace("%", "%%"))
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(scope="session")
def pg_test_engine():
    """Session-scoped PostgreSQL engine initialized via Alembic baseline upgrade."""
    from copixiv.db.engine import create_database_engine

    test_db_url = os.environ.get("COPIXIV_TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)
    url = make_url(test_db_url)
    _ensure_local_pg(url.host or "127.0.0.1", url.port or 5433)
    _ensure_database_exists(test_db_url)

    engine = create_database_engine(test_db_url)
    _reset_test_schema(engine, test_db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_connection(pg_test_engine):
    """Function-scoped connection wrapped in an outer transaction that always rolls back."""
    conn = pg_test_engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        if trans.is_active:
            trans.rollback()
        conn.close()


@pytest.fixture
def db_session_factory(db_connection):
    """Session factory bound to the savepoint-isolated connection with attached Rust engine."""
    from copixiv.db.delta import detach_engine_runtime
    from copixiv.db.novel_snapshot import PgSnapshotStore
    from copixiv.query_engine.sync import attach_and_start_engine

    factory = sessionmaker(
        bind=db_connection,
        autocommit=False,
        autoflush=False,
        expire_on_commit=True,
        join_transaction_mode="create_savepoint",
    )
    attach_and_start_engine(PgSnapshotStore(factory), build_initial=True)
    try:
        yield factory
    finally:
        detach_engine_runtime(factory)


@pytest.fixture
def uow(db_session_factory):
    """Function-scoped SqlUnitOfWork backed by the savepoint session factory."""
    from copixiv.db.uow import SqlUnitOfWork

    return SqlUnitOfWork(db_session_factory)


@pytest.fixture
def committed_session_factory(pg_test_engine):
    """Session factory that performs real commits, truncating all tables before and after."""
    from copixiv.db.delta import detach_engine_runtime
    from copixiv.db.novel_snapshot import PgSnapshotStore
    from copixiv.query_engine.sync import attach_and_start_engine

    _truncate_all_tables(pg_test_engine)
    factory = sessionmaker(
        bind=pg_test_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=True,
    )
    attach_and_start_engine(PgSnapshotStore(factory), build_initial=True)
    try:
        yield factory
    finally:
        detach_engine_runtime(factory)
        _truncate_all_tables(pg_test_engine)


@pytest.fixture
def fake_pixiv_client() -> FakePixivClient:
    """Fresh in-memory FakePixivClient per test."""
    return FakePixivClient()
