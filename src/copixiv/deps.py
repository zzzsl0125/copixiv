"""FastAPI dependencies — session factory, unit of work, API key auth, and
typed access to application services stored on ``app.state``.

``app.state`` is populated by the container's lifespan; it is read ONLY here,
so endpoints depend on typed ``Annotated`` aliases instead of string-keyed
state lookups.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, APIKeyQuery
from sqlalchemy.orm import Session, sessionmaker

from copixiv.config import AppConfig
from copixiv.db.uow import SqlUnitOfWork
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage


class ExportTaskInfoProtocol(Protocol):
    """Structural interface for export task metadata returned by ``TaskManagerProtocol``."""

    @property
    def zip_name(self) -> str | None: ...


class TaskManagerProtocol(Protocol):
    """Structural interface for the task manager stored on ``app.state``."""

    def reload_cron_jobs(self) -> None: ...
    def run_task(self, name: str, params: dict[str, Any] | None = None) -> int: ...
    def run_task_now(self, task_id: int) -> None: ...
    def cancel_task(self, task_id: int) -> str: ...
    def get_export_info(self, task_id: int) -> ExportTaskInfoProtocol | None: ...


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """The process-wide SQLAlchemy session factory (app.state)."""
    return request.app.state.session_factory


def get_app_config(request: Request) -> AppConfig:
    """The application config object (app.state)."""
    return request.app.state.config


def get_account_pool(request: Request) -> AccountPool:
    """The process-wide Pixiv account pool (app.state)."""
    return request.app.state.account_pool


def get_file_storage(request: Request) -> FileStorage:
    """The file-storage service (app.state)."""
    return request.app.state.file_storage


def get_pixiv_client(request: Request) -> PixivClient:
    """The shared Pixiv client (app.state)."""
    return request.app.state.client


def get_task_manager(request: Request) -> TaskManagerProtocol:
    """The task-manager facade (app.state)."""
    return request.app.state.task_manager


SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]
AppConfigDep = Annotated[AppConfig, Depends(get_app_config)]
AccountPoolDep = Annotated[AccountPool, Depends(get_account_pool)]
FileStorageDep = Annotated[FileStorage, Depends(get_file_storage)]
PixivClientDep = Annotated[PixivClient, Depends(get_pixiv_client)]
TaskManagerDep = Annotated[TaskManagerProtocol, Depends(get_task_manager)]


def get_uow(session_factory: SessionFactoryDep) -> SqlUnitOfWork:
    """Return an unopened request-scoped unit of work."""
    return SqlUnitOfWork(session_factory)


UowDep = Annotated[SqlUnitOfWork, Depends(get_uow)]


def require_api_key(
    request: Request,
    header_key: Annotated[str | None, Depends(api_key_header)] = None,
) -> None:
    """Verify ``X-API-Key`` header when ``security.api_key`` is configured."""
    expected: str = request.app.state.config.security.api_key
    if not expected:
        return
    if header_key is None or not secrets.compare_digest(header_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_download_api_key(
    request: Request,
    header_key: Annotated[str | None, Depends(api_key_header)] = None,
    query_key: Annotated[str | None, Depends(api_key_query)] = None,
) -> None:
    """Verify ``X-API-Key`` header or ``?api_key=`` query param for downloads."""
    expected: str = request.app.state.config.security.api_key
    if not expected:
        return
    provided = header_key if header_key is not None else query_key
    if provided is None or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
