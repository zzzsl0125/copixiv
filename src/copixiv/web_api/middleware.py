"""HTTP middleware — request access logging via loguru.

Pure-ASGI middleware (no ``BaseHTTPMiddleware``) so it sees the real
response status code from exception handlers and keeps ``log/access.log``
in sync with actual responses.
"""

import time

from starlette.types import Receive, Scope, Send

from copixiv.app.logger import logger


class AccessLogMiddleware:
    """Log every HTTP request to ``access.log`` via loguru."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        status_code = 0

        async def _send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            with logger.contextualize(name="http_access"):
                logger.info(
                    f"{scope['method']} {scope['path']} → "
                    f"{status_code} {elapsed_ms:.1f}ms",
                )
