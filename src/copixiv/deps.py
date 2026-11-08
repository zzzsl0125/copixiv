"""FastAPI dependencies — session lifecycle, unit of work, query parsing,
and typed access to the application services on ``app.state``.

The ``app.state`` bag is populated by the container's lifespan; it is
read ONLY here, so endpoints depend on typed functions instead of
string-keyed state attributes.

The two UoW dependencies are deliberately **synchronous generators**.
FastAPI runs a sync ``yield`` dependency in the same worker thread as the
node that declares it, so the session is created, used and closed on one
thread — the requirement SQLAlchemy ``Session`` imposes.  Declaring them
``async`` would put the blocking session work back on the event loop.
"""

import json
from collections.abc import Iterator

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


def get_pixiv_client(request: Request):
    """The shared Pixiv client (app.state).

    Endpoints that must ask Pixiv for something the local DB cannot answer
    use this: today only the **author-id lookup** behind the metadata editor
    (``features/authors/resolve_names.py::lookup_author_profile``).  Callers
    must treat failure as *not fatal* — the client raises on rate limits /
    cooling / network errors, and every caller so far degrades to "name
    unknown" instead of failing the request.
    """
    return request.app.state.client


def get_task_manager(request: Request):
    """The task-manager facade (app.state)."""
    return request.app.state.task_manager


def get_uow(
    session_factory=Depends(get_session_factory),
) -> Iterator[SqlUnitOfWork]:
    """Yield a request-scoped unit of work.

    The UoW creates its session from the process-wide ``session_factory``
    and owns the full session lifecycle.  The transaction commits on clean
    exit and rolls back on exception, so endpoints never touch
    commit/rollback or the session lifecycle themselves.

    Synchronous on purpose: FastAPI runs this in the worker thread of the
    endpoint that depends on it, so the ``Session`` never crosses threads.
    """
    uow = SqlUnitOfWork(session_factory)
    with uow.begin():
        yield uow


def get_write_uow(
    session_factory=Depends(get_session_factory),
) -> Iterator[SqlUnitOfWork]:
    """Yield a request-scoped UoW inside a documented write boundary.

    Same lifecycle as :func:`get_uow`; the ``db_write()`` marker only says
    "this request writes" — PostgreSQL MVCC needs no process-wide lock, so
    correctness comes from the transaction plus ``ON CONFLICT`` upserts.
    """
    from copixiv.db.write_lock import db_write

    uow = SqlUnitOfWork(session_factory)
    with db_write(), uow.begin():
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
