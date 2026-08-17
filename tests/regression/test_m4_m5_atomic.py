"""M4/M5 回归测试：文件写入原子性 + 资产下载并发去重。

M4 —— 文件写入非原子 + 存在即信任：
- ``save_novel_text`` 崩溃会留下截断文件，且该截断文件被当作完整文件跳过。
- ``download_image`` 直接写目标路径，失败还会 ``os.remove`` 最终目标。
- ``create_epub`` 直接写目标路径。

M5 —— 资产下载并发竞态：
- ``process_novel_assets`` 每次调用都提交任务，不对同一 novel 去重。

期望行为（修复后）：
- 写入走同目录临时文件 + ``os.replace``，异常时只清理临时文件。
- 已存在且大小 > 0 才跳过；大小为 0 视为截断残留，覆盖重写。
- 同一 novel 的资产任务在 in-flight 期间只入队一次。
"""

import asyncio
import builtins
import threading
from pathlib import Path

import pytest

from copixiv.infrastructure.epub.builder import EpubBuilder
from copixiv.infrastructure.storage.file_storage import FileStorage
from copixiv.domain.models.novel import Novel
from copixiv.infrastructure.storage.image_downloader import ImageDownloader


# ---------------------------------------------------------------------------
# M4: save_novel_text
# ---------------------------------------------------------------------------


def test_save_novel_text_crash_leaves_no_partial_file(tmp_path, monkeypatch):
    """崩溃安全：写一半抛异常 → 目标文件不存在，临时文件被清理。"""
    storage = FileStorage(download_dir=str(tmp_path))

    def write_half_then_raise(self, content, encoding=None):
        with open(self, "w", encoding=encoding or "utf-8") as f:
            f.write(content[: len(content) // 2])
        raise OSError("simulated disk full")

    monkeypatch.setattr(Path, "write_text", write_half_then_raise)

    with pytest.raises(OSError):
        storage.save_novel_text(1, "Test", "hello world")

    target = storage.novel_text_path(1, "Test")
    assert not target.exists()
    assert list(target.parent.glob("*.tmp")) == []


def test_save_novel_text_overwrites_zero_byte_residue(tmp_path):
    """零字节残留（截断文件）应被覆盖重写，而不是当作完整文件跳过。"""
    storage = FileStorage(download_dir=str(tmp_path))
    target = storage.novel_text_path(1, "Test")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")

    result = storage.save_novel_text(1, "Test", "hello world")

    assert result == target
    assert target.read_text(encoding="utf-8") == "hello world"
    assert list(target.parent.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# M4: download_image
# ---------------------------------------------------------------------------


def test_download_image_failure_keeps_existing_file(tmp_path, monkeypatch):
    """下载失败只删临时文件，绝不删除最终目标（即使目标是零字节残留）。"""
    save_path = tmp_path / "img.jpg"
    save_path.write_bytes(b"")  # 零字节 → 触发重下

    class BoomSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("network down")

        def close(self):
            pass

    monkeypatch.setattr(
        "copixiv.infrastructure.storage.image_downloader._create_session",
        BoomSession,
    )

    dl = ImageDownloader(max_workers=1)
    try:
        assert dl.download_image("http://x/img.jpg", save_path) is False
        assert save_path.exists()
        assert save_path.stat().st_size == 0
    finally:
        dl.shutdown()


def test_download_image_atomic_on_partial_write(tmp_path, monkeypatch):
    """原子性：写临时文件后抛异常 → 最终路径不存在，且从不直接写最终路径。"""
    save_path = tmp_path / "img.jpg"
    opened_for_write: list[str] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if "w" in mode:
            opened_for_write.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)

    class PartialResponse:
        headers = {"content-length": "100"}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"partial"
            raise RuntimeError("connection reset")

    class FakeSession:
        def get(self, *args, **kwargs):
            return PartialResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        "copixiv.infrastructure.storage.image_downloader._create_session",
        FakeSession,
    )

    dl = ImageDownloader(max_workers=1)
    try:
        assert dl.download_image("http://x/img.jpg", save_path) is False
        assert not save_path.exists()
        assert list(save_path.parent.glob("*.tmp")) == []
        assert str(save_path) not in opened_for_write
    finally:
        dl.shutdown()


# ---------------------------------------------------------------------------
# M5: process_novel_assets 去重
# ---------------------------------------------------------------------------


def test_process_novel_assets_dedups_inflight_id(tmp_path, monkeypatch):
    """同一 novel 在 in-flight 期间重复提交 → 只入队一次。"""
    dl = ImageDownloader(max_workers=2)
    entered = threading.Event()
    release = threading.Event()

    def slow_work(data):
        entered.set()
        release.wait(5)
        return None

    monkeypatch.setattr(dl, "_download_assets", slow_work)

    data = Novel(
        id=1,
        title="novel1",
        author_id=0,
        path=str(tmp_path / "1" / "novel1.txt"),
        images={"1": {}},
        illusts={},
    )

    async def submit_twice():
        await dl.process_novel_assets(data)
        await dl.process_novel_assets(data)

    try:
        asyncio.run(submit_twice())
        assert entered.wait(5)
        assert len(dl._futures) == 1
        assert 1 in dl._in_flight
    finally:
        release.set()
        dl.shutdown()


# ---------------------------------------------------------------------------
# M4: create_epub
# ---------------------------------------------------------------------------


def _epub_data(tmp_path):
    novel_dir = tmp_path / "novel"
    novel_dir.mkdir()
    text_path = novel_dir / "novel1.txt"
    text_path.write_text("Hello world", encoding="utf-8")
    return Novel(
        id=1,
        title="Test Novel",
        author_name="Author",
        author_id=0,
        path=str(text_path),
    ), novel_dir


def test_create_epub_success_is_atomic(tmp_path):
    """成功路径：最终文件存在且无 .tmp 残留。"""
    data, novel_dir = _epub_data(tmp_path)
    builder = EpubBuilder()

    assert builder.create_epub(data) is True

    output = novel_dir / "Test Novel_1.epub"
    assert output.exists()
    assert output.stat().st_size > 0
    assert list(novel_dir.glob("*.tmp")) == []


def test_create_epub_failure_leaves_no_partial(tmp_path, monkeypatch):
    """失败路径：write_epub 写一半抛异常 → 无部分产物。"""
    data, novel_dir = _epub_data(tmp_path)

    from ebooklib import epub as epub_mod

    def boom(name, book, opts=None):
        Path(name).write_bytes(b"partial")
        raise RuntimeError("disk full")

    monkeypatch.setattr(epub_mod, "write_epub", boom)
    builder = EpubBuilder()

    assert builder.create_epub(data) is False

    output = novel_dir / "Test Novel_1.epub"
    assert not output.exists()
    assert list(novel_dir.glob("*.tmp")) == []
