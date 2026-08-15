"""Image downloader — fetches cover/illustration images in a thread pool."""

import asyncio
import atexit
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from copixiv.app.config import config


def _create_session() -> requests.Session:
    """Create a requests session with retries and Pixiv-appropriate headers."""
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({
        "Referer": "https://www.pixiv.net/",
        "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
    })
    return session


class ImageDownloader:
    """Downloads novel cover and illustration images, then triggers EPUB creation.

    Runs downloads in a dedicated thread pool to avoid blocking the event loop.
    """

    def __init__(
        self,
        max_workers: int = 4,
        epub_builder: Any | None = None,
    ):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._epub_builder = epub_builder
        self._futures: list[tuple[int, Future]] = []
        self._in_flight: set[int] = set()
        self._in_flight_lock = threading.Lock()
        atexit.register(self.shutdown)

    def __del__(self) -> None:
        """Best-effort cleanup — atexit is the primary safety net."""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass

    def download_image(
        self, url: str, save_path: Path, session: requests.Session | None = None
    ) -> bool:
        """Download a single image to *save_path*. Returns True on success."""
        if save_path.exists() and save_path.stat().st_size > 0:
            return True

        local_session = session or _create_session()
        should_close = session is None
        tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")

        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            proxies = {
                "http": config.proxy.http,
                "https": config.proxy.https,
            }
            response = local_session.get(url, stream=True, timeout=10, proxies=proxies)
            response.raise_for_status()

            expected_size = int(response.headers.get("content-length", 0))
            downloaded_size = 0

            with open(tmp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)

            if expected_size and downloaded_size != expected_size:
                raise RuntimeError(
                    f"Incomplete download: expected {expected_size}, got {downloaded_size}"
                )
            os.replace(tmp_path, save_path)
            return True
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        finally:
            if should_close:
                local_session.close()

    async def process_novel_assets(self, data: dict, force: bool = False) -> None:
        """Download all assets for a novel and attempt EPUB creation.

        Runs in the thread pool; returns immediately (fire-and-forget).
        """
        # Normalise the input: callers may pass a Pydantic Novel model or a
        # plain dict.  The rest of this method (and _download_assets) uses
        # dict access, so convert the model to a dict first.
        if hasattr(data, "model_dump"):
            data = data.model_dump()

        path_str = data.get("path")
        if not path_str:
            return

        epub_path = Path(path_str).with_suffix(".epub")
        if epub_path.exists() and not force:
            return

        images = data.get("images")
        illusts = data.get("illusts")
        if not images and not illusts:
            return

        novel_id = data["id"]
        with self._in_flight_lock:
            if novel_id in self._in_flight:
                return
            self._in_flight.add(novel_id)

        try:
            future = self._executor.submit(self._download_assets, data.copy())
        except Exception:
            with self._in_flight_lock:
                self._in_flight.discard(novel_id)
            raise

        self._futures.append((novel_id, future))
        future.add_done_callback(lambda _f, nid=novel_id: self._release(nid))

    def _release(self, novel_id: int) -> None:
        """Remove *novel_id* from the in-flight set (called from the worker thread)."""
        with self._in_flight_lock:
            self._in_flight.discard(novel_id)

    async def await_all(self) -> list[tuple[int, str]]:
        """Wait for all in-flight asset tasks (image download + EPUB) to finish.

        Downloads stay fire-and-forget, but callers that need "files are
        ready before I persist" — the batch pipeline before its persist
        phase, single-novel tasks before their upsert — must ``await``
        this.  Uses ``asyncio.wrap_future`` so waiting never blocks the
        event loop.

        Returns ``[(novel_id, reason), ...]`` for every task that failed,
        so callers can persist the failures into ``failed_novel`` inside
        their write transaction (previously these errors were swallowed
        by the worker thread).

        The in-flight list is swapped out first: tasks submitted while we
        are waiting land in a fresh list and are NOT waited on (they
        belong to the next round, which will gate on them).
        """
        futures, self._futures = self._futures, []
        failures: list[tuple[int, str]] = []
        for novel_id, future in futures:
            try:
                reason = await asyncio.wrap_future(future)
                if reason:
                    failures.append((novel_id, str(reason)))
            except Exception as exc:  # defensive: unexpected future error
                failures.append((novel_id, str(exc)))
        return failures

    def _download_assets(self, data: dict) -> str | None:
        """Synchronous asset download + EPUB creation (runs in thread pool).

        Returns ``None`` on success, or a failure reason string so the
        caller (``await_all``) can persist it into ``failed_novel``.
        """
        from copixiv.app.logger import logger

        base_path = Path(data["path"]).parent
        novel_id = str(data["id"])
        images = data.get("images", {})
        illusts = data.get("illusts", {})
        cover_url = data.get("cover_url")

        downloaded_files: list[Path] = []
        session = _create_session()

        try:
            # Cover
            if cover_url:
                logger.debug(f"下载: #{novel_id} 封面 → {cover_url}")
                ext = Path(cover_url).suffix or ".jpg"
                path = base_path / f"{novel_id}_c_cover{ext}"
                if self.download_image(cover_url, path, session):
                    downloaded_files.append(path)
                    logger.debug(f"下载: #{novel_id} 封面 OK")

            # Uploaded images
            if images:
                logger.debug(
                    f"下载: #{novel_id} 内嵌图片 {len(images)} 张",
                )
                for img_id, img_info in images.items():
                    urls = img_info.get("urls", {})
                    url = (
                        urls.get("original")
                        or urls.get("large")
                        or urls.get("medium")
                        or urls.get("small")
                    )
                    if url:
                        ext = Path(url).suffix or ".jpg"
                        path = base_path / f"{novel_id}_u_{img_id}{ext}"
                        if self.download_image(url, path, session):
                            downloaded_files.append(path)

            # Linked illustrations
            if illusts:
                logger.debug(
                    f"下载: #{novel_id} 关联插图 {len(illusts)} 张",
                )
                for illust_id, wrapper in illusts.items():
                    illust_data = (
                        wrapper.get("illust")
                        if isinstance(wrapper, dict)
                        else wrapper
                    )
                    if isinstance(illust_data, dict):
                        imgs = illust_data.get("images", {})
                        url = (
                            imgs.get("original")
                            or imgs.get("medium")
                            or imgs.get("small")
                        )
                        if url:
                            ext = Path(url).suffix or ".jpg"
                            path = base_path / f"{novel_id}_p_{illust_id}{ext}"
                            if self.download_image(url, path, session):
                                downloaded_files.append(path)

            # EPUB —— 只有成功才清理临时图片
            if self._epub_builder is not None:
                if not self._epub_builder.create_epub(data):
                    return f"EPUB 生成失败: novel {novel_id}"
                logger.info(
                    f"下载: #{novel_id} EPUB 完成 "
                    f"(已清理 {len(downloaded_files)} 张临时图片)",
                )
                for f in downloaded_files:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            return None
        except Exception:
            logger.exception(f"Error processing assets for novel {novel_id}")
            # EPUB 失败(create_epub 返回 False 或抛异常)时统一保留已下载的
            # 图片:download_image 下载失败时会自删坏文件,所以留下的都是
            # 完整文件;下次重试时 download_image 看到文件已存在会直接跳过,
            # 复用它们重建 EPUB,避免重复下载。
            return f"资产处理异常: novel {novel_id}"
        finally:
            session.close()

    def shutdown(self) -> None:
        """Wait for all in-flight downloads to finish."""
        self._executor.shutdown(wait=True)
