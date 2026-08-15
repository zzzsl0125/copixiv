"""Optional API-key middleware — pure ASGI.

When ``api_key`` is non-empty, every ``/api/`` request (except OPTIONS
preflight) must carry the matching ``X-API-Key`` header.  An empty key
disables the check entirely, so this is a zero-config safety net that can
be turned on by setting ``security.api_key`` in ``config.yaml``.
"""

from __future__ import annotations

import json

from starlette.types import Receive, Scope, Send


class APIAuthMiddleware:
    """Require ``X-API-Key`` on ``/api/`` requests when a key is configured."""

    def __init__(self, app, api_key: str = ""):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if not self.api_key:
            await self.app(scope, receive, send)
            return

        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and scope["method"] != "OPTIONS"
        ):
            if self._extract_api_key(scope) != self.api_key:
                await self._send_error(send, 401, "Invalid or missing API key")
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _extract_api_key(scope: Scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-api-key":
                return value.decode("latin-1")
        return None

    @staticmethod
    async def _send_error(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
