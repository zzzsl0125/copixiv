"""Domain exception to HTTP response mappings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copixiv.core.exceptions import (
    DomainError,
    NotFoundError,
    ValidationError,
)

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


def register_exception_handlers(app: FastAPI) -> None:
    """Attach domain exception handlers onto *app*."""
    from fastapi.responses import JSONResponse

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )
