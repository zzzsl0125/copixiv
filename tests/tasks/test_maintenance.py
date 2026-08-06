"""Tests for the check_epub maintenance task (status sync + downgrade)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Author, Novel
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.tasks.maintenance import check_epub


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    sf = create_session_factory(engine)
    with sf() as s:
        s.add(Author(author_id=1, author_name="作者"))
        s.commit()
    return sf


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

        result = await check_epub(uow=SqlUnitOfWork(session_factory))

        assert "降级" in result.summary
        assert _get_status(session_factory, 1) == 0

    async def test_keeps_pending_with_placeholders(
        self, session_factory, tmp_path,
    ):
        """PENDING + no epub file + placeholders present → stays PENDING."""
        await _seed_pending(
            session_factory, 2, "正文 [uploadedimage:12345]", False, tmp_path,
        )

        result = await check_epub(uow=SqlUnitOfWork(session_factory))

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 2) == 1

    async def test_completes_pending_with_epub_file(
        self, session_factory, tmp_path,
    ):
        """PENDING + epub file exists → DONE (existing behaviour)."""
        await _seed_pending(
            session_factory, 3, "有图 [uploadedimage:1]", True, tmp_path,
        )

        result = await check_epub(uow=SqlUnitOfWork(session_factory))

        assert "已完成" in result.summary
        assert _get_status(session_factory, 3) == 2
