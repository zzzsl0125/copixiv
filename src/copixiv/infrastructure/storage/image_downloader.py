"""Image downloader — fetches cover/illustration images in a thread pool."""

import atexit
import os
from concurrent.futures import ThreadPoolExecutor
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
        if save_path.exists():
            return True

        local_session = session or _create_session()
        should_close = session is None

        try:
            proxies = {
                "http": config.proxy.http,
                "https": config.proxy.https,
            }
            response = local_session.get(url, stream=True, timeout=10, proxies=proxies)
            response.raise_for_status()

            expected_size = int(response.headers.get("content-length", 0))
            downloaded_size = 0

            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)

            if expected_size and downloaded_size != expected_size:
                raise RuntimeError(
                    f"Incomplete download: expected {expected_size}, got {downloaded_size}"
                )
            return True
        except Exception:
            if save_path.exists():
                try:
                    os.remove(save_path)
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

        self._executor.submit(self._download_assets, data.copy())

    def _download_assets(self, data: dict) -> None:
        """Synchronous asset download + EPUB creation (runs in thread pool)."""
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

            # EPUB
            if self._epub_builder and self._epub_builder.create_epub(data):
                logger.info(
                    f"下载: #{novel_id} EPUB 完成 "
                    f"(已清理 {len(downloaded_files)} 张临时图片)",
                )
                for f in downloaded_files:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        except Exception:
            logger.exception(f"Error processing assets for novel {novel_id}")
            # EPUB creation failed — don't leave half-downloaded temp files behind.
            for f in downloaded_files:
                try:
                    os.remove(f)
                except OSError:
                    pass
        finally:
            session.close()

    def shutdown(self) -> None:
        """Wait for all in-flight downloads to finish."""
        self._executor.shutdown(wait=True)
