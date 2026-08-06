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

from sqlalchemy import create_engine, event

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database import models
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database.write_lock import db_write
from copixiv.infrastructure.repositories.fts import FTSManager
from copixiv.tasks import novel_tasks
from copixiv.tasks.pipeline import _batch_handle


def _make_engine(path):
    """File-backed SQLite engine with WAL, like production."""
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        pool_size=8,
        max_overflow=0,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    models.Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        FTSManager(conn).rebuild_novel_fts()
    return engine


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


class TestChineseFiltering:
    """Regression: collect mode must still filter to Chinese novels."""

    async def test_author_fetch_filters_chinese(self, monkeypatch, tmp_path):
        engine = _make_engine(tmp_path / "author_filter.db")
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
            1, force=True, client=FakeClient([CN_NOVEL, EN_NOVEL]),
            uow=uow, file_storage=None, image_downloader=None, config=None,
        )

        assert [n["id"] for n in seen["novels"]] == [100]
        assert result.new_novel_titles == []

    async def test_novel_follow_filters_chinese(self, monkeypatch, tmp_path):
        engine = _make_engine(tmp_path / "follow_filter.db")
        session_factory = create_session_factory(engine)
        uow = SqlUnitOfWork(session_factory)

        seen: dict = {}

        async def fake_batch_handle(novels, session_factory, **kwargs):
            seen["novels"] = list(novels)
            return [], set()

        monkeypatch.setattr(novel_tasks, "_batch_handle", fake_batch_handle)

        result = await novel_tasks.novel_follow(
            client=FakeClient([CN_NOVEL, EN_NOVEL]),
            uow=uow, file_storage=None, image_downloader=None, config=None,
        )

        assert [n["id"] for n in seen["novels"]] == [100]
        assert result.new_novel_titles == []


class TestBatchHandleEndToEnd:
    """plan → download → persist writes land in the DB exactly once."""

    async def test_new_novel_persisted(self, tmp_path):
        engine = _make_engine(tmp_path / "pipeline.db")
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
