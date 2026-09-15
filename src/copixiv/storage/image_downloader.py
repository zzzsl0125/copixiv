"""Image downloader — fetches cover/illustration images in a thread pool.

Images are fetched anonymously (Referer-only, no OAuth token — see
``pixiv/http.py``), so this pool is independent of the
account pool.  A global start-to-start interval throttles the CDN side
(IP-level protection) while the thread pool keeps multiple transfers
in flight.
"""

import asyncio
import atexit
import copy
import os
import re
import threading
import time
import zipfile
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from copixiv.core.draft import NovelDraft
from copixiv.core.services import has_image_placeholders
from copixiv.pixiv.http import create_image_session, pick_image_url
from copixiv.storage.epub.builder import is_valid_epub, resolve_epub_path
from copixiv.log import logger

# Same marker syntax the builder resolves — keep in sync with
# ``storage.epub.builder._HAS_IMAGE_PATTERN``.
_MARKER = re.compile(rb"\[(?:uploadedimage|pixivimage):[\d-]+\]")


def _epub_has_raw_marker(epub_path: Path) -> bool:
    """True when a built EPUB still carries an unresolved image marker.

    Such a file is readable but *not finished*: the reader would see
    ``[pixivimage:123]`` as literal text.  The "already exists ⇒ done" guard
    must not treat it as complete.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if not name.lower().endswith((".xhtml", ".html", ".htm")):
                    continue
                if _MARKER.search(zf.read(name)):
                    return True
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def _is_decodable_image(path: Path) -> bool:
    """True when PIL can actually open *path* as an image.

    Guards the silent failure mode of 2026-09-16: a response that passed every
    HTTP-level check but whose bytes are not an image (CDN error document,
    truncated body).  Such a file used to be kept as a successful asset and
    later rendered as a "缺图框", hiding the real problem.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


class ImageDownloader:
    """Downloads novel cover and illustration images, then triggers EPUB creation.

    Runs downloads in a dedicated thread pool to avoid blocking the event loop.
    """

    def __init__(
        self,
        max_workers: int = 4,
        min_interval: float = 0.25,
        epub_builder: Any | None = None,
        proxy_http: str = "",
        proxy_https: str = "",
    ):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._epub_builder = epub_builder
        self._proxy_http = proxy_http
        self._proxy_https = proxy_https
        self._futures: list[tuple[int, Future]] = []
        self._in_flight: set[int] = set()
        self._in_flight_lock = threading.Lock()
        # Terminal asset outcome per novel — written from the worker threads
        # (and from the submitting coroutine on the early-return branches),
        # read by the event loop via ``drain_outcomes()``.
        self._outcomes: dict[int, str] = {}
        self._outcomes_lock = threading.Lock()
        # IP-level throttle: minimum start-to-start gap between downloads.
        self._min_interval = min_interval
        self._throttle_lock = threading.Lock()
        self._last_start: float = 0.0
        atexit.register(self.shutdown)

    def __del__(self) -> None:
        """Best-effort cleanup — atexit is the primary safety net."""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass

    def _throttle(self) -> None:
        """Space out download *starts* across the whole pool.

        Runs in the worker thread; holding the lock while sleeping is
        intentional — threads queue at the gate and are admitted at most
        ``min_interval`` apart, while the actual transfers still overlap.
        """
        if self._min_interval <= 0:
            return
        with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_start)
            if wait > 0:
                time.sleep(wait)
            self._last_start = time.monotonic()

    def download_image(
        self, url: str, save_path: Path, session: requests.Session | None = None
    ) -> bool:
        """Download a single image to *save_path*. Returns True on success."""
        if save_path.exists() and save_path.stat().st_size > 0:
            if _is_decodable_image(save_path):
                return True
            # Present but unusable (bad bytes from an earlier attempt): drop it
            # and re-fetch instead of short-circuiting forever — otherwise one
            # bad download poisons this asset on every future run.
            logger.warning(f"已存在的图片无法解码，重新下载: {save_path.name}")
            try:
                save_path.unlink()
            except OSError:
                pass

        local_session = session or create_image_session(
            self._proxy_http, self._proxy_https,
        )
        should_close = session is None
        tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")

        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            self._throttle()
            response = local_session.get(url, stream=True, timeout=10)
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
            # A 200 with a matching Content-Length is NOT proof of an image:
            # the CDN can answer with an error document, and a truncated body
            # still matches the header when the server lies.  Both produced
            # files that ``Image.open`` rejected later (PIL
            # UnidentifiedImageError) — surfacing as "缺图框" in the EPUB while
            # the download was recorded as a success.  Verify pixels *here*,
            # where the bytes are still on the temp path and a retry is cheap.
            if not _is_decodable_image(tmp_path):
                head = b""
                try:
                    with open(tmp_path, "rb") as fh:
                        head = fh.read(64)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Not a decodable image ({downloaded_size} bytes, "
                    f"head={head[:32]!r})"
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

    async def process_novel_assets(
        self, novel: NovelDraft, force: bool = False,
    ) -> None:
        """Download all assets for a novel and attempt EPUB creation.

        Takes the write-path :class:`~copixiv.core.draft.NovelDraft`;
        runs in the thread pool and returns immediately (fire-and-forget).
        """
        path_str = novel.path
        if not path_str:
            self._record_outcome(novel.id, "skipped")
            return

        novel_id = novel.id

        # Body-driven decision, evaluated once and used twice below.  The
        # guard used to short-circuit on "a valid zip exists" alone, which
        # disagreed with the reconciler (same row: PENDING + file + no
        # placeholder → NO_IMAGES there, ``done`` here) and blessed files
        # whose XHTML still carried raw markers.
        needs_epub: bool | None = None
        if novel.content is not None:
            needs_epub = has_image_placeholders(novel.content)

        epub_path = resolve_epub_path(Path(path_str))
        if epub_path.exists() and not force and is_valid_epub(epub_path):
            if needs_epub is False:
                # Nothing to embed and a readable file is already there:
                # genuinely finished.  Reporting ``done`` here is what keeps a
                # re-download of an image-less novel from downgrading an
                # existing DONE to NO_IMAGES merely because the fresh body
                # carries no marker.
                self._record_outcome(novel_id, "done")
                return
            if not _epub_has_raw_marker(epub_path):
                # Marker-bearing text, clean file: done.
                self._record_outcome(novel_id, "done")
                return
            # File exists but still holds raw markers — fall through and
            # rebuild it instead of reporting a finished novel that isn't.

        if not novel.images and not novel.illusts and needs_epub is not True:
            # No downloadable asset in this API payload and the body needs no
            # EPUB.  When the body *does* carry placeholders the worker is
            # given the chance to rebuild from the text (or to render the
            # missing-image boxes) — without that, 3 181 placeholder novels
            # could never be rebuilt (2026-09 排查).
            self._record_outcome(novel_id, "skipped")
            return

        with self._in_flight_lock:
            if novel_id in self._in_flight:
                # Still in flight → deliberately no outcome: the next gate
                # (``await_all`` + ``drain_outcomes``) reports it.
                return
            self._in_flight.add(novel_id)

        try:
            future = self._executor.submit(
                self._download_assets, copy.copy(novel),
            )
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

    def _record_outcome(self, novel_id: int, outcome: str) -> None:
        """Register one novel's terminal asset outcome.

        Called on every return path of :meth:`process_novel_assets` /
        :meth:`_download_assets` — from the worker thread for submitted
        tasks, from the submitting coroutine for the early returns.  The
        lock covers the cross-thread hand-off to ``drain_outcomes``.
        """
        with self._outcomes_lock:
            self._outcomes[novel_id] = outcome

    def drain_outcomes(self, ids: Iterable[int] | None = None) -> dict[int, str]:
        """Return {novel_id: outcome} accumulated since the last drain.

        outcome ∈ {"done", "skipped", "failed"}：
          done    EPUB 已写出（或本来就已存在且完整 → 视为已完成）
          skipped 无需 EPUB（正文没有图片占位符），或没有 path 可写
          failed  下载/生成失败，已计入 await_all() 的失败列表
        在途（尚未完成）的 novel 不出现在返回值里。

        *ids* scopes the drain to one round's novels.  The downloader is a
        single application-wide instance (``app.py``) shared by concurrent
        ``ingest()`` calls (``failed_retry`` fans out with ``asyncio.gather``),
        and ``await_all()`` only waits for *its own* futures — so an
        unscoped drain would let round B swallow round A's ``done`` results
        and then write a status update for rows A has not persisted yet
        (a no-op UPDATE → the row stays PENDING, exactly the "finished but
        not marked" bug this is meant to kill).  Outcomes for ids outside
        *ids* stay in the ledger for whoever owns them.
        """
        with self._outcomes_lock:
            if ids is None:
                outcomes, self._outcomes = self._outcomes, {}
                return outcomes
            wanted = set(ids)
            outcomes = {
                nid: out for nid, out in self._outcomes.items() if nid in wanted
            }
            for nid in outcomes:
                self._outcomes.pop(nid, None)
            return outcomes

    async def await_all(self) -> list[tuple[int, str]]:
        """Wait for all in-flight asset tasks (image download + EPUB) to finish.

        Downloads stay fire-and-forget, but callers that need "files are
        ready before I persist" — the ingest pipeline before its persist
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

    def _download_assets(self, novel: NovelDraft) -> str | None:
        """Synchronous asset download + EPUB creation (runs in thread pool).

        Returns ``None`` on success, or a failure reason string so the
        caller (``await_all``) can persist it into ``failed_novel``.
        """
        from copixiv.log import logger

        base_path = Path(novel.path).parent
        novel_id = str(novel.id)
        images = novel.images or {}
        illusts = novel.illusts or {}
        cover_url = novel.cover_url

        # The body decides whether an EPUB is warranted at all: a novel with
        # no ``[uploadedimage:…]`` placeholder has nothing to embed.  The
        # webview draft carries the text; a metadata-only draft (content is
        # None) leaves the decision to the builder.
        needs_epub: bool | None = None
        if novel.content is not None:
            needs_epub = has_image_placeholders(novel.content)

        downloaded_files: list[Path] = []
        session = create_image_session(self._proxy_http, self._proxy_https)

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
                    url = pick_image_url(img_info.get("urls"))
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
                        url = pick_image_url(
                            illust_data.get("images"),
                            order=("original", "medium", "small"),
                        )
                        if url:
                            ext = Path(url).suffix or ".jpg"
                            path = base_path / f"{novel_id}_p_{illust_id}{ext}"
                            if self.download_image(url, path, session):
                                downloaded_files.append(path)

            # EPUB —— 只有成功才清理临时图片。
            # ``needs_epub=False``（正文没有图片占位符）时跳过，避免为一本
            # 无图小说留下一个空壳 EPUB；正文占位符判定与 has_epub 同一套
            # 规则（core.services.has_image_placeholders）。
            if self._epub_builder is not None:
                if not self._epub_builder.create_epub(
                    novel, needs_epub=needs_epub,
                ):
                    self._record_outcome(novel.id, "failed")
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
                # ``needs_epub=False``: the builder returned True without
                # writing anything (the body has no image placeholder), so
                # there is no new EPUB to call DONE — that is a ``skipped``.
                self._record_outcome(
                    novel.id, "skipped" if needs_epub is False else "done",
                )
            else:
                # No builder wired → no EPUB can be produced.  Never claim
                # DONE for a file that does not exist (the app always wires
                # one — ``app.py``; this guards test/degraded assembly).
                self._record_outcome(novel.id, "skipped")
            return None
        except Exception:
            self._record_outcome(novel.id, "failed")
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
