"""Layer 1 unit tests: pure async ImageDownloader (respx/tenacity) and AssetBatch isolation."""

from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from copixiv.core.draft import build_novel
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.storage.epub.builder import EpubBuildResult
from copixiv.storage.image_downloader import ImageDownloader


def _make_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeEpubBuilder:
    def create_epub(self, novel, needs_epub=None, asset_paths=None) -> EpubBuildResult:
        del asset_paths
        return EpubBuildResult(
            ok=True,
            written=needs_epub is not False,
            output_path=Path(novel.path).with_suffix(".epub"),
            embedded_images=1 if needs_epub is not False else 0,
            missing_images=0,
            has_raw_markers=False,
        )


@pytest.mark.asyncio
async def test_image_downloader_async_respx_retries_corrupt_and_permanent_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    async def _no_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr("copixiv.storage.image_downloader.asyncio.sleep", _no_sleep)
    png_bytes = _make_png_bytes()

    async with httpx.AsyncClient() as http_client:
        downloader = ImageDownloader(
            http_client=http_client,
            max_concurrency=2,
            min_interval=0.0,
            max_attempts=3,
        )

        # 503 then corrupt HTML body then valid PNG -> retries and succeeds
        target = tmp_path / "0000" / "10_u_1.png"
        with respx.mock:
            route = respx.get("https://i.pximg.net/ok.png").mock(
                side_effect=[
                    httpx.Response(503, text="busy"),
                    httpx.Response(200, content=b"<html>not an image</html>"),
                    httpx.Response(200, content=png_bytes),
                ]
            )
            assert (
                await downloader.download_image("https://i.pximg.net/ok.png", target, root=tmp_path) is True
            )
            assert route.call_count == 3
            assert target.read_bytes() == png_bytes

        # Re-downloading an existing valid image makes 0 HTTP requests
        with respx.mock:
            route_cached = respx.get("https://i.pximg.net/ok.png").mock(return_value=httpx.Response(500))
            assert (
                await downloader.download_image("https://i.pximg.net/ok.png", target, root=tmp_path) is True
            )
            assert route_cached.call_count == 0

        # Permanent 404 fails immediately without retry
        with respx.mock:
            route_404 = respx.get("https://i.pximg.net/gone.png").mock(
                return_value=httpx.Response(404, text="not found")
            )
            gone = tmp_path / "0000" / "10_u_2.png"
            assert (
                await downloader.download_image("https://i.pximg.net/gone.png", gone, root=tmp_path) is False
            )
            assert route_404.call_count == 1
            assert not gone.exists()

        # Pre-existing corrupt file on disk is unlinked and replaced cleanly
        with respx.mock:
            corrupt = tmp_path / "0000" / "10_u_3.png"
            corrupt.parent.mkdir(parents=True, exist_ok=True)
            corrupt.write_bytes(b"corrupt-bytes")
            respx.get("https://i.pximg.net/heal.png").mock(
                return_value=httpx.Response(200, content=png_bytes)
            )
            assert (
                await downloader.download_image("https://i.pximg.net/heal.png", corrupt, root=tmp_path)
                is True
            )
            assert corrupt.read_bytes() == png_bytes

        # Content-Encoding with differing wire Content-Length succeeds without false incomplete
        import gzip

        gz_bytes = gzip.compress(png_bytes)
        encoded_target = tmp_path / "0000" / "10_u_4.png"
        with respx.mock:
            respx.get("https://i.pximg.net/gz.png").mock(
                return_value=httpx.Response(
                    200,
                    content=gz_bytes,
                    headers={
                        "Content-Encoding": "gzip",
                        "Content-Length": str(len(gz_bytes)),
                    },
                )
            )
            assert (
                await downloader.download_image(
                    "https://i.pximg.net/gz.png",
                    encoded_target,
                    root=tmp_path,
                )
                is True
            )
            assert encoded_target.read_bytes() == png_bytes


@pytest.mark.asyncio
async def test_asset_batches_isolate_overlapping_and_disjoint_novels_without_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    async with httpx.AsyncClient() as http_client:
        raw_downloader = ImageDownloader(
            http_client=http_client,
            max_concurrency=2,
            min_interval=0.0,
        )
        pipeline = NovelAssetPipeline(
            raw_downloader,
            epub_builder=_FakeEpubBuilder(),
            download_root=tmp_path,
        )
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()

        async def _fake_download_all(novel, base_dir):
            del novel, base_dir
            worker_started.set()
            await asyncio.wait_for(release_worker.wait(), timeout=5.0)
            return []

        monkeypatch.setattr(pipeline, "_download_all_images", _fake_download_all)

        def _forbid_rescan(_path):
            raise AssertionError("NovelAssetPipeline should use EpubBuildResult without re-scanning")

        monkeypatch.setattr(
            "copixiv.features.novels.ingest.asset_pipeline.state.inspect_epub", _forbid_rescan
        )

        txt10 = tmp_path / "n10.txt"
        txt10.write_text("正文 [uploadedimage:1]", encoding="utf-8")
        txt20 = tmp_path / "n20.txt"
        txt20.write_text("无图正文", encoding="utf-8")

        d10 = replace(
            build_novel(id=10, title="Img10", author_id=1, author_name="A", content="正文 [uploadedimage:1]"),
            path=str(txt10),
            images={"1": {"urls": {"original": "http://x/1.jpg"}}},
        )
        d20 = replace(
            build_novel(id=20, title="Plain20", author_id=1, author_name="A", content="无图正文"),
            path=str(txt20),
        )

        try:
            batch_a = pipeline.new_batch()
            batch_b = pipeline.new_batch()

            await pipeline.process_novel_assets(d10, force=True, batch=batch_a)
            await asyncio.wait_for(worker_started.wait(), timeout=3.0)
            await pipeline.process_novel_assets(d20, force=True, batch=batch_a)
            await pipeline.process_novel_assets(d10, force=True, batch=batch_b)
            release_worker.set()

            failures_a, failures_b = await asyncio.gather(
                batch_a.await_all(),
                batch_b.await_all(),
            )
            assert failures_a == []
            assert failures_b == []

            outcomes_a = batch_a.drain_outcomes()
            outcomes_b = batch_b.drain_outcomes()
            assert outcomes_a == {10: "done", 20: "skipped"}
            assert outcomes_b == {10: "done"}
        finally:
            release_worker.set()
            await pipeline.aclose()


@pytest.mark.asyncio
async def test_asset_pipeline_cleans_temp_files_and_incomplete_epub_on_failure(
    tmp_path: Path,
):
    async with httpx.AsyncClient() as http_client:
        raw_downloader = ImageDownloader(
            http_client=http_client,
            max_concurrency=2,
            min_interval=0.0,
        )
        txt = tmp_path / "0000" / "55_Novel.txt"
        txt.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text("正文 [uploadedimage:1]", encoding="utf-8")
        cover_file = tmp_path / "0000" / "55_c_cover.jpg"
        cover_file.write_bytes(b"cover-bytes")
        incomplete_epub = tmp_path / "0000" / "55_Novel.epub"

        class _IncompleteEpubBuilder:
            def create_epub(self, novel, needs_epub=None, asset_paths=None) -> EpubBuildResult:
                del novel, needs_epub, asset_paths
                incomplete_epub.write_bytes(b"incomplete-epub")
                return EpubBuildResult(
                    ok=True,
                    written=True,
                    output_path=incomplete_epub,
                    embedded_images=0,
                    missing_images=1,
                    has_raw_markers=True,
                )

        pipeline = NovelAssetPipeline(
            raw_downloader,
            epub_builder=_IncompleteEpubBuilder(),
            download_root=tmp_path,
        )

        async def _fake_download_all(novel, base_dir):
            del novel, base_dir
            return [cover_file]

        pipeline._download_all_images = _fake_download_all  # type: ignore[method-assign]

        draft = replace(
            build_novel(id=55, title="Novel", author_id=1, author_name="A", content="正文 [uploadedimage:1]"),
            path=str(txt),
            images={"1": {"urls": {"original": "http://x/1.jpg"}}},
        )

        try:
            batch = pipeline.new_batch()
            await pipeline.process_novel_assets(draft, force=True, batch=batch)
            failures = await batch.await_all()
            assert len(failures) == 1
            assert failures[0][0] == 55
            assert not cover_file.exists()
            assert not incomplete_epub.exists()
        finally:
            await pipeline.aclose()


@pytest.mark.asyncio
async def test_fetch_novel_and_assets_has_epub_only_none_when_stored_path_and_not_rewrote(
    tmp_path: Path,
):
    from types import SimpleNamespace

    from copixiv.core.models import EpubStatus
    from copixiv.features.novels.ingest.download import fetch_novel_and_assets
    from copixiv.storage.file_storage import FileStorage

    storage = FileStorage(str(tmp_path))
    async with httpx.AsyncClient() as http_client:
        raw_downloader = ImageDownloader(
            http_client=http_client,
            max_concurrency=1,
            min_interval=0.0,
        )
        pipeline = NovelAssetPipeline(raw_downloader, epub_builder=None, download_root=tmp_path)

        class _FakeClient:
            async def webview_novel(self, novel_id: int):
                return SimpleNamespace(
                    id=novel_id,
                    title="SameTitle",
                    user_id=10,
                    rating={"bookmark": 1, "view": 2},
                    caption="",
                    series_id=None,
                    series_title=None,
                    series_navigation=None,
                    cdate="2026-01-01T00:00:00+09:00",
                    tags=[],
                    text="已有正文内容",
                    images=[],
                    illusts=[],
                    cover_url=None,
                )

        # Pre-create the text file on disk so body_unchanged() returns True (rewrote=False)
        expected_path = storage.save_novel_text(77, "SameTitle", "已有正文内容")

        try:
            batch1 = pipeline.new_batch()
            # New DB row (stored_path=None) even though file was already on disk -> has_epub preserved
            draft_new = await fetch_novel_and_assets(
                77,
                _FakeClient(),  # type: ignore[arg-type]
                storage,
                pipeline,
                stored_path=None,
                asset_batch=batch1,
            )
            await batch1.await_all()
            assert draft_new is not None
            assert draft_new.has_epub == EpubStatus.NO_IMAGES
            assert draft_new.rewrote_body is False

            batch2 = pipeline.new_batch()
            # Existing DB row (stored_path set) and unchanged -> has_epub=None
            draft_existing = await fetch_novel_and_assets(
                77,
                _FakeClient(),  # type: ignore[arg-type]
                storage,
                pipeline,
                stored_path=expected_path,
                asset_batch=batch2,
            )
            await batch2.await_all()
            assert draft_existing is not None
            assert draft_existing.has_epub is None
            assert draft_existing.rewrote_body is False
        finally:
            await pipeline.aclose()
