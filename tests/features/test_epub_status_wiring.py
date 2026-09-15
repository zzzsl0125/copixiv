"""回归：EPUB 产出方自己把 ``has_epub`` 推到 DONE（契约修复1）。

背景（2026-09 事故）：``process_novel_assets`` 是 fire-and-forget，EPUB
写成功后**没有任何人**把 ``has_epub`` 从 ``1`` 推到 ``2``；唯一的置位者是
每周对账任务 ``check_epub``（曾被禁用 37 天），造成 820 本"已做好却显示
待处理"。

本文件覆盖契约的三条验收：

1. EPUB 真的写出 → 同一持久化事务结束后 DB 里 ``has_epub == 2``；
2. 正文无占位符（``skipped``）→ 状态**不被**置成 ``2``；
3. EPUB 生成失败（``create_epub`` 返回 False）→ 保持 ``1``，且进
   ``failed_novel`` 台账。

用真实的 ``ImageDownloader`` + ``FileStorage`` + ``EpubBuilder``/假 builder
跑真实的 ``ingest`` 流程（PG fixture 来自 tests/conftest.py），只在
webview 响应处打桩——这样"写出成功"和"标成 DONE"之间没有测试替身可以
掩盖接线缺失。
"""

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from copixiv.core.models import EpubStatus
from copixiv.db.models import Author, FailedNovel, Novel
from copixiv.features.novels.ingest import ingest
from copixiv.storage.epub.builder import EpubBuilder, is_valid_epub
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader

TEXT_WITH_IMAGE = "正文开始 [uploadedimage:1] 正文结束"
TEXT_WITHOUT_IMAGE = "纯文字正文，没有任何图片占位符"


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


class FakeClient:
    """``webview_novel`` returns a canned response; ``user_detail`` a name."""

    def __init__(self, webview_result, user_name="测试作者"):
        self._webview = webview_result
        self._user_name = user_name

    async def webview_novel(self, novel_id):
        return self._webview

    async def user_detail(self, user_id):
        return {"user": {"name": self._user_name}}


def _webview(novel_id: int, text: str, title: str = "新小说"):
    return SimpleNamespace(
        id=novel_id, title=title, user_id=1,
        rating=SimpleNamespace(bookmark=5, view=10),
        text=text, caption="中文标题", series_id=None,
        series_title=None, series_navigation=None,
        cdate="2026-01-01T00:00:00", tags=["中文"],
        images=None, illusts=None, cover_url=None,
    )


class _FailingEpubBuilder:
    """``create_epub`` 返回 False —— 模拟 EPUB 写入失败。"""

    def __init__(self):
        self.calls: list[int] = []

    def create_epub(self, novel, needs_epub=None):
        self.calls.append(novel.id)
        return False


def _prebuilt_epub(path: Path) -> None:
    """A readable (zip) EPUB at *path* — satisfies ``is_valid_epub``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")


async def _run_ingest(novel_id, text, storage, downloader, session_factory):
    try:
        return await ingest(
            ids=[novel_id],
            session_factory=session_factory,
            client=FakeClient(_webview(novel_id, text)),
            file_storage=storage,
            image_downloader=downloader,
        )
    finally:
        downloader.shutdown()


class TestProducerMarksDone:
    async def test_written_epub_marks_has_epub_done(
        self, session_factory, tmp_path,
    ):
        """EPUB 生成成功 → 同一事务结束后 has_epub == 2（DONE）。"""
        storage = FileStorage(str(tmp_path / "download"))
        downloader = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=EpubBuilder(),
        )

        out = await _run_ingest(
            100, TEXT_WITH_IMAGE, storage, downloader, session_factory,
        )

        assert out.new_count == 1
        assert out.failed == []
        # 磁盘上确实有 EPUB —— 不是"标成 2 却没有文件"（事故的另一半：
        # 216 本残缺 EPUB 被记成完成）。
        assert is_valid_epub(storage.novel_epub_path(100, "新小说"))

        with session_factory() as s:
            novel = s.get(Novel, 100)
            assert novel is not None
            assert novel.has_epub == EpubStatus.DONE

    async def test_existing_valid_epub_also_marks_done(
        self, session_factory, tmp_path,
    ):
        """EPUB 本来就已存在且有效（早退分支，记 done）→ 同样置 2。"""
        storage = FileStorage(str(tmp_path / "download"))
        epub_path = storage.novel_epub_path(200, "新小说")
        _prebuilt_epub(epub_path)

        downloader = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=EpubBuilder(),
        )
        out = await _run_ingest(
            200, TEXT_WITH_IMAGE, storage, downloader, session_factory,
        )

        assert out.failed == []
        # 早退：没有重新生成，原有字节原封不动。
        assert zipfile.is_zipfile(epub_path)
        assert downloader._futures == []
        with session_factory() as s:
            assert s.get(Novel, 200).has_epub == EpubStatus.DONE

    async def test_text_without_placeholder_is_not_marked_done(
        self, session_factory, tmp_path,
    ):
        """正文无占位符（skipped）→ 状态不被置成 2，也不写空壳 EPUB。"""
        storage = FileStorage(str(tmp_path / "download"))
        downloader = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=EpubBuilder(),
        )

        out = await _run_ingest(
            101, TEXT_WITHOUT_IMAGE, storage, downloader, session_factory,
        )

        assert out.new_count == 1
        assert out.failed == []
        with session_factory() as s:
            novel = s.get(Novel, 101)
            assert novel is not None
            # 修复2 定义的终态：正文无占位符 → NO_IMAGES。直接钉住具体值，
            # 否则「被悄悄置回 PENDING/NO」这类漂移抓不到（2026-09-16 评审）。
            assert novel.has_epub == EpubStatus.NO_IMAGES
        assert not storage.novel_epub_path(101, "新小说").exists()

    async def test_failed_epub_keeps_pending_and_records_ledger(
        self, session_factory, tmp_path,
    ):
        """EPUB 生成失败 → has_epub 保持 1，失败进 failed_novel 台账。"""
        storage = FileStorage(str(tmp_path / "download"))
        builder = _FailingEpubBuilder()
        downloader = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=builder,
        )

        out = await _run_ingest(
            102, TEXT_WITH_IMAGE, storage, downloader, session_factory,
        )

        assert builder.calls == [102]
        assert (102, "EPUB 生成失败: novel 102") in out.failed
        with session_factory() as s:
            novel = s.get(Novel, 102)
            assert novel is not None
            assert novel.has_epub == EpubStatus.PENDING
            row = s.get(FailedNovel, 102)
            assert row is not None
            assert row.failure_type == "download"
            assert row.error_message == "EPUB 生成失败: novel 102"
            assert row.title == "新小说"

    async def test_failed_epub_on_existing_pending_row_stays_pending(
        self, session_factory, tmp_path,
    ):
        """已入库且 has_epub=1 的小说重下仍失败 → 保持 1，台账更新。"""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="测试作者"))
            s.flush()
            s.add(Novel(
                id=103, title="新小说", author_id=1, path="/tmp/103.txt",
                has_epub=EpubStatus.PENDING,
            ))
            s.commit()

        storage = FileStorage(str(tmp_path / "download"))
        downloader = ImageDownloader(
            max_workers=1, min_interval=0,
            epub_builder=_FailingEpubBuilder(),
        )
        out = await _run_ingest(
            103, TEXT_WITH_IMAGE, storage, downloader, session_factory,
        )

        assert (103, "EPUB 生成失败: novel 103") in out.failed
        with session_factory() as s:
            assert s.get(Novel, 103).has_epub == EpubStatus.PENDING
            row = s.get(FailedNovel, 103)
            assert row is not None
            assert row.error_message == "EPUB 生成失败: novel 103"
