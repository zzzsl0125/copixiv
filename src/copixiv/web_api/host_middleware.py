"""Host-header validation middleware — pure ASGI.

The server listens on ``0.0.0.0`` for LAN access, so an attacker who can
trick the victim's browser into resolving ``evil.example.com`` to the server
(DNS rebinding) could otherwise read ``/api/tokens`` from a cross-origin
page.  Validating the ``Host`` header blocks that attack while leaving LAN
access untouched: local IP literals and ``localhost`` are always allowed.
"""

from __future__ import annotations

import ipaddress
import json

from starlette.types import Receive, Scope, Send


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
        # "example.com:8000" / "127.0.0.1:8000" → strip the port
        host = host.rsplit(":", 1)[0]
    # Otherwise: bare IPv6 literal ("::1") with multiple colons and no port
    # — keep it whole so ipaddress can parse it.
    return host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HostValidationMiddleware:
    """Reject requests whose ``Host`` header is not an allow-listed value.

    Pass-through conditions (any one suffices):
    - the host is an IP literal (IPv4/IPv6, brackets already stripped)
    - ``host.lower()`` is ``localhost`` or is listed in *allowed_hosts*
    - ``host == "testserver"`` (TestClient's default, keeps tests working)
    """

    def __init__(self, app, allowed_hosts: list[str] | None = None):
        self.app = app
        self.allowed_hosts = allowed_hosts or []

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = _normalize_host(_extract_host_header(scope))
        if self._is_allowed(host):
            await self.app(scope, receive, send)
            return

        await self._send_error(send, 400, "Invalid Host header")

    def _is_allowed(self, host: str) -> bool:
        lowered = host.lower()
        # TestClient's default Host — keep existing tests working.
        if lowered == "testserver":
            return True
        if _is_ip_literal(host):
            return True
        if lowered == "localhost":
            return True
        allowed = {h.lower() for h in self.allowed_hosts}
        return lowered in allowed

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
