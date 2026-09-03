"""FastAPI dependencies — session lifecycle, unit of work, query parsing,
and typed access to the application services on ``app.state``.

The ``app.state`` bag is populated by the container's lifespan; it is
read ONLY here, so endpoints depend on typed functions instead of
string-keyed state attributes (docs/MODULARITY.md §M9).
"""

import json
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request

from copixiv.db.uow import SqlUnitOfWork


def get_session_factory(request: Request):
    """The process-wide SQLAlchemy session factory (app.state)."""
    return request.app.state.session_factory


def get_app_config(request: Request):
    """The application config object (app.state)."""
    return request.app.state.config


def get_file_storage(request: Request):
    """The file-storage service (app.state)."""
    return request.app.state.file_storage


def get_task_manager(request: Request):
    """The task-manager facade (app.state)."""
    return request.app.state.task_manager


async def get_uow(
    session_factory=Depends(get_session_factory),
) -> AsyncIterator[SqlUnitOfWork]:
    """Yield a request-scoped unit of work.

    The UoW creates its session from the process-wide ``session_factory``
    and owns the full session lifecycle.  The transaction commits on clean
    exit and rolls back on exception (including handler errors /
    cancellation), so endpoints never touch commit/rollback or the session
    lifecycle themselves.
    """
    uow = SqlUnitOfWork(session_factory)
    async with uow.begin():
        yield uow


async def get_write_uow(
    session_factory=Depends(get_session_factory),
) -> AsyncIterator[SqlUnitOfWork]:
    """Yield a request-scoped UoW inside a write-transaction boundary.

    There is no global write lock any more: ``db_write()`` is a no-op
    transaction-boundary marker (PostgreSQL MVCC multi-writer).  Write
    endpoints use this dependency so their writes go through
    ``uow.begin()`` (commit on success / rollback on error) and stay
    short and set-based, matching the task pipeline's discipline.
    Read-only endpoints keep using :func:`get_uow`.
    """
    from copixiv.db.write_lock import db_write

    uow = SqlUnitOfWork(session_factory)
    async with db_write():
        async with uow.begin():
            yield uow


def parse_json_param(value: str | None, name: str) -> dict | None:
    """Parse a JSON string request parameter into a dict.

    Args:
        value: The raw query/cursor string (``None`` → ``None``).
        name: Parameter name, used in the 400 error message.

    Raises:
        HTTPException: 400 when the string is not valid JSON.
    """
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail=f"Invalid {name} JSON format"
        )
