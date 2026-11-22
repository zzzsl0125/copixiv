"""Pure async HTTP image downloader — streams, PIL-verifies, and writes atomically."""

from __future__ import annotations

import asyncio
import contextlib
import io
from collections.abc import Mapping
from pathlib import Path

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from copixiv.log import logger
from copixiv.storage.atomic import write_atomic

_PERMANENT_HTTP_STATUS = frozenset({400, 401, 403, 404, 410})
_DEFAULT_MAX_IMAGE_BYTES = 50 * 1024 * 1024


class _TransientImageError(RuntimeError):
    """Retryable network, CDN 5xx/429, truncated body, or corrupt pixel error."""


class _PermanentImageError(RuntimeError):
    """Non-retryable 4xx / size limit error."""


def _is_decodable_image_bytes(data: bytes) -> bool:
    """True when PIL can open and verify *data* in memory."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def _is_decodable_image(path: Path) -> bool:
    """True when PIL can open and verify *path* as an image."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def _write_and_verify_image(
    save_path: Path,
    data: bytes,
    root: Path | None,
) -> None:
    """Verify PIL decoding in memory, then write *data* atomically."""
    if not _is_decodable_image_bytes(data):
        head = data[:32]
        raise _TransientImageError(f"Not a decodable image ({len(data)} bytes, head={head!r})")
    write_atomic(save_path, data, root=root)


def _check_existing_image(save_path: Path) -> bool:
    """Return True if *save_path* exists and decodes cleanly; unlink if corrupt."""
    if not save_path.exists() or save_path.stat().st_size <= 0:
        return False
    if _is_decodable_image(save_path):
        return True
    logger.warning(f"已存在的图片无法解码，重新下载: {save_path.name}")
    with contextlib.suppress(OSError):
        save_path.unlink()
    return False


class ImageDownloader:
    """Asynchronous image downloader backed by ``httpx.AsyncClient`` and ``tenacity``.

    Keeps zero dependencies on Pixiv models, EPUB builders, or domain features:
    callers pass URLs, destination paths, and optional request headers.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        max_concurrency: int = 4,
        min_interval: float = 0.25,
        default_headers: Mapping[str, str] | None = None,
        timeout: float = 15.0,
        max_attempts: int = 3,
        max_image_bytes: int = _DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        self._http_client = http_client
        self._semaphore = asyncio.Semaphore(max(max_concurrency, 1))
        self._min_interval = min_interval
        self._throttle_lock = asyncio.Lock()
        self._last_start: float = 0.0
        self._default_headers = dict(default_headers) if default_headers else {}
        self._timeout = timeout
        self._max_attempts = max(max_attempts, 1)
        self._max_image_bytes = max_image_bytes

    async def _throttle(self) -> None:
        """Space out download starts across concurrent tasks."""
        if self._min_interval <= 0:
            return
        async with self._throttle_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._min_interval - (now - self._last_start)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start = loop.time()

    async def _download_once(
        self,
        url: str,
        save_path: Path,
        headers: dict[str, str],
        root: Path | None,
    ) -> None:
        async with self._semaphore:
            await self._throttle()
            try:
                async with self._http_client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=self._timeout,
                ) as resp:
                    if resp.status_code in _PERMANENT_HTTP_STATUS:
                        raise _PermanentImageError(f"HTTP {resp.status_code} for {url}")
                    if resp.status_code >= 400:
                        raise _TransientImageError(f"HTTP {resp.status_code} for {url}")

                    has_encoding = bool(resp.headers.get("content-encoding"))
                    raw_len = resp.headers.get("content-length")
                    expected_size = int(raw_len) if raw_len and raw_len.isdigit() else 0
                    if expected_size > self._max_image_bytes:
                        raise _PermanentImageError(f"Image too large ({expected_size} bytes): {url}")

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > self._max_image_bytes:
                            raise _PermanentImageError(f"Image exceeded {self._max_image_bytes} bytes: {url}")
            except (_PermanentImageError, _TransientImageError):
                raise
            except httpx.HTTPError as exc:
                raise _TransientImageError(f"HTTP error for {url}: {exc}") from exc

            downloaded_size = len(buf)
            if expected_size and not has_encoding and downloaded_size != expected_size:
                raise _TransientImageError(
                    f"Incomplete download: expected {expected_size}, got {downloaded_size}"
                )

        await asyncio.to_thread(_write_and_verify_image, save_path, bytes(buf), root)

    async def download_image(
        self,
        url: str,
        save_path: Path,
        *,
        headers: Mapping[str, str] | None = None,
        root: Path | None = None,
    ) -> bool:
        """Download, PIL-verify, and atomically write a single image."""
        if await asyncio.to_thread(_check_existing_image, save_path):
            return True

        merged_headers = dict(self._default_headers)
        if headers:
            merged_headers.update(headers)

        def _log_retry(retry_state: RetryCallState) -> None:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            logger.debug(
                "图片下载重试 ({}/{}) {}: {}",
                retry_state.attempt_number,
                self._max_attempts,
                save_path.name,
                exc,
            )

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                retry=retry_if_exception_type(_TransientImageError),
                wait=wait_exponential(multiplier=0.5, min=0.25, max=4.0),
                before_sleep=_log_retry,
                sleep=asyncio.sleep,
                reraise=True,
            ):
                with attempt:
                    await self._download_once(url, save_path, merged_headers, root)
            return True
        except (_PermanentImageError, _TransientImageError) as exc:
            logger.debug(f"图片下载失败 {save_path.name}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"图片下载异常 {save_path.name}: {exc}")
            return False


__all__ = ["ImageDownloader"]
