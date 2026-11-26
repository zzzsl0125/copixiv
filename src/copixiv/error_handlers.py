"""Domain and database exception to HTTP response mappings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from copixiv.core.exceptions import (
    DomainError,
    NotFoundError,
    ValidationError,
)
from copixiv.log import logger

if TYPE_CHECKING:
    from fastapi import FastAPI

_DOMAIN_HTTP_STATUS = (
    (NotFoundError, 404),
    (ValidationError, 400),
)


def _domain_error_http_status(exc: DomainError) -> int:
    """Map a domain exception to its HTTP status (default 500)."""
    for exc_type, status in _DOMAIN_HTTP_STATUS:
        if isinstance(exc, exc_type):
            return status
    return 500


def _integrity_error_detail(exc: IntegrityError) -> str:
    """Translate PostgreSQL ``IntegrityError`` constraint violations into Chinese."""
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    constraint = (getattr(diag, "constraint_name", None) or "").lower()

    if pgcode == "23505":
        if "tag_preference" in constraint:
            return "该标签偏好已存在"
        is_credential = "token" in constraint or "source_credential" in constraint
        if "follow" in constraint and (is_credential or "pixiv_follow" in constraint):
            return "已有其他账号被设为追更账号，请重试"
        if is_credential:
            return "同名账号已存在"
        if "tag_alias" in constraint:
            return "该标签别名已存在"
        return "记录已存在，不能重复创建"
    if pgcode == "23503":
        return "关联的记录不存在，无法执行此操作"
    if pgcode == "23514":
        return "提交的数据不符合约束条件"
    return "数据约束冲突，请检查输入"


def register_exception_handlers(app: FastAPI) -> None:
    """Attach domain and database exception handlers onto *app*."""
    from fastapi.responses import JSONResponse

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error_handler(_request, exc: IntegrityError):
        logger.warning(f"IntegrityError mapped to 400: {exc.orig or exc}")
        return JSONResponse(
            status_code=400,
            content={"detail": _integrity_error_detail(exc)},
        )
