"""FastAPI dependencies — session factory, unit of work, query parsing,
and typed access to the application services on ``app.state``.

The ``app.state`` bag is populated by the container's lifespan; it is
read ONLY here, so endpoints depend on typed functions instead of
string-keyed state attributes.
"""

import json

from fastapi import Depends, HTTPException, Request

from copixiv.db.uow import SqlUnitOfWork


def get_session_factory(request: Request):
    """The process-wide SQLAlchemy session factory (app.state)."""
    return request.app.state.session_factory


def get_app_config(request: Request):
    """The application config object (app.state)."""
    return request.app.state.config


def get_account_pool(request: Request):
    """The process-wide Pixiv account pool (app.state)."""
    return request.app.state.account_pool


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
) -> SqlUnitOfWork:
    """Return an unopened request-scoped unit of work.

    Callers open explicit transaction scopes around each DB unit:
    ``with uow.begin():`` (or ``run_write_transaction`` / ``read_transaction``
    in async endpoints and background tasks).
    """
    return SqlUnitOfWork(session_factory)


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
