"""FastAPI dependencies — session lifecycle and query parsing."""

import json
from collections.abc import Generator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session


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
