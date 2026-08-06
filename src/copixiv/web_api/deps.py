"""FastAPI dependencies — session lifecycle, unit of work, query parsing."""

import json
from collections.abc import AsyncIterator, Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from copixiv.infrastructure.database.uow import SqlUnitOfWork


def get_db(request: Request) -> Generator[Session, None, None]:
    """Yield a database session from the application's session factory.

    The session factory is attached to ``app.state`` by the container.
    """
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


async def get_uow(db: Session = Depends(get_db)) -> AsyncIterator[SqlUnitOfWork]:
    """Yield a request-scoped unit of work.

    The transaction commits on clean exit and rolls back on exception
    (including handler errors / cancellation), so endpoints never touch
    commit/rollback or the session lifecycle themselves.
    """
    uow = SqlUnitOfWork(db)
    async with uow.begin():
        yield uow


def parse_queries_json(queries_str: str | None) -> dict | None:
    """Parse a JSON queries string into a dict."""
    if not queries_str:
        return None
    try:
        return json.loads(queries_str)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="Invalid queries JSON format"
        )


def parse_json_cursor(cursor_str: str | None) -> dict | None:
    """Parse a JSON cursor string into a dict."""
    if not cursor_str:
        return None
    try:
        return json.loads(cursor_str)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="Invalid cursor JSON format"
        )
