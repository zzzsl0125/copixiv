"""Task-layer regression tests for the collect-then-persist refactor.

Covers the two regressions the refactor could have introduced:

1. Chinese-language filtering must still happen in ``author_fetch`` /
   ``novel_follow`` (the old per-page handler filtered every page; the
   collect mode filters once after accumulation).
2. ``_batch_handle`` end-to-end: plan → download → persist writes land
   in the database exactly once, with no lock errors.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database import models
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database.write_lock import db_write, DbWriteLock
from copixiv.infrastructure.repositories.fts import FTSManager
from copixiv.tasks import novel_tasks
from copixiv.tasks.pipeline import _batch_handle


def _engine_with_fts(file_engine):
    """Conftest file engine + a rebuilt novel_fts table (pipeline needs it)."""
    with file_engine.connect() as conn:
        FTSManager(conn).rebuild_novel_fts()
    return file_engine


CN_NOVEL = {
    "id": 100, "title": "测试小说", "caption": "这是一篇中文小说。",
    "tags": ["中文"],
}
EN_NOVEL = {
    "id": 200, "title": "Hello World", "caption": "This is an English novel.",
    "tags": [],
}


class FakeClient:
    def __init__(self, novels):
        self._novels = novels

    @asynccontextmanager
    async def account_rule(self, *args, **kwargs):
        yield self

    async def user_novels(self, author_id, fetch_all=False):
        return {"novels": self._novels}

    async def user_detail(self, user_id):
        return {"user": {"name": "测试作者"}}

    async def novel_follow(self, fetch_til=None):
        return {"novels": self._novels}


@pytest.mark.slow
class TestChineseFiltering:
    """Regression: collect mode must still filter to Chinese novels."""

    async def test_author_fetch_filters_chinese(self, monkeypatch, file_engine):
        engine = _engine_with_fts(file_engine)
        session_factory = create_session_factory(engine)
        uow = SqlUnitOfWork(session_factory)
        async with db_write():
            async with uow.begin():
                uow.authors.ensure_exists({1})

        seen: dict = {}

        async def fake_batch_handle(novels, session_factory, **kwargs):
            seen["novels"] = list(novels)
            return [], set()

        monkeypatch.setattr(novel_tasks, "_batch_handle", fake_batch_handle)

        result = await novel_tasks.author_fetch(
            novel_tasks.AuthorFetchArgs(author_id=1, force=True),
            novel_tasks.TaskContext(
                uow=uow, client=FakeClient([CN_NOVEL, EN_NOVEL]),
                file_storage=None, image_downloader=None, config=None,
                write_lock=DbWriteLock(),
            ),
        )

        assert [n["id"] for n in seen["novels"]] == [100]
        assert result.new_novel_titles == []

    async def test_novel_follow_filters_chinese(self, monkeypatch, file_engine):
        engine = _engine_with_fts(file_engine)
        session_factory = create_session_factory(engine)
        uow = SqlUnitOfWork(session_factory)

        seen: dict = {}

        async def fake_batch_handle(novels, session_factory, **kwargs):
            seen["novels"] = list(novels)
            return [], set()

        monkeypatch.setattr(novel_tasks, "_batch_handle", fake_batch_handle)

        result = await novel_tasks.novel_follow(
            novel_tasks.NovelFollowArgs(),
            novel_tasks.TaskContext(
                uow=uow, client=FakeClient([CN_NOVEL, EN_NOVEL]),
                file_storage=None, image_downloader=None, config=None,
                write_lock=DbWriteLock(),
            ),
        )

        assert [n["id"] for n in seen["novels"]] == [100]
        assert result.new_novel_titles == []


@pytest.mark.slow
class TestBatchHandleEndToEnd:
    """plan → download → persist writes land in the DB exactly once."""

    async def test_new_novel_persisted(self, tmp_path, file_engine):
        engine = _engine_with_fts(file_engine)
        session_factory = create_session_factory(engine)

        class FakeClient:
            @asynccontextmanager
            async def account_rule(self, *args, **kwargs):
                yield self

            async def webview_novel(self, novel_id):
                return SimpleNamespace(
                    id=novel_id, title="新小说", user_id=1,
                    rating=SimpleNamespace(bookmark=5, view=10),
                    text="正文内容", caption="中文标题", series_id=None,
                    series_title=None, series_navigation=None,
                    cdate="2026-01-01T00:00:00", tags=["中文"],
                    images=None, illusts=None, cover_url=None,
                )

        class FakeStorage:
            download_dir = str(tmp_path / "download")

            def save_novel_text(self, novel_id, title, content, force=False):
                pass

        class FakeImageDownloader:
            async def process_novel_assets(self, data, force=False):
                pass

            async def await_all(self):
                return []

        novels = [SimpleNamespace(
            id=100, title="新小说", caption="中文",
            user=SimpleNamespace(id=1, name="作者"), series=None,
            total_bookmarks=5, total_view=10, text_length=100,
            create_date="2026-01-01T00:00:00",
            tags=[SimpleNamespace(name="中文")],
        )]

        titles, new_author_ids = await _batch_handle(
            novels, session_factory,
            client=FakeClient(), file_storage=FakeStorage(),
            image_downloader=FakeImageDownloader(), redownload=False,
        )

        assert titles == ["新小说"]
        assert new_author_ids == {1}

        with session_factory() as session:
            assert session.get(models.Novel, 100) is not None
            assert session.get(models.Author, 1) is not None


class _NullWebviewClient:
    """webview_novel always returns None — simulates a deleted novel."""

    async def webview_novel(self, novel_id):
        return None


@pytest.mark.slow
class TestNovelFetchFailureRecorded:
    """Regression: novel_fetch must record fetch failures into failed_novel
    (aligned with the batch path), so deleted novels leave a trace."""

    async def test_fetch_failure_recorded(self, file_engine):
        engine = _engine_with_fts(file_engine)
        session_factory = create_session_factory(engine)
        uow = SqlUnitOfWork(session_factory)

        result = await novel_tasks.novel_fetch(
            novel_tasks.NovelFetchArgs(id=999),
            novel_tasks.TaskContext(
                client=_NullWebviewClient(),
                uow=uow,
                file_storage=None,
                image_downloader=None,
                write_lock=DbWriteLock(),
            ),
        )

        assert "获取失败" in result.summary
        with session_factory() as s:
            row = s.get(models.FailedNovel, 999)
            assert row is not None
            assert row.failure_type == "download"
            assert "webview_novel 返回空" in row.error_message


class _RetryWebviewClient:
    """webview_novel returns a valid novel; user_detail resolves the author."""

    async def webview_novel(self, novel_id):
        return SimpleNamespace(
            id=novel_id, title="重试小说", user_id=1,
            rating=SimpleNamespace(bookmark=5, view=10),
            text="正文内容", caption="中文标题", series_id=None,
            series_title=None, series_navigation=None,
            cdate="2026-01-01T00:00:00", tags=["中文"],
            images=None, illusts=None, cover_url=None,
        )

    async def user_detail(self, user_id):
        return {"user": {"name": "测试作者"}}


class _RetryStorage:
    download_dir = "/tmp/retry-download"

    def save_novel_text(self, novel_id, title, content, force=False):
        pass


class _RetryImageDownloader:
    async def process_novel_assets(self, data, force=False):
        pass

    async def await_all(self):
        return []


@pytest.mark.slow
class TestFailedRetryTask:
    """failed_retry: successful retries clear the failure ledger entry."""

    async def test_retry_success_clears_record(self, file_engine):
        session_factory = create_session_factory(file_engine)
        with session_factory() as s:
            s.add(models.FailedNovel(
                novel_id=100, failure_type="download",
                error_message="File name too long", failed_times=3,
            ))
            s.commit()

        ctx = novel_tasks.TaskContext(
            client=_RetryWebviewClient(),
            uow=SqlUnitOfWork(session_factory),
            session_factory=session_factory,
            file_storage=_RetryStorage(),
            image_downloader=_RetryImageDownloader(),
            write_lock=DbWriteLock(),
        )
        result = await novel_tasks.failed_retry(
            novel_tasks.FailedRetryArgs(novel_ids=[100]), ctx,
        )

        assert "成功 1/1" in result.summary
        with session_factory() as s:
            assert s.get(models.FailedNovel, 100) is None
            assert s.get(models.Novel, 100) is not None

    async def test_retry_missing_novel_re_records(self, file_engine):
        """webview 返回空 → 重试失败 → 记录保留并计数 +1。"""
        session_factory = create_session_factory(file_engine)
        with session_factory() as s:
            s.add(models.FailedNovel(
                novel_id=999, failure_type="download",
                error_message="旧错误", failed_times=1,
            ))
            s.commit()

        ctx = novel_tasks.TaskContext(
            client=_NullWebviewClient(),
            uow=SqlUnitOfWork(session_factory),
            session_factory=session_factory,
            file_storage=None,
            image_downloader=None,
            write_lock=DbWriteLock(),
        )
        result = await novel_tasks.failed_retry(
            novel_tasks.FailedRetryArgs(novel_ids=[999]), ctx,
        )

        assert "成功 0/1" in result.summary
        with session_factory() as s:
            row = s.get(models.FailedNovel, 999)
            assert row is not None
            assert row.failed_times == 2
