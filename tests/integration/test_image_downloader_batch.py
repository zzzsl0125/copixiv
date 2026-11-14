"""Layer 3 integration tests: AssetBatch isolation across concurrent ImageDownloader batches."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from copixiv.core.draft import build_novel
from copixiv.storage.image_downloader import ImageDownloader


class _FakeEpubBuilder:
    def create_epub(self, novel, needs_epub=None) -> bool:
        return True


@pytest.mark.asyncio
async def test_asset_batches_isolate_overlapping_and_disjoint_novels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    downloader = ImageDownloader(
        max_workers=2, min_interval=0.0, epub_builder=_FakeEpubBuilder(),
    )

    def _fake_download_all(novel, base_dir, session):
        # Yield thread briefly so both batches overlap on novel_id=10
        time.sleep(0.05)
        return []

    monkeypatch.setattr(downloader, "_download_all_images", _fake_download_all)
    monkeypatch.setattr("copixiv.storage.image_downloader.state.is_complete", lambda _p: True)

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
        batch_a = downloader.new_batch()
        batch_b = downloader.new_batch()

        # Both batches submit overlapping novel 10, while batch_a also handles novel 20
        await asyncio.gather(
            downloader.process_novel_assets(d10, force=True, batch=batch_a),
            downloader.process_novel_assets(d20, force=True, batch=batch_a),
            downloader.process_novel_assets(d10, force=True, batch=batch_b),
        )

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
        downloader.shutdown()
