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
import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from copixiv.core import formats
from copixiv.core.draft import NovelDraft
from copixiv.log import logger
from copixiv.pixiv.http import create_image_session, pick_image_url
from copixiv.storage import assets
from copixiv.storage.atomic import atomic_write
from copixiv.storage.epub import state


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


def _existing_epub_finished(epub_path: Path, needs_epub: bool | None) -> bool:
    """Return True if *epub_path* already satisfies *needs_epub* (blocking zip I/O)."""
    if not epub_path.exists() or not state.is_readable_container(epub_path):
        return False
    if needs_epub is False:
        return True
    if needs_epub is None:
        return not state.has_raw_markers(epub_path)
    return bool(needs_epub is True and state.is_complete(epub_path))


class AssetBatch:
    """Isolated per-call bucket for async image/EPUB tasks and their outcomes.

    Sharing the application-wide :class:`ImageDownloader` (its thread pool and
    IP-level rate-limit gate) via an ``AssetBatch`` guarantees that concurrent
    ``ingest()`` branches never steal or clobber each other's futures or
    outcomes — even when two branches overlap on the same ``novel_id``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: list[tuple[int, Future[tuple[str | None, str | None]]]] = []
        self._outcomes: dict[int, str] = {}

    def record_outcome(self, novel_id: int, outcome: str) -> None:
        """Store a terminal asset outcome for *novel_id* in this batch."""
        with self._lock:
            self._outcomes[novel_id] = outcome

    def add_future(
        self,
        novel_id: int,
        future: Future[tuple[str | None, str | None]],
    ) -> None:
        """Attach an in-flight download future to this batch."""
        with self._lock:
            self._futures.append((novel_id, future))

    async def await_all(self, ids: Iterable[int] | None = None) -> list[tuple[int, str]]:
        """Wait for futures queued on this batch and populate outcomes."""
        with self._lock:
            if ids is None:
                futures, self._futures = self._futures, []
            else:
                wanted = set(ids)
                futures = [(n, f) for n, f in self._futures if n in wanted]
                self._futures = [(n, f) for n, f in self._futures if n not in wanted]

        failures: list[tuple[int, str]] = []
        for novel_id, future in futures:
            try:
                outcome, reason = await asyncio.wrap_future(future)
                if outcome is not None:
                    self.record_outcome(novel_id, outcome)
                if reason:
                    failures.append((novel_id, str(reason)))
            except Exception as exc:  # defensive: unexpected future error
                self.record_outcome(novel_id, "failed")
                failures.append((novel_id, str(exc)))
        return failures

    def drain_outcomes(self, ids: Iterable[int] | None = None) -> dict[int, str]:
        """Return and clear ``{novel_id: outcome}`` accumulated on this batch."""
        with self._lock:
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
        self._default_batch = AssetBatch()
        self._in_flight: dict[int, Future[tuple[str | None, str | None]]] = {}
        self._in_flight_lock = threading.Lock()
        # IP-level throttle: minimum start-to-start gap between downloads.
        self._min_interval = min_interval
        self._throttle_lock = threading.Lock()
        self._last_start: float = 0.0
        atexit.register(self.shutdown)

    def new_batch(self) -> AssetBatch:
        """Create an isolated :class:`AssetBatch` sharing this pool and throttle."""
        return AssetBatch()

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

        try:
            self._throttle()
            response = local_session.get(url, stream=True, timeout=10)
            response.raise_for_status()

            expected_size = int(response.headers.get("content-length", 0))
            downloaded_size = 0

            # 原子写：流式落临时文件，校验通过才由 atomic_write 替换到
            # save_path。异常退出时临时文件由它清掉（下面直接返回 False）。
            with atomic_write(save_path) as tmp_path:
                with open(tmp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)

                if expected_size and downloaded_size != expected_size:
                    raise RuntimeError(
                        f"Incomplete download: expected {expected_size}, "
                        f"got {downloaded_size}"
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
            return True
        except Exception:
            return False
        finally:
            if should_close:
                local_session.close()

    async def process_novel_assets(
        self,
        novel: NovelDraft,
        force: bool = False,
        batch: AssetBatch | None = None,
    ) -> None:
        """Download all assets for a novel and attempt EPUB creation.

        Takes the write-path :class:`~copixiv.core.draft.NovelDraft`;
        runs in the thread pool and returns immediately (fire-and-forget).
        When *batch* is passed, futures and outcomes stay isolated to that batch.
        """
        target = batch if batch is not None else self._default_batch
        path_str = novel.path
        if not path_str:
            target.record_outcome(novel.id, "skipped")
            return

        novel_id = novel.id

        # Body-driven decision, evaluated once and used twice below.  The
        # guard used to short-circuit on "a valid zip exists" alone, which
        # disagreed with the reconciler (same row: PENDING + file + no
        # placeholder → NO_IMAGES there, ``done`` here) and blessed files
        # whose XHTML still carried raw markers.
        #
        # Tri-state, and all three branches matter:
        #   True  — the body has placeholders: an existing file only counts if
        #           it is *complete* (markers resolved AND an illustration
        #           embedded), otherwise rebuild.
        #   False — the body needs no EPUB: a pre-existing file is final, even
        #           an image-less one.  Re-judging it would downgrade a
        #           legitimately DONE row.
        #   None  — a metadata-only draft (no body): nothing can be judged, so
        #           any readable file short-circuits.  Rebuilding here would be
        #           pure waste and the builder would read the text anyway.
        needs_epub = state.needs_epub(novel.content)

        epub_path = formats.sibling_path(Path(path_str), formats.EPUB)
        if not force and await asyncio.to_thread(
            _existing_epub_finished, epub_path, needs_epub
        ):
            target.record_outcome(novel_id, "done")
            return

        if not novel.images and not novel.illusts and needs_epub is not True:
            # No downloadable asset in this API payload and the body needs no
            # EPUB.  When the body *does* carry placeholders the worker is
            # given the chance to rebuild from the text (or to render the
            # missing-image boxes) — without that, 3 181 placeholder novels
            # could never be rebuilt (2026-09 排查).
            target.record_outcome(novel_id, "skipped")
            return

        with self._in_flight_lock:
            existing_future = self._in_flight.get(novel_id)
            if existing_future is not None:
                # Attach the shared in-flight future to this batch so its caller
                # still waits for completion and receives its outcome.
                target.add_future(novel_id, existing_future)
                return
            future = self._executor.submit(
                self._download_assets, copy.copy(novel),
            )
            self._in_flight[novel_id] = future

        target.add_future(novel_id, future)

        def _on_done(_fut: Any, nid: int = novel_id) -> None:
            self._release(nid, _fut)

        future.add_done_callback(_on_done)

    def _release(self, novel_id: int, future: Any = None) -> None:
        """Remove *novel_id* from the in-flight map (called from the worker thread)."""
        with self._in_flight_lock:
            current = self._in_flight.get(novel_id)
            if future is None or current is future:
                self._in_flight.pop(novel_id, None)

    def drain_outcomes(self, ids: Iterable[int] | None = None) -> dict[int, str]:
        """Drain outcomes accumulated on the default (unbatched) collector."""
        return self._default_batch.drain_outcomes(ids)

    async def await_all(self, ids: Iterable[int] | None = None) -> list[tuple[int, str]]:
        """Wait for unbatched futures on the default collector."""
        return await self._default_batch.await_all(ids)

    def _download_assets(self, novel: NovelDraft) -> tuple[str | None, str | None]:
        """Synchronous asset download + EPUB creation (runs in thread pool).

        Returns ``(outcome, failure_reason)`` so waiting batches can record
        each item's terminal state independently.
        """
        base_dir = Path(novel.path or "").parent
        novel_id = str(novel.id)
        needs_epub = state.needs_epub(novel.content)
        session = create_image_session(self._proxy_http, self._proxy_https)

        try:
            downloaded = self._download_all_images(novel, base_dir, session)
            return self._build_epub(novel, downloaded, needs_epub)
        except Exception:
            logger.exception(f"Error processing assets for novel {novel_id}")
            # EPUB 失败(create_epub 返回 False 或抛异常)时统一保留已下载的
            # 图片:download_image 下载失败时会自删坏文件,所以留下的都是
            # 完整文件;下次重试时 download_image 看到文件已存在会直接跳过,
            # 复用它们重建 EPUB,避免重复下载。
            return "failed", f"资产处理异常: novel {novel_id}"
        finally:
            session.close()

    def _download_all_images(
        self, novel: NovelDraft, base_dir: Path, session: requests.Session,
    ) -> list[Path]:
        """下载封面 / 内嵌图 / 关联插图 → 落盘的路径列表。

        三组的 URL 来源与目录名不同，但动作完全一样（挑一个 URL、落到
        ``{id}_{kind}_{asset}``、成功就记账），所以循环只有一份。
        """
        novel_id = novel.id
        downloaded: list[Path] = []

        jobs: list[tuple[str, str | int, str]] = []
        if novel.cover_url:
            jobs.append((assets.COVER, "", novel.cover_url))
        for img_id, img_info in (novel.images or {}).items():
            url = pick_image_url(img_info.get("urls"))
            if url:
                jobs.append((assets.UPLOADED, img_id, url))
        for illust_id, wrapper in (novel.illusts or {}).items():
            illust_data = (
                wrapper.get("illust") if isinstance(wrapper, dict) else wrapper
            )
            if isinstance(illust_data, dict):
                url = pick_image_url(
                    illust_data.get("images"),
                    order=("original", "medium", "small"),
                )
                if url:
                    jobs.append((assets.ILLUSTRATION, illust_id, url))

        for kind, asset_id, url in jobs:
            path = assets.asset_path(
                base_dir, novel_id, kind, asset_id, assets.asset_suffix(url),
            )
            if self.download_image(url, path, session):
                downloaded.append(path)
            elif kind == assets.COVER:
                logger.debug(f"下载: #{novel_id} 封面失败 → {url}")

        return downloaded

    def _build_epub(
        self, novel: NovelDraft, downloaded: list[Path], needs_epub: bool | None,
    ) -> tuple[str | None, str | None]:
        """让 builder 产出 EPUB，并返回 ``(outcome, failure_reason)``。

        结局的三分法是刻意的 —— 只有第一种值得重试、值得记台账：

        * **来源给了图、一张都没落地** → ``("failed", reason)``（重试会复用已落地的那些）；
        * **正文不需要 EPUB** → ``("skipped", None)``（builder 也没写文件）；
        * **来源压根没给图** → ``(None, None)``，行留在 PENDING，让 ``check_epub``
          用它的放弃规则判定（作者删掉的图是终态，不是错误）。
        """
        novel_id = novel.id
        if self._epub_builder is None:
            # 没有接 builder → 产不出 EPUB。绝不为一个不存在的文件宣称 DONE
            # （应用装配总会接上，见 app.py；这里兜的是测试 / 降级装配）。
            return "skipped", None

        if not self._epub_builder.create_epub(novel, needs_epub=needs_epub):
            return "failed", f"EPUB 生成失败: novel {novel_id}"

        if needs_epub is False:
            # builder 没写任何东西（正文没有图片占位符）→ 没有新 EPUB 可宣称
            # DONE，这是 ``skipped``。
            return "skipped", None

        # builder 的 True **不是**成品证明：某张图下载失败会退化成可见的
        # 缺图框，于是文件存在、没有 raw 标记、却一张图都没嵌进去。把它宣称为
        # DONE 正是"生产方与对账方对同一个文件给出相反结论"的由来。
        epub_path = formats.sibling_path(Path(novel.path or ""), formats.EPUB)
        has_source_images = bool(novel.images or novel.illusts)
        if state.is_complete(epub_path):
            outcome = "done"
        elif has_source_images:
            logger.warning(f"下载: #{novel_id} EPUB 未完成（图片下载失败）")
            return "failed", f"EPUB 不完整: novel {novel_id}"
        else:
            logger.warning(
                f"下载: #{novel_id} 正文有图片占位符但来源未给图片，"
                f"交给检查任务判定"
            )
            return None, None

        logger.info(
            f"下载: #{novel_id} EPUB 完成 "
            f"(已清理 {len(downloaded)} 张临时图片)",
        )
        for path in downloaded:
            try:
                os.remove(path)
            except OSError:
                pass
        return outcome, None

    def shutdown(self) -> None:
        """Wait for all in-flight downloads to finish."""
        self._executor.shutdown(wait=True)
