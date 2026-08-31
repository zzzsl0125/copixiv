"""Unit tests for the ``ingest`` pipeline (features/novels/ingest.py).

Covers the refactored single-novel ingestion path (the ``novel_fetch``
task delegates here) with fake client/storage/downloader — no network:

- success path: fetch → save text → persist (author/series placeholders,
  upsert) → author-name two-phase writeback
- already-known skip path (upsert returns 0 → ``outcome.new_count == 0``)
- fetch-failure path: ``webview_novel`` returns None → ``outcome.failed``
  and a failed_novel trace
- asset-failure path: await_all failures recorded in the same transaction
- persist_novels invariants: FK placeholders + refreshed summaries
"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from copixiv.features.novels.ingest import ingest
from copixiv.features.novels.persist import persist_novels
from copixiv.core.draft import build_novel
from copixiv.db.models import (
    Author, FailedNovel, Novel, Series,
)
from copixiv.db.uow import SqlUnitOfWork

# session_factory comes from tests/conftest.py (shared in-memory engine).


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Truncate all tables before each test (PG session-scoped DB)."""
    yield


class FakeClient:
    """webview_novel returns a canned response; user_detail returns a name."""

    def __init__(self, webview_result=None, user_name="测试作者"):
        self._webview = webview_result
        self._user_name = user_name
        self.user_detail_calls: list[int] = []

    async def webview_novel(self, novel_id):
        return self._webview

    async def user_detail(self, user_id):
        self.user_detail_calls.append(user_id)
        return {"user": {"name": self._user_name}}


class FakeStorage:
    def __init__(self, tmp_path):
        self.download_dir = str(tmp_path / "download")
        self.saved: list[tuple[int, str, bool]] = []

    def save_novel_text(self, novel_id, title, content, force=False):
        self.saved.append((novel_id, title, force))


class FakeImageDownloader:
    def __init__(self, failures=None):
        self._failures = failures or []
        self.processed: list[int] = []

    async def process_novel_assets(self, data, force=False):
        # data is now a write-path NovelDraft (K3 contract)
        self.processed.append(data.id)

    async def await_all(self):
        return list(self._failures)


def _webview(novel_id: int, title: str = "新小说", text: str = "正文内容"):
    return SimpleNamespace(
        id=novel_id, title=title, user_id=1,
        rating=SimpleNamespace(bookmark=5, view=10),
        text=text, caption="中文标题", series_id=None,
        series_title=None, series_navigation=None,
        cdate="2026-01-01T00:00:00", tags=["中文"],
        images=None, illusts=None, cover_url=None,
    )


def _ingest_kwargs(session_factory, tmp_path, client, downloader):
    return dict(
        session_factory=session_factory,
        client=client,
        file_storage=FakeStorage(tmp_path),
        image_downloader=downloader,
    )


class TestIngest:
    async def test_download_new_novel_persists(
        self, session_factory, tmp_path,
    ):
        client = FakeClient(_webview(100))
        storage = FakeStorage(tmp_path)
        downloader = FakeImageDownloader()

        out = await ingest(
            ids=[100],
            session_factory=session_factory,
            client=client,
            file_storage=storage,
            image_downloader=downloader,
        )

        assert out.titles == ["新小说"]
        assert out.new_count == 1
        assert out.new_author_ids == {1}
        assert out.failed == []
        assert storage.saved == [(100, "新小说", False)]
        assert downloader.processed == [100]

        with session_factory() as s:
            novel = s.get(Novel, 100)
            assert novel is not None
            assert novel.title == "新小说"
            # author-name resolution ran: placeholder got its name back
            assert novel.author_name == "测试作者"
            assert s.get(Author, 1).author_name == "测试作者"
        assert client.user_detail_calls == [1]

    async def test_download_new_novel_backfills_known_author_name(
        self, session_factory, tmp_path,
    ):
        # Author already known locally; webview download still inserts the
        # novel with author_name=None and must be backfilled from author row.
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="测试作者"))
            s.commit()

        client = FakeClient(_webview(100))
        out = await ingest(
            ids=[100],
            **_ingest_kwargs(session_factory, tmp_path, client, FakeImageDownloader()),
        )

        assert out.new_count == 1
        with session_factory() as s:
            novel = s.get(Novel, 100)
            assert novel is not None
            assert novel.author_name == "测试作者"
        # Locally-known name should not require another Pixiv API call.
        assert client.user_detail_calls == []

    async def test_download_skips_existing(
        self, session_factory, tmp_path,
    ):
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="测试作者"))
            s.flush()
            s.add(Novel(id=100, title="新小说", author_id=1, path="/tmp/100.txt"))
            s.commit()

        client = FakeClient(_webview(100))
        out = await ingest(
            ids=[100],
            **_ingest_kwargs(session_factory, tmp_path, client, FakeImageDownloader()),
        )

        assert out.new_count == 0
        # Author name already known locally → no API call for it
        assert client.user_detail_calls == []

    async def test_fetch_failure_leaves_trace(
        self, session_factory, tmp_path,
    ):
        client = FakeClient(None)  # webview_novel returns None (deleted novel)
        out = await ingest(
            ids=[999],
            **_ingest_kwargs(session_factory, tmp_path, client, FakeImageDownloader()),
        )

        # The pipeline still reports the failure to callers/notifier.
        assert (999, "webview_novel 返回空") in out.failed
        assert out.titles == []
        assert out.new_count == 0
        # The novel was NEVER persisted — but the ledger has no FK by
        # design (ingest downloads BEFORE persisting), so the failure MUST
        # still be recorded (product contract: download failures are never
        # silently dropped).
        with session_factory() as s:
            row = s.get(FailedNovel, 999)
            assert row is not None
            assert row.failed_times == 1
            s.delete(row)
            s.commit()

    async def test_asset_failures_recorded_in_persist_transaction(
        self, session_factory, tmp_path,
    ):
        client = FakeClient(_webview(100))
        downloader = FakeImageDownloader(failures=[(100, "图片下载失败")])
        out = await ingest(
            ids=[100],
            **_ingest_kwargs(session_factory, tmp_path, client, downloader),
        )

        # Novel still persisted, failure recorded alongside
        assert out.new_count == 1
        assert (100, "图片下载失败") in out.failed
        with session_factory() as s:
            assert s.get(Novel, 100) is not None
            row = s.get(FailedNovel, 100)
            assert row is not None
            assert row.error_message == "图片下载失败"

    async def test_redownload_forces_save(self, session_factory, tmp_path):
        client = FakeClient(_webview(100))
        storage = FakeStorage(tmp_path)
        out = await ingest(
            ids=[100],
            force=True,
            session_factory=session_factory,
            client=client,
            file_storage=storage,
            image_downloader=FakeImageDownloader(),
        )
        assert storage.saved == [(100, "新小说", True)]

    async def test_success_forgets_failure_record(self, session_factory, tmp_path):
        """成功下载必须清除失败台账——否则手动重试成功后记录永远残留。"""
        with session_factory() as s:
            s.add(Author(author_id=1, author_name="测试作者"))
            s.flush()
            s.add(Novel(id=100, title="新小说", author_id=1, path="/tmp/100.txt"))
            s.flush()
            s.add(FailedNovel(
                novel_id=100, failure_type="download",
                error_message="旧失败", failed_times=3,
                title="新小说", last_failed_at=datetime(2026, 8, 19, 19, 0,
                                                         tzinfo=ZoneInfo("Asia/Shanghai")),
            ))
            s.commit()

        client = FakeClient(_webview(100))
        out = await ingest(
            ids=[100],
            **_ingest_kwargs(session_factory, tmp_path, client, FakeImageDownloader()),
        )

        # The novel was already persisted (seed), so this is a metadata-only
        # re-upsert (new_count 0) — the failure record must still be cleared.
        assert out.new_count == 0
        with session_factory() as s:
            assert s.get(FailedNovel, 100) is None
            assert s.get(Novel, 100) is not None


class TestPersistNovels:
    async def test_creates_fk_placeholders_and_refreshes_summaries(
        self, session_factory,
    ):
        uow = SqlUnitOfWork(session_factory)
        novel = build_novel(
            id=300, title="T", author_id=3, series_id=5,
            like=10, view=20, text=100,
            tags=["测试标签"],
        )

        async with uow.begin():
            count = await persist_novels(uow, [novel])

        assert count == 1
        with session_factory() as s:
            assert s.get(Novel, 300) is not None
            author = s.get(Author, 3)
            assert author is not None
            # update_summary ran: aggregates written into the placeholder
            assert author.novel_count == 1
            assert author.like == 10
            series = s.get(Series, 5)
            assert series is not None
            assert series.novel_count == 1

    async def test_existing_novel_returns_zero(self, session_factory):
        with session_factory() as s:
            s.add(Author(author_id=3, author_name="a"))
            s.flush()
            s.add(Novel(id=300, title="T", author_id=3, path="/tmp/300.txt"))
            s.commit()

        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            count = await persist_novels(uow, [
                build_novel(id=300, title="T", author_id=3),
            ])
        assert count == 0

    async def test_empty_input_is_noop(self, session_factory):
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            assert await persist_novels(uow, []) == 0
            assert await persist_novels(uow, [None]) == 0
