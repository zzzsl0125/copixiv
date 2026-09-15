"""Tests for the maintenance tasks (epub status sync + search-index tasks)."""

from pathlib import Path

import pytest
from sqlalchemy import text

from copixiv.core.draft import NovelDraft
from copixiv.core.models import EpubStatus
from copixiv.db.models import Author, Novel
from copixiv.db.uow import SqlUnitOfWork
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import check_epub, check_fts, rebuild_fts


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


@pytest.fixture
def session_factory(session_factory):
    """The shared conftest factory, pre-seeded with Author(1) for FK needs."""
    with session_factory() as s:
        s.add(Author(author_id=1, author_name="作者"))
        s.commit()
    return session_factory


def _write_real_epub(
    txt: Path, novel_id: int, *, with_image: bool = True,
    cover_only: bool = False,
) -> None:
    """Build a genuine EPUB next to *txt* (the reconciler validates the zip).

    ``with_image`` embeds one 2×2 JPEG as ``EPUB/images/1.jpg`` — the same
    name the builder derives from the ``{novel_id}_u_1.jpg`` asset on disk —
    so the ``0 → 2`` promotion rule (which requires an embedded illustration)
    is exercised for real instead of against an empty shell.

    ``cover_only`` additionally drops a cover JPEG at ``EPUB/cover.jpg``,
    reproducing the shape of the 216 broken books: a perfectly readable EPUB
    with a cover and **no** illustration.  The reconciler must not promote
    those to done (the cover is not an embedded image).
    """
    if with_image:
        from PIL import Image

        Image.new("RGB", (2, 2), (255, 0, 0)).save(
            txt.parent / f"{novel_id}_u_1.jpg"
        )
        # The builder only embeds images for markers the *text* carries
        # (``needs_epub`` semantics), so the marker is part of the fixture.
        body = txt.read_text(encoding="utf-8")
        if "[uploadedimage:1]" not in body:
            txt.write_text(body + "\n[uploadedimage:1]\n", encoding="utf-8")
    draft = NovelDraft(
        id=novel_id,
        title="t",
        author_id=1,
        path=str(txt),
        images={"1": {}} if with_image else None,
    )
    assert EpubBuilder().create_epub(draft) is True
    if cover_only:
        import zipfile

        from PIL import Image

        cover = txt.parent / f"{novel_id}_c_cover.jpg"
        Image.new("RGB", (2, 2), (0, 0, 255)).save(cover)
        epub = txt.with_suffix(".epub")
        with zipfile.ZipFile(epub, "a") as zf:
            zf.write(cover, "EPUB/cover.jpg")


def _write_placeholder_epub(txt: Path) -> None:
    """A valid EPUB whose XHTML still holds a raw image marker."""
    import zipfile

    out = txt.with_suffix(".epub")
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "EPUB/content.xhtml",
            "<html><body>正文 [pixivimage:94602186]</body></html>",
        )


def _inject_marker(epub: Path) -> None:
    """Rewrite an existing EPUB's XHTML so a raw marker survives in it.

    Reproduces the exact residue of the 216 books: images embedded, yet one
    placeholder was never resolved and shipped as literal text.
    """
    import zipfile

    with zipfile.ZipFile(epub) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    for name, data in items.items():
        if name.endswith(".xhtml"):
            items[name] = data.replace(
                b"</body>", b"<p>[pixivimage:94602186]</p></body>"
            )
    with zipfile.ZipFile(epub, "w") as zf:
        for name, data in items.items():
            zf.writestr(name, data)


async def _seed(
    sf, nid: int, txt_text: str, epub: bool, tmp_path: Path,
    status: int = EpubStatus.PENDING, real_epub: bool = True,
):
    """Insert a novel whose txt (and optionally epub) exists on disk."""
    d = tmp_path / str(nid)
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"novel{nid}.txt"
    txt.write_text(txt_text, encoding="utf-8")
    if epub:
        if real_epub:
            _write_real_epub(txt, nid)
        else:
            (d / f"novel{nid}.epub").write_text("epub")
    uow = SqlUnitOfWork(sf)
    async with uow.begin():
        uow.session.add(Novel(
            id=nid, title=f"n{nid}", author_id=1,
            path=str(txt), has_epub=status,
        ))
    return txt


async def _seed_pending(sf, nid: int, txt_text: str, epub: bool, tmp_path: Path):
    """Insert a PENDING novel whose txt (and optionally epub) exists on disk."""
    return await _seed(
        sf, nid, txt_text, epub, tmp_path, status=EpubStatus.PENDING,
    )


def _get_status(sf, nid: int) -> int:
    with sf() as s:
        return s.get(Novel, nid).has_epub


class TestCheckEpubDowngrade:
    async def test_downgrades_pending_without_placeholders(
        self, session_factory, tmp_path,
    ):
        """PENDING + no epub + body has no placeholders → NO_IMAGES (terminal)."""
        await _seed_pending(session_factory, 1, "没有图片的正文", False, tmp_path)

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "确认为无图" in result.summary
        assert _get_status(session_factory, 1) == EpubStatus.NO_IMAGES

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


class TestCheckEpubTombstoneRepair:
    """``has_epub = 0`` rows used to be invisible to check_epub (2026-09).

    229 806 rows carried 0, 3 124 of them with image placeholders in the
    body and no EPUB — they could never be repaired because the query
    filtered on ``has_epub > 0``.
    """

    async def test_no_with_placeholders_and_no_file_becomes_pending(
        self, session_factory, tmp_path,
    ):
        await _seed(
            session_factory, 10, "正文 [uploadedimage:12345]", False, tmp_path,
            status=EpubStatus.NO,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "启用为待处理" in result.summary
        assert _get_status(session_factory, 10) == 1

    async def test_no_without_placeholders_becomes_no_images(
        self, session_factory, tmp_path,
    ):
        """Unclassified row + body has no placeholders → terminal NO_IMAGES.

        The old behaviour left it at 0, which meant the row was re-read from
        disk on every future sweep *and* was indistinguishable from a row
        still waiting for classification.
        """
        await _seed(
            session_factory, 11, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "确认为无图" in result.summary
        assert _get_status(session_factory, 11) == EpubStatus.NO_IMAGES

    async def test_no_images_row_is_not_rescanned(
        self, session_factory, tmp_path, monkeypatch,
    ):
        """A terminal NO_IMAGES row with no file is skipped without disk I/O."""
        await _seed(
            session_factory, 14, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO_IMAGES,
        )
        from copixiv.tasks import maintenance

        calls: list[str] = []
        real_scan = maintenance._scan_placeholder
        monkeypatch.setattr(
            maintenance, "_scan_placeholder",
            lambda p: calls.append(str(p)) or real_scan(p),
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert calls == []                      # no text read at all
        assert _get_status(session_factory, 14) == EpubStatus.NO_IMAGES
        assert result.summary.endswith("无变化")

    async def test_no_images_row_with_complete_file_becomes_done(
        self, session_factory, tmp_path,
    ):
        """A *complete* file appearing later heals a terminal row."""
        await _seed(
            session_factory, 15, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO_IMAGES,
        )
        _write_real_epub(tmp_path / "15" / "novel15.txt", 15)   # 有插图

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "已完成" in result.summary
        assert _get_status(session_factory, 15) == EpubStatus.DONE

    async def test_no_images_row_with_cover_only_file_stays_terminal(
        self, session_factory, tmp_path,
    ):
        """A cover-only EPUB must NOT drag a terminal row back to done.

        This is the B2 case from the 2026-09-16 review: the old rule promoted
        on ``is_valid_epub`` alone, so any readable zip — including the empty
        shells of the 216-lineage — was blessed as finished.
        """
        await _seed(
            session_factory, 17, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO_IMAGES,
        )
        _write_real_epub(
            tmp_path / "17" / "novel17.txt", 17,
            with_image=False, cover_only=True,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert _get_status(session_factory, 17) == EpubStatus.NO_IMAGES
        assert result.summary.endswith("无变化")

    async def test_pending_with_image_less_epub_is_queued(
        self, session_factory, tmp_path,
    ):
        """PENDING + valid file but nothing embedded → regenerate, not done."""
        await _seed(
            session_factory, 16, "正文 [uploadedimage:1]", False, tmp_path,
            status=EpubStatus.PENDING,
        )
        _write_real_epub(tmp_path / "16" / "novel16.txt", 16, with_image=False)

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 16) == EpubStatus.PENDING

    async def test_no_with_leftover_marker_epub_is_queued_not_done(
        self, session_factory, tmp_path,
    ):
        """B1 regression: ``0 → 2`` must check content, not just zip-ness.

        The ``1 → 2`` branch already refused to bless a file whose XHTML still
        held a raw marker; the ``0 → 2`` branch did not, so the first full
        sweep of 229 k unclassified rows would have re-blessed the 216
        leftover-marker EPUBs as finished — and a ``2`` row is never
        re-examined.  Both promotions now share ``_epub_is_complete``.
        """
        txt = await _seed(
            session_factory, 18, "正文 [pixivimage:94602186]", False, tmp_path,
            status=EpubStatus.NO,
        )
        _write_real_epub(txt, 18, with_image=True)      # 有插图……
        _inject_marker(txt.with_suffix(".epub"))        # ……但 XHTML 残留裸标记

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "启用为待处理" in result.summary
        assert _get_status(session_factory, 18) != EpubStatus.DONE

    async def test_no_with_epub_containing_images_becomes_done(
        self, session_factory, tmp_path,
    ):
        """The 784-file heal: EPUB already on disk, status never updated."""
        await _seed(
            session_factory, 12, "正文 [uploadedimage:1]", True, tmp_path,
            status=EpubStatus.NO,
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "已完成" in result.summary
        assert _get_status(session_factory, 12) == 2

    async def test_no_with_image_less_epub_is_queued_for_rebuild(
        self, session_factory, tmp_path,
    ):
        """File exists but embeds nothing → rebuild, do not call it done."""
        await _seed(
            session_factory, 13, "正文 [uploadedimage:1]", False, tmp_path,
            status=EpubStatus.NO,
        )
        # A valid EPUB built before the image landed: no EPUB/images/ entry.
        _write_real_epub(tmp_path / "13" / "novel13.txt", 13, with_image=False)

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "启用为待处理" in result.summary
        assert _get_status(session_factory, 13) == 1


class TestCheckEpubCorruptAndLeftoverEpubs:
    """Corrupt zips and EPUBs holding raw placeholders are not "done"."""

    async def test_corrupt_epub_marked_done_is_reverted(
        self, session_factory, tmp_path,
    ):
        """Two non-zip files were served to readers for months."""
        await _seed(
            session_factory, 20, "正文 [uploadedimage:1]", True, tmp_path,
            status=EpubStatus.DONE, real_epub=False,   # text stub, not a zip
        )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "回退" in result.summary
        assert _get_status(session_factory, 20) == 1

    async def test_pending_epub_with_leftover_placeholders_stays_pending(
        self, session_factory, tmp_path,
    ):
        """216 EPUBs shipped with ``[pixivimage:…]`` still in the text."""
        txt = await _seed_pending(
            session_factory, 21, "正文 [pixivimage:94602186]", False, tmp_path,
        )
        _write_placeholder_epub(txt)

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 21) == 1


class TestCheckEpubDoesNotBlockTheEventLoop:
    """The sweep reads every novel from disk (232 k rows in production).

    ``asyncio.wait_for`` runs an async task *on* the event loop, so the
    disk work must be handed to a worker thread — otherwise a cron run
    freezes the whole backend (no API responses) for its full duration.
    """

    async def test_sweep_runs_off_the_event_loop_thread(
        self, session_factory, tmp_path, monkeypatch,
    ):
        import threading

        from copixiv.tasks import maintenance

        await _seed_pending(
            session_factory, 30, "正文 [uploadedimage:1]", True, tmp_path,
        )

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real_sweep = maintenance._sweep

        def spy(rows, offset=0):
            seen["thread"] = threading.get_ident()
            return real_sweep(rows, offset)

        monkeypatch.setattr(maintenance, "_sweep", spy)

        await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert seen["thread"] != loop_thread


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

        assert "确认为无图" in result.summary
        assert _get_status(session_factory, 4) == EpubStatus.NO_IMAGES

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


class TestSearchIndexMaintenanceTasks:
    """rebuild_fts (REINDEX) + check_fts (index health) maintenance tasks."""

    _CREATE_INDEX = (
        "CREATE INDEX novel_search_gin ON novel USING gin ("
        "to_tsvector('simple', copixiv_novel_text("
        "title, author_name, series_name, tags)))"
    )

    @staticmethod
    def _seed(sf):
        # Author(1) already exists — the module-level session_factory
        # fixture pre-seeds it for the check_epub tests' FK requirements.
        with sf() as s:
            s.add(Novel(id=1, title="标题", author_id=1, path="/tmp/1.txt"))
            s.commit()

    async def test_rebuild_fts_task_reindexes(self, session_factory):
        self._seed(session_factory)

        result = await rebuild_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "重建完成" in result.summary
        assert "novel_search_gin" in result.summary

    async def test_check_fts_task_reports_healthy(self, session_factory):
        self._seed(session_factory)
        await rebuild_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        result = await check_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "健康" in result.summary
        assert "小说 1 本" in result.summary

    async def test_check_fts_task_reports_missing_index(
        self, session_factory, pg_engine,
    ):
        # Drop the expression index, then restore it (the search index is a
        # plain index now, so "missing" is the only unhealthy state left).
        with pg_engine.begin() as conn:
            conn.execute(text("DROP INDEX novel_search_gin"))
        try:
            result = await check_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))
            assert "索引不存在" in result.summary
        finally:
            with pg_engine.begin() as conn:
                conn.execute(text(self._CREATE_INDEX))
