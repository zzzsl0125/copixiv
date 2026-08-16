"""Tests for the maintenance tasks (epub status sync + FTS tasks)."""

from pathlib import Path

import pytest
from sqlalchemy import text

from copixiv.infrastructure.database.models import Author, Novel
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.tasks.context import TaskContext
from copixiv.tasks.maintenance import check_epub, check_fts, rebuild_fts


@pytest.fixture
def session_factory(session_factory):
    """The shared conftest factory, pre-seeded with Author(1) for FK needs."""
    with session_factory() as s:
        s.add(Author(author_id=1, author_name="作者"))
        s.commit()
    return session_factory


async def _seed_pending(sf, nid: int, txt_text: str, epub: bool, tmp_path: Path):
    """Insert a PENDING novel whose txt (and optionally epub) exists on disk."""
    d = tmp_path / str(nid)
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"novel{nid}.txt"
    txt.write_text(txt_text, encoding="utf-8")
    if epub:
        (d / f"novel{nid}.epub").write_text("epub")
    uow = SqlUnitOfWork(sf)
    async with uow.begin():
        uow.session.add(Novel(
            id=nid, title=f"n{nid}", author_id=1,
            path=str(txt), has_epub=1,
        ))


def _get_status(sf, nid: int) -> int:
    with sf() as s:
        return s.get(Novel, nid).has_epub


class TestCheckEpubDowngrade:
    async def test_downgrades_pending_without_placeholders(
        self, session_factory, tmp_path,
    ):
        """PENDING + no epub file + body text has no image placeholders → NO."""
        await _seed_pending(session_factory, 1, "没有图片的正文", False, tmp_path)

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "降级" in result.summary
        assert _get_status(session_factory, 1) == 0

    async def test_keeps_pending_with_placeholders(
        self, session_factory, tmp_path,
    ):
        """PENDING + no epub file + placeholders present → stays PENDING."""
        await _seed_pending(
            session_factory, 2, "正文 [uploadedimage:12345]", False, tmp_path,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 2) == 1

    async def test_completes_pending_with_epub_file(
        self, session_factory, tmp_path,
    ):
        """PENDING + epub file exists → DONE (existing behaviour)."""
        await _seed_pending(
            session_factory, 3, "有图 [uploadedimage:1]", True, tmp_path,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "已完成" in result.summary
        assert _get_status(session_factory, 3) == 2


class TestCheckEpubStalePlaceholders:
    """PENDING + placeholders + no image files ever + stale txt → downgrade."""

    async def test_downgrades_stale_placeholder_novel(
        self, session_factory, tmp_path,
    ):
        import os
        import time as _time

        await _seed_pending(
            session_factory, 4, "正文 [uploadedimage:1]", False, tmp_path,
        )
        txt = tmp_path / "4" / "novel4.txt"
        old = _time.time() - 30 * 86400          # 30 天前 = 早已放弃重试
        os.utime(txt, (old, old))

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "降级" in result.summary
        assert _get_status(session_factory, 4) == 0

    async def test_keeps_fresh_placeholder_novel(
        self, session_factory, tmp_path,
    ):
        """Placeholders + no images but txt is fresh → stays pending."""
        await _seed_pending(
            session_factory, 5, "正文 [uploadedimage:2]", False, tmp_path,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 5) == 1


class TestFtsMaintenanceTasks:
    """D1/D3: rebuild_fts + check_fts maintenance tasks."""

    @staticmethod
    def _seed(sf):
        # Author(1) already exists — the module-level session_factory
        # fixture pre-seeds it for the check_epub tests' FK requirements.
        with sf() as s:
            s.add(Novel(id=1, title="标题", author_id=1, path="/tmp/1.txt"))
            s.commit()

    @staticmethod
    def _fts_count(sf) -> int:
        with sf() as s:
            return s.execute(text("SELECT COUNT(*) FROM novel_fts")).scalar()

    async def test_rebuild_fts_task_indexes_and_reports_count(self, session_factory):
        self._seed(session_factory)

        result = await rebuild_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "重建完成" in result.summary
        assert "1 本" in result.summary
        assert self._fts_count(session_factory) == 1

    async def test_check_fts_task_reports_healthy(self, session_factory):
        self._seed(session_factory)
        await rebuild_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        result = await check_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "健康" in result.summary
        assert "条目 1/1" in result.summary

    async def test_check_fts_task_reports_missing_table(self, session_factory):
        # fixture DB has no novel_fts virtual table
        result = await check_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "索引表不存在" in result.summary
