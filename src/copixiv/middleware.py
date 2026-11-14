"""Pure-ASGI HTTP middlewares for access logging, Host check, and API key auth."""

from __future__ import annotations

import ipaddress
import json
import time
from urllib.parse import parse_qs

from starlette.types import Receive, Scope, Send

from copixiv.log import logger


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


def _extract_host_header(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"host":
            return value.decode("latin-1")
    return ""


def _normalize_host(raw: str) -> str:
    """Strip the port (and IPv6 brackets) from a Host header value."""
    host = raw.strip()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[1:end]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HostValidationMiddleware:
    """Reject requests whose ``Host`` header is not an allow-listed value."""

    def __init__(self, app, allowed_hosts: list[str] | None = None):
        self.app = app
        self.allowed_hosts = allowed_hosts or []
        self._allowed_hosts_lower = frozenset(h.lower() for h in self.allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = _normalize_host(_extract_host_header(scope))
        if self._is_allowed(host):
            await self.app(scope, receive, send)
            return

        await _send_json_error(send, 400, "Invalid Host header")

    def _is_allowed(self, host: str) -> bool:
        lowered = host.lower()
        if _is_ip_literal(host):
            return True
        if lowered == "localhost":
            return True
        return lowered in self._allowed_hosts_lower


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
                await _send_json_error(send, 401, "Invalid or missing API key")
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _extract_api_key(scope: Scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-api-key":
                return value.decode("latin-1")

        query = scope.get("query_string", b"")
        if query:
            params = parse_qs(query.decode("latin-1"))
            values = params.get("api_key")
            if values:
                return values[0]
        return None


async def _send_json_error(send: Send, status: int, detail: str) -> None:
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
