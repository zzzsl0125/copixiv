"""回归：EPUB 状态守卫（2026-09-16 评审补钉的测试缺口）。

评审指出三处「测试抓不到回退」/「无覆盖」的缺口，这里逐个钉死：

1. **坏 zip 回归其实没被钉住** —— ``test_corrupt_epub_is_not_treated_as_done``
   走的是 ``_download_assets``，而守卫只看 ``epub_path.exists()``；把守卫退回
   ``exists()`` 它依然全绿。真正的守卫在 ``process_novel_assets`` 的早退分支，
   这里直接打它的脸。
2. **drain 的作用域** —— 共享 ImageDownloader 上，一轮只能取走自己那些 id 的
   outcome，否则并发 ingest 会互相偷结果、DONE 永久丢失。
3. **正文分块扫描的边界** —— ``_scan_placeholder`` 以 1 MiB 分块 + tail 重叠，
   占位符跨块时必须仍能命中（纯静态推理，无测试）。
"""

import asyncio
import zipfile
from pathlib import Path

from copixiv.core.draft import NovelDraft
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks import maintenance


def _novel(tmp_path: Path, novel_id: int, body: str) -> tuple[Path, NovelDraft]:
    d = tmp_path / str(novel_id)
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"novel{novel_id}.txt"
    txt.write_text(body, encoding="utf-8")
    return txt, NovelDraft(
        id=novel_id, title="t", author_id=0, path=str(txt),
        images={"1": {"urls": {"original": "http://x/1.png"}}},
        content=body,
    )


class TestCorruptEpubRegenerates:
    """The guard must reject a file that merely *exists*."""

    def test_corrupt_epub_is_rebuilt_not_short_circuited(
        self, tmp_path, monkeypatch,
    ):
        txt, novel = _novel(tmp_path, 31, "正文 [uploadedimage:1]")
        epub = txt.with_suffix(".epub")
        epub.write_bytes(b"not a zip at all")          # 坏文件

        dl = ImageDownloader(max_workers=1, epub_builder=EpubBuilder())

        def fake_download(url, save_path, session=None):
            from PIL import Image

            Image.new("RGB", (2, 2), (0, 0, 255)).save(save_path)
            return True

        monkeypatch.setattr(dl, "download_image", fake_download)
        try:
            asyncio.run(dl.process_novel_assets(novel))
            assert asyncio.run(dl.await_all()) == []
            assert zipfile.is_zipfile(epub)            # 真的重建了
        finally:
            dl.shutdown()

    def test_clean_existing_epub_still_short_circuits(
        self, tmp_path, monkeypatch,
    ):
        """干净且完整的既有 EPUB 仍然直接跳过（不重复下载、判 done）。"""
        txt, novel = _novel(tmp_path, 32, "正文 [uploadedimage:1]")
        from PIL import Image

        Image.new("RGB", (2, 2), (255, 0, 0)).save(
            txt.parent / "32_u_1.jpg"
        )
        assert EpubBuilder().create_epub(NovelDraft(
            id=32, title="t", author_id=0, path=str(txt), images={"1": {}},
        )) is True

        dl = ImageDownloader(max_workers=1, epub_builder=EpubBuilder())
        called: list[str] = []
        monkeypatch.setattr(
            dl, "download_image",
            lambda *a, **k: called.append("download") or True,
        )
        try:
            asyncio.run(dl.process_novel_assets(novel))
            assert dl.drain_outcomes() == {32: "done"}
            assert called == []                        # 没有任何下载
        finally:
            dl.shutdown()


class TestDrainOutcomesScope:
    """One round must not steal another round's outcomes."""

    def test_drain_only_returns_and_removes_the_given_ids(self):
        dl = ImageDownloader(max_workers=1)
        try:
            dl._record_outcome(1, "done")      # 属于另一轮
            dl._record_outcome(2, "done")
            dl._record_outcome(3, "failed")

            got = dl.drain_outcomes([2])

            assert got == {2: "done"}
            # 1 与 3 留给它们各自的一轮，没有被吞掉
            assert dl.drain_outcomes() == {1: "done", 3: "failed"}
            assert dl.drain_outcomes() == {}
        finally:
            dl.shutdown()

    def test_unscoped_drain_still_clears_everything(self):
        dl = ImageDownloader(max_workers=1)
        try:
            dl._record_outcome(7, "skipped")
            assert dl.drain_outcomes() == {7: "skipped"}
            assert dl.drain_outcomes() == {}
        finally:
            dl.shutdown()


class TestPlaceholderScanChunkBoundary:
    """The 1 MiB chunking must not lose a marker that straddles a boundary."""

    def test_marker_across_chunk_boundary_is_found(self, tmp_path):
        marker = "[uploadedimage:12345]"
        for offset in (0, 1 << 20, (1 << 20) * 2):
            body = "あ" * offset + marker
            f = tmp_path / f"n{offset}.txt"
            f.write_text(body, encoding="utf-8")
            assert maintenance._scan_placeholder(f) is True, offset

    def test_plain_text_and_missing_file(self, tmp_path):
        plain = tmp_path / "plain.txt"
        plain.write_text("没有任何标记的正文" * 1000, encoding="utf-8")
        assert maintenance._scan_placeholder(plain) is False
        # 缺文件 → 「无法判定」，绝不能当成「无图」
        assert maintenance._scan_placeholder(tmp_path / "nope.txt") is None
