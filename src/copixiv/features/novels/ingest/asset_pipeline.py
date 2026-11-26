"""Novel asset pipeline — coordinates cover/illustration downloads and EPUB builds."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from copixiv.core import naming as formats
from copixiv.core.draft import NovelDraft
from copixiv.core.models import SOURCE_PIXIV
from copixiv.core.sources import EpubPolicy
from copixiv.log import logger
from copixiv.pixiv.http import pick_image_url
from copixiv.storage import assets
from copixiv.storage.epub import state
from copixiv.storage.epub.builder import EpubBuildResult
from copixiv.storage.image_downloader import ImageDownloader


def _existing_epub_finished(epub_path: Path, needs_epub: bool | None) -> bool:
    """Return True if *epub_path* already satisfies *needs_epub* (blocking zip I/O)."""
    readable, complete = state.inspect_epub(epub_path)
    if not readable:
        return False
    if needs_epub is True:
        return complete
    return True


def _remove_downloaded_files(paths: Iterable[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            os.remove(path)


def _extract_uploaded_image_url(img_info: Any) -> str | None:
    """Extract an image URL from Pixiv ``{"urls": ...}``, ``{"url": ...}``, or plain strings."""
    if isinstance(img_info, str):
        cleaned = img_info.strip()
        return cleaned or None
    if isinstance(img_info, Mapping):
        url = pick_image_url(img_info.get("urls"))
        if url:
            return url
        raw_url = img_info.get("url")
        if isinstance(raw_url, str):
            cleaned = raw_url.strip()
            return cleaned or None
    return None


class AssetBatch:
    """Isolated per-call bucket for async image/EPUB tasks and their outcomes."""

    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[tuple[str | None, str | None]]]] = []
        self._outcomes: dict[int, str] = {}

    def record_outcome(self, novel_id: int, outcome: str) -> None:
        """Store a terminal asset outcome for *novel_id* in this batch."""
        self._outcomes[novel_id] = outcome

    def add_task(
        self,
        novel_id: int,
        task: asyncio.Task[tuple[str | None, str | None]],
    ) -> None:
        """Attach an in-flight download task to this batch."""
        self._tasks.append((novel_id, task))

    async def await_all(self, ids: Iterable[int] | None = None) -> list[tuple[int, str]]:
        """Wait for tasks queued on this batch and populate outcomes."""
        if ids is None:
            tasks, self._tasks = self._tasks, []
        else:
            wanted = set(ids)
            tasks = [(n, t) for n, t in self._tasks if n in wanted]
            self._tasks = [(n, t) for n, t in self._tasks if n not in wanted]

        failures: list[tuple[int, str]] = []
        for novel_id, task in tasks:
            try:
                outcome, reason = await task
                if outcome is not None:
                    self.record_outcome(novel_id, outcome)
                if reason:
                    failures.append((novel_id, str(reason)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_outcome(novel_id, "failed")
                failures.append((novel_id, str(exc)))
        return failures

    def drain_outcomes(self, ids: Iterable[int] | None = None) -> dict[int, str]:
        """Return and clear ``{novel_id: outcome}`` accumulated on this batch."""
        if ids is None:
            outcomes, self._outcomes = self._outcomes, {}
            return outcomes
        wanted = set(ids)
        outcomes = {nid: out for nid, out in self._outcomes.items() if nid in wanted}
        for nid in outcomes:
            self._outcomes.pop(nid, None)
        return outcomes


class NovelAssetPipeline:
    """Coordinates novel image downloads and EPUB generation across sources."""

    def __init__(
        self,
        image_downloader: ImageDownloader | None = None,
        epub_builder: Any | None = None,
        *,
        image_headers: Mapping[str, str] | None = None,
        download_root: str | Path | None = None,
        http_manager: Any | None = None,
        source_registry: Any | None = None,
    ) -> None:
        self._downloader = image_downloader
        self._epub_builder = epub_builder
        self._image_headers = dict(image_headers) if image_headers is not None else None
        self._download_root = Path(download_root) if download_root is not None else None
        self._http_manager = http_manager
        self._source_registry = source_registry
        self._in_flight: dict[int, asyncio.Task[tuple[str | None, str | None]]] = {}

    def new_batch(self) -> AssetBatch:
        """Create an isolated :class:`AssetBatch` sharing this pipeline."""
        return AssetBatch()

    async def process_novel_assets(
        self,
        novel: NovelDraft,
        force: bool = False,
        batch: AssetBatch | None = None,
    ) -> None:
        """Queue async image downloads + EPUB creation for *novel*."""
        if batch is None:
            raise ValueError("AssetBatch is required")

        path_str = novel.path
        if not path_str:
            batch.record_outcome(novel.id, "skipped")
            return

        novel_id = novel.id
        needs_epub = state.needs_epub(novel.content)
        epub_path = formats.sibling_path(Path(path_str), formats.EPUB)

        if not force and await asyncio.to_thread(_existing_epub_finished, epub_path, needs_epub):
            batch.record_outcome(novel_id, "done")
            return

        if not novel.images and not novel.illusts and needs_epub is not True:
            batch.record_outcome(novel_id, "skipped")
            return

        existing_task = self._in_flight.get(novel_id)
        if existing_task is not None and not existing_task.done():
            batch.add_task(novel_id, existing_task)
            return

        task = asyncio.create_task(self._process_one(copy.copy(novel), needs_epub))
        self._in_flight[novel_id] = task
        batch.add_task(novel_id, task)

        def _on_done(
            finished_task: asyncio.Task[tuple[str | None, str | None]],
            nid: int = novel_id,
        ) -> None:
            self._release(nid, finished_task)

        task.add_done_callback(_on_done)

    def _release(
        self,
        novel_id: int,
        task: asyncio.Task[tuple[str | None, str | None]] | None = None,
    ) -> None:
        current = self._in_flight.get(novel_id)
        if task is None or current is task:
            self._in_flight.pop(novel_id, None)

    async def aclose(self) -> None:
        """Cancel pending asset tasks."""
        pending = [t for t in self._in_flight.values() if not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._in_flight.clear()

    async def _process_one(
        self,
        novel: NovelDraft,
        needs_epub: bool | None,
    ) -> tuple[str | None, str | None]:
        base_dir = Path(novel.path or "").parent
        novel_id = str(novel.id)
        downloaded: list[Path] = []
        try:
            downloaded = await self._download_all_images(novel, base_dir)
            return await self._build_epub(novel, downloaded, needs_epub)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Error processing assets for novel {novel_id}")
            return "failed", f"资产处理异常: novel {novel_id}"
        finally:
            if downloaded:
                await asyncio.to_thread(_remove_downloaded_files, downloaded)

    def _select_downloader(self, source: str) -> ImageDownloader | None:
        if self._http_manager is not None:
            try:
                source_downloader = self._http_manager.get_image_downloader(source)
            except Exception:
                source_downloader = None
            if source_downloader is not None:
                return source_downloader
        return self._downloader

    async def _download_all_images(
        self,
        novel: NovelDraft,
        base_dir: Path,
    ) -> list[Path]:
        novel_id = novel.id
        jobs: list[tuple[str, str | int, str]] = []
        if novel.cover_url:
            jobs.append((assets.COVER, "", novel.cover_url))
        for img_id, img_info in (novel.images or {}).items():
            url = _extract_uploaded_image_url(img_info)
            if url:
                jobs.append((assets.UPLOADED, img_id, url))
        for illust_id, wrapper in (novel.illusts or {}).items():
            illust_data = wrapper.get("illust") if isinstance(wrapper, dict) else wrapper
            if isinstance(illust_data, dict):
                url = pick_image_url(
                    illust_data.get("images"),
                    order=("original", "medium", "small"),
                )
                if url:
                    jobs.append((assets.ILLUSTRATION, illust_id, url))

        if not jobs:
            return []

        downloader = self._select_downloader(novel.source)
        if downloader is None:
            return []
        headers = self._image_headers if novel.source == SOURCE_PIXIV else None

        paths = [
            assets.asset_path(
                base_dir,
                novel_id,
                kind,
                asset_id,
                assets.asset_suffix(url),
            )
            for kind, asset_id, url in jobs
        ]
        results = await asyncio.gather(
            *[
                downloader.download_image(
                    url,
                    path,
                    headers=headers,
                    root=self._download_root,
                )
                for (_kind, _aid, url), path in zip(jobs, paths, strict=True)
            ]
        )

        downloaded: list[Path] = []
        for (kind, _aid, url), path, ok in zip(jobs, paths, results, strict=True):
            if ok:
                downloaded.append(path)
            elif kind == assets.COVER:
                logger.debug(f"下载: #{novel_id} 封面失败 → {url}")
        return downloaded

    async def _build_epub(
        self,
        novel: NovelDraft,
        downloaded: list[Path],
        needs_epub: bool | None,
    ) -> tuple[str | None, str | None]:
        novel_id = novel.id
        if self._epub_builder is None:
            return "skipped", None

        build_kwargs: dict[str, Any] = {"needs_epub": needs_epub}
        if downloaded:
            build_kwargs["asset_paths"] = downloaded
        result: EpubBuildResult = await asyncio.to_thread(
            self._epub_builder.create_epub,
            novel,
            **build_kwargs,
        )

        policy = (
            self._source_registry.epub_policy(novel.source)
            if self._source_registry is not None
            else (EpubPolicy.CONTAINER_ONLY if int(novel.id) < 0 else EpubPolicy.STRICT_PLACEHOLDERS)
        )

        if policy == EpubPolicy.BEST_EFFORT_EXTERNAL:
            if needs_epub is False:
                return "skipped", None
            if result.is_complete:
                logger.info(
                    f"下载: #{novel_id} EPUB 完成 (已清理 {len(downloaded)} 张临时图片)",
                )
                return "done", None
            if result.written and result.output_path is not None:
                await asyncio.to_thread(_remove_downloaded_files, [result.output_path])
            if result.embedded_images == 0 and (not result.ok or not result.is_complete):
                logger.warning(
                    f"下载: #{novel_id} 外部来源图片未能嵌入（缺失 {result.missing_images} 张），降级为无图",
                )
            else:
                logger.warning(f"下载: #{novel_id} 外部来源 EPUB 不完整，降级为无图")
            return "no_images", None

        has_source_images = bool(novel.images or novel.illusts)

        if not result.ok:
            if result.written and result.output_path is not None:
                await asyncio.to_thread(_remove_downloaded_files, [result.output_path])
            return "failed", f"EPUB 生成失败: novel {novel_id}"
        if needs_epub is False or not result.written:
            return "skipped", None
        if result.is_complete:
            logger.info(
                f"下载: #{novel_id} EPUB 完成 (已清理 {len(downloaded)} 张临时图片)",
            )
            return "done", None
        if has_source_images:
            logger.warning(f"下载: #{novel_id} EPUB 未完成（图片下载失败）")
            if result.output_path is not None:
                await asyncio.to_thread(_remove_downloaded_files, [result.output_path])
            return "failed", f"EPUB 不完整: novel {novel_id}"

        logger.warning(f"下载: #{novel_id} 正文有图片占位符但来源未给图片，交给检查任务判定")
        return None, None


__all__ = ["AssetBatch", "NovelAssetPipeline"]
