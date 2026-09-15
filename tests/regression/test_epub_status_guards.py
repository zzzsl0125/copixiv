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

from copixiv.core.models import Novel
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks import maintenance


def _novel(tmp_path: Path, novel_id: int, body: str) -> tuple[Path, Novel]:
    d = tmp_path / str(novel_id)
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"novel{novel_id}.txt"
    txt.write_text(body, encoding="utf-8")
    return txt, Novel(
        id=novel_id, title="t", author_id=0, path=str(txt),
        images={"1": {"urls": {"original": "http://x/1.png"}}},
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
        from copixiv.core.draft import NovelDraft

        assert EpubBuilder().create_epub(NovelDraft(
            id=32, title="t", author_id=0, path=str(txt), images={"1": {}},
        )) is True
        novel.content = "正文 [uploadedimage:1]"

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


class TestLongBasename:
    """251-253 字节的正文名：EPUB 仍须产出、仍须可被找到。

    生产实例（2026-09-16 修复脚本）：3 本小说的 basename 已贴到 NAME_MAX，
    ``.epub.tmp`` 写入直接 Errno 36，一本 EPUB 都没产出。修法是就地按字节
    截断 basename（绝不改用 ``download_dir`` 重新推导——那是相对路径，会把
    文件丢到别处），并让读取端能用同前缀/id 前缀把截断后的文件找回来。
    """

    def _long_case(self, tmp_path, novel_id: int):
        # 250 字节左右的中文名 + 8 位 id，复刻生产里的 251-253 字节
        title = "测试标题" * 20
        from copixiv.core.services import build_path

        txt = Path(build_path(novel_id, title, str(tmp_path)))
        txt.parent.mkdir(parents=True, exist_ok=True)
        return txt

    def test_epub_is_written_and_resolvable_when_name_is_at_the_limit(
        self, tmp_path,
    ):
        from copixiv.core.draft import NovelDraft
        from copixiv.storage.epub.builder import resolve_epub_path

        novel_id = 27549104
        txt = self._long_case(tmp_path, novel_id)
        # 故意把名字撑到 250-253 字节：去掉预算截断，模拟脚本用原始标题拼路径
        long_name = ("长" * 80) + f"_{novel_id}.txt"
        txt = txt.with_name(long_name)
        txt.write_text("正文 [uploadedimage:1]\n", encoding="utf-8")
        assert 240 <= len(txt.name.encode()) <= 253

        from PIL import Image

        Image.new("RGB", (2, 2), (255, 0, 0)).save(
            txt.parent / f"{novel_id}_u_1.jpg"
        )
        assert EpubBuilder().create_epub(NovelDraft(
            id=novel_id, title="t", author_id=1, path=str(txt),
            images={"1": {}},
        )) is True

        produced = resolve_epub_path(txt)
        assert produced.is_file()
        assert len(produced.name.encode()) <= 250
        assert zipfile.is_zipfile(produced)
        with zipfile.ZipFile(produced) as z:
            names = z.namelist()
            html = b"".join(
                z.read(n) for n in names if n.endswith(".xhtml")
            )
        assert any("images/" in n and not n.endswith("/") for n in names)
        assert b"[uploadedimage:" not in html
        assert not list(txt.parent.glob("*.tmp"))

    def test_normal_name_keeps_the_same_stem(self, tmp_path):
        from copixiv.core.draft import NovelDraft
        from copixiv.storage.epub.builder import resolve_epub_path

        txt = tmp_path / "普通标题_18822939.txt"
        txt.write_text("正文 [uploadedimage:1]\n", encoding="utf-8")
        from PIL import Image

        Image.new("RGB", (2, 2), (255, 0, 0)).save(
            tmp_path / "18822939_u_1.jpg"
        )
        assert EpubBuilder().create_epub(NovelDraft(
            id=18822939, title="t", author_id=1, path=str(txt),
            images={"1": {}},
        )) is True

        produced = resolve_epub_path(txt)
        assert produced == txt.with_suffix(".epub")     # 同名，可直接推导
        assert produced.is_file()


class TestDownloadedImageIsVerified:
    """200 + 正确 Content-Length ≠ 图片（2026-09-16 复现的静默失败）。

    日志里反复出现 ``cannot identify image file …_u_25395125.png``：下载被判成功、
    文件留在磁盘、稍后被渲染成"缺图框"。校验必须发生在临时文件阶段——那时
    重下最便宜，也还没被记成成功。
    """

    def test_non_image_body_is_rejected_at_download_time(
        self, tmp_path, monkeypatch,
    ):
        from copixiv.storage.image_downloader import ImageDownloader

        html = b"<html><body>404 not found</body></html>"
        dl = ImageDownloader(max_workers=1)
        dl._min_interval = 0

        class _Resp:
            headers = {"content-length": str(len(html))}
            def raise_for_status(self): pass
            def iter_content(self, chunk_size=8192): yield html

        class _Session:
            def get(self, url, stream=True, timeout=10): return _Resp()
            def close(self): pass

        save = tmp_path / "9_u_1.png"
        try:
            assert dl.download_image("http://x/1.png", save, _Session()) is False
            assert not save.exists()                      # 坏字节绝不落盘
            assert not save.with_suffix(".png.tmp").exists()
        finally:
            dl.shutdown()

    def test_valid_image_is_accepted(self, tmp_path):
        from PIL import Image

        from copixiv.storage.image_downloader import ImageDownloader

        png = tmp_path / "good.png"
        Image.new("RGB", (4, 4), (1, 2, 3)).save(png)
        data = png.read_bytes()
        dl = ImageDownloader(max_workers=1)
        dl._min_interval = 0

        class _Resp:
            headers = {"content-length": str(len(data))}
            def raise_for_status(self): pass
            def iter_content(self, chunk_size=8192): yield data

        class _Session:
            def get(self, url, stream=True, timeout=10): return _Resp()
            def close(self): pass

        out = tmp_path / "9_u_2.png"
        try:
            assert dl.download_image("http://x/2.png", out, _Session()) is True
            assert out.read_bytes() == data
        finally:
            dl.shutdown()

    def test_cached_file_with_bad_bytes_is_refetched(self, tmp_path, monkeypatch):
        from PIL import Image

        from copixiv.storage.image_downloader import ImageDownloader

        bad = tmp_path / "9_u_3.png"
        bad.write_bytes(b"not an image at all")
        png = tmp_path / "src.png"
        Image.new("RGB", (4, 4), (9, 9, 9)).save(png)
        data = png.read_bytes()
        dl = ImageDownloader(max_workers=1)
        dl._min_interval = 0
        calls: list[str] = []

        class _Resp:
            headers = {"content-length": str(len(data))}
            def raise_for_status(self): pass
            def iter_content(self, chunk_size=8192): yield data

        class _Session:
            def get(self, url, stream=True, timeout=10):
                calls.append(url)
                return _Resp()
            def close(self): pass

        try:
            assert dl.download_image("http://x/3.png", bad, _Session()) is True
            assert calls == ["http://x/3.png"]            # 没有被"已存在"短路
            assert bad.read_bytes() == data
        finally:
            dl.shutdown()
