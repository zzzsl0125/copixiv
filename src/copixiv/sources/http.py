"""Per-source HTTP client and ImageDownloader lifecycle management."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from copixiv.core.sources import ProxyMode, normalize_proxy_url
from copixiv.storage.image_downloader import ImageDownloader

_DEFAULT_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16)


def _make_cookie_stripping_hooks(client_ref: list[httpx.AsyncClient]) -> dict[str, list[Any]]:
    async def _strip_request_cookies(request: httpx.Request) -> None:
        request.headers.pop("cookie", None)
        if client_ref:
            client_ref[0].cookies.clear()

    async def _strip_response_cookies(response: httpx.Response) -> None:
        response.cookies.clear()
        if client_ref:
            client_ref[0].cookies.clear()

    return {
        "request": [_strip_request_cookies],
        "response": [_strip_response_cookies],
    }


@dataclass(frozen=True)
class _SourceHttpSpec:
    proxy: str | None
    strip_cookies: bool
    image_headers: tuple[tuple[str, str], ...]
    timeout: float


@dataclass
class _SourceHttpEntry:
    spec: _SourceHttpSpec
    client: httpx.AsyncClient
    image_downloader: ImageDownloader


class SourceHttpManager:
    """Manages isolated ``httpx.AsyncClient`` and ``ImageDownloader`` instances per source."""

    def __init__(self, global_proxy_url: str | None = None, *, timeout: float = 30.0) -> None:
        self._global_proxy_url = normalize_proxy_url(global_proxy_url)
        self._timeout = timeout
        self._entries: dict[str, _SourceHttpEntry] = {}
        self._retired: list[httpx.AsyncClient] = []

    def resolve_proxy(self, proxy_mode: ProxyMode, proxy_url: str | None) -> str | None:
        """Resolve effective proxy URL for a source's proxy mode."""
        if proxy_mode == "direct":
            return None
        if proxy_mode == "custom":
            return normalize_proxy_url(proxy_url)
        return self._global_proxy_url

    def configure_source(
        self,
        source: str,
        *,
        proxy_mode: ProxyMode = "inherit",
        proxy_url: str | None = None,
        strip_cookies: bool = False,
        image_headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.AsyncClient:
        """Create or hot-swap the HTTP client and image downloader for *source*."""
        effective_proxy = self.resolve_proxy(proxy_mode, proxy_url)
        effective_timeout = self._timeout if timeout is None else timeout
        normalized_headers = tuple(sorted((str(k), str(v)) for k, v in (image_headers or {}).items()))
        spec = _SourceHttpSpec(
            proxy=effective_proxy,
            strip_cookies=strip_cookies,
            image_headers=normalized_headers,
            timeout=effective_timeout,
        )

        existing = self._entries.get(source)
        if existing is not None and existing.spec == spec:
            return existing.client

        client_ref: list[httpx.AsyncClient] = []
        event_hooks = _make_cookie_stripping_hooks(client_ref) if strip_cookies else None
        client = httpx.AsyncClient(
            proxy=effective_proxy,
            timeout=effective_timeout,
            limits=_DEFAULT_LIMITS,
            event_hooks=event_hooks,
        )
        client_ref.append(client)

        downloader = ImageDownloader(
            http_client=client,
            max_concurrency=4,
            default_headers=dict(normalized_headers) if normalized_headers else None,
        )
        self._entries[source] = _SourceHttpEntry(
            spec=spec,
            client=client,
            image_downloader=downloader,
        )
        if existing is not None:
            self._retired.append(existing.client)
        return client

    def get_client(self, source: str) -> httpx.AsyncClient:
        """Return the active ``httpx.AsyncClient`` for *source*, initializing lazily if needed."""
        entry = self._entries.get(source)
        if entry is None:
            self.configure_source(source)
            entry = self._entries[source]
        return entry.client

    def get_image_downloader(self, source: str) -> ImageDownloader:
        """Return the active ``ImageDownloader`` for *source*, initializing lazily if needed."""
        entry = self._entries.get(source)
        if entry is None:
            self.configure_source(source)
            entry = self._entries[source]
        return entry.image_downloader

    async def aclose_retired(self) -> None:
        """Close any clients retired during prior ``configure_source`` calls."""
        retired = self._retired
        self._retired = []
        for client in retired:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def aclose(self) -> None:
        """Close all active and retired HTTP clients."""
        await self.aclose_retired()
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            with contextlib.suppress(Exception):
                await entry.client.aclose()


__all__ = ["SourceHttpManager"]
