"""Tests for the maintenance tasks (epub status sync + search-index tasks)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from copixiv.core.draft import NovelDraft
from copixiv.core.models import EpubStatus
from copixiv.db.models import Author, Novel, Series
from copixiv.db.uow import SqlUnitOfWork
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import (
    _ORPHAN_CURSOR_KEY,
    _SERIES_CURSOR_KEY,
    _SWEEP_CURSOR_KEY,
    _read_cursor,
    check_epub,
    check_fts,
    rebuild_fts,
)
from copixiv.tasks.maintenance import FixSeriesIndexArgs, fix_series_index
# ``check_epub`` reads its sweep knobs from its own module globals, so patches
# that must change its behaviour target ``.epub`` (the package only re-exports
# them). Patching the package would leave the real consumer untouched.
from copixiv.tasks.maintenance import epub as maintenance_epub


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


# ``check_epub`` is a decision table over (row status x file facts x body
# facts), so its cases are data, not prose: one row per combination keeps the
# whole table visible at once and makes a new state cheap to add.  The columns
# are the same three facts the sweep reads (``state.py``): is there a readable
# container, does its XHTML still hold a raw marker, did it embed an image.
async def _run_check_epub(session_factory):
    return await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))


def _epub_case(
    nid: int, body: str, *, epub: bool, status: int,
):
    """One ``check_epub`` row: seed a novel, run the task, return the verdict."""
    return nid, body, epub, status


class TestCheckEpubDowngrade:
    @pytest.mark.parametrize(
        "body,epub,status,expected_summary,expected_status",
        [
            # PENDING + no epub + body has no placeholders -> terminal no-images.
            ("没有图片的正文", False, EpubStatus.PENDING, "确认为无图",
             EpubStatus.NO_IMAGES),
            # PENDING + no epub file + placeholders present -> stays pending.
            ("正文 [uploadedimage:12345]", False, EpubStatus.PENDING, "仍待处理",
             EpubStatus.PENDING),
            # PENDING + epub file exists -> DONE (existing behaviour).
            ("有图 [uploadedimage:1]", True, EpubStatus.PENDING, "已完成",
             EpubStatus.DONE),
        ],
        ids=["no-images-terminal", "placeholders-pending", "epub-completes"],
    )
    async def test_pending_row_verdict(
        self, session_factory, tmp_path,
        body, epub, status, expected_summary, expected_status,
    ):
        await _seed(session_factory, 1, body, epub, tmp_path, status=status)

        result = await _run_check_epub(session_factory)

        assert expected_summary in result.summary
        assert _get_status(session_factory, 1) == expected_status


def _prepare_epub(prep: str, txt_path: Path, nid: int) -> None:
    """Materialise one "EPUB file facts" column of the check_epub table."""
    if prep == "none":
        return
    if prep == "complete":
        _write_real_epub(txt_path, nid)                     # readable + image
    elif prep == "image_less":
        _write_real_epub(txt_path, nid, with_image=False)   # readable, no image
    elif prep == "cover_only":
        _write_real_epub(
            txt_path, nid, with_image=False, cover_only=True,
        )                                                  # the 216 lineage shape
    elif prep == "marker":
        _write_real_epub(txt_path, nid, with_image=True)
        _inject_marker(txt_path.with_suffix(".epub"))      # B1 residue
    else:                                                  # pragma: no cover
        raise AssertionError(f"unknown epub prep: {prep}")


class TestCheckEpubTombstoneRepair:
    """``has_epub = 0`` rows used to be invisible to check_epub (2026-09).

    229 806 rows carried 0, 3 124 of them with image placeholders in the
    body and no EPUB — they could never be repaired because the query
    filtered on ``has_epub > 0``.

    The verdict is a pure function of (row status, body placeholders, file
    facts), so every promotion rule is one row here.  Two facts carry the
    incident knowledge:

    * ``2 -> DONE`` (and ``0 -> DONE``) require ``state.is_complete`` — a file
      written while its images were missing must not be blessed, or a
      ``DONE`` row is never re-examined (B1/B2).
    * A cover-only EPUB is *not* "has an illustration": counting it was how
      the 216 empty shells got promoted.
    """

    @pytest.mark.parametrize(
        "status,body,prep,expected_summary,expected_status",
        [
            # 0 + placeholders, no file -> queue for generation.
            (EpubStatus.NO, "正文 [uploadedimage:12345]", "none",
             "启用为待处理", EpubStatus.PENDING),
            # 0 + plain body -> terminal no-images (the old code left it 0,
            # which was both re-read forever and indistinguishable from
            # "still waiting to be classified").
            (EpubStatus.NO, "纯文字无图", "none",
             "确认为无图", EpubStatus.NO_IMAGES),
            # 0 + a complete file that merely was never marked -> heal to done.
            (EpubStatus.NO, "正文 [uploadedimage:1]", "complete",
             "已完成", EpubStatus.DONE),
            # 0 + file whose images never landed -> rebuild, never done.
            (EpubStatus.NO, "正文 [uploadedimage:1]", "image_less",
             "启用为待处理", EpubStatus.PENDING),
            # 0 + complete file still carrying a raw marker -> rebuild (B1).
            (EpubStatus.NO, "正文 [pixivimage:94602186]", "marker",
             "启用为待处理", EpubStatus.PENDING),
            # 3 (terminal) + a complete file appearing later -> heal to done.
            (EpubStatus.NO_IMAGES, "纯文字无图", "complete",
             "已完成", EpubStatus.DONE),
            # 3 + cover-only file stays terminal (B2: the 216 lineage).
            (EpubStatus.NO_IMAGES, "纯文字无图", "cover_only",
             "无变化", EpubStatus.NO_IMAGES),
            # 1 + readable file embedding nothing -> regenerate, not done.
            (EpubStatus.PENDING, "正文 [uploadedimage:1]", "image_less",
             "仍待处理", EpubStatus.PENDING),
        ],
        ids=[
            "no-placeholders-no-file", "no-plain-body", "no-complete-file",
            "no-image-less", "no-leftover-marker", "no-images-healed",
            "no-images-cover-only", "pending-image-less",
        ],
    )
    async def test_verdict(
        self, session_factory, tmp_path,
        status, body, prep, expected_summary, expected_status,
    ):
        txt = await _seed(
            session_factory, 1, body,
            prep == "complete",          # _seed can build the file itself
            tmp_path, status=status,
        )
        _prepare_epub(prep, txt, 1)

        result = await _run_check_epub(session_factory)

        assert expected_summary in result.summary
        assert _get_status(session_factory, 1) == expected_status

    async def test_terminal_row_with_no_file_does_no_disk_io(
        self, session_factory, tmp_path, monkeypatch,
    ):
        """A terminal NO_IMAGES row with no file is skipped without disk I/O.

        Separate from the verdict table because this asserts *cost*, not a
        status: the sweep must not read the body of a row that cannot change.
        """
        await _seed(
            session_factory, 14, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO_IMAGES,
        )
        calls: list[str] = []
        real_scan = maintenance_epub._scan_placeholder
        monkeypatch.setattr(
            maintenance_epub, "_scan_placeholder",
            lambda p: calls.append(str(p)) or real_scan(p),
        )

        result = await _run_check_epub(session_factory)

        assert calls == []                      # no text read at all
        assert _get_status(session_factory, 14) == EpubStatus.NO_IMAGES
        assert "无变化" in result.summary


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

        result = await _run_check_epub(session_factory)

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

        result = await _run_check_epub(session_factory)

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

        await _seed_pending(
            session_factory, 30, "正文 [uploadedimage:1]", True, tmp_path,
        )

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real_sweep = maintenance_epub._sweep

        def spy(rows, offset=0):
            seen["thread"] = threading.get_ident()
            return real_sweep(rows, offset)

        monkeypatch.setattr(maintenance_epub, "_sweep", spy)

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

        result = await _run_check_epub(session_factory)

        assert "确认为无图" in result.summary
        assert _get_status(session_factory, 4) == EpubStatus.NO_IMAGES

    async def test_keeps_fresh_placeholder_novel(
        self, session_factory, tmp_path,
    ):
        """Placeholders + no images but txt is fresh → stays pending."""
        await _seed_pending(
            session_factory, 5, "正文 [uploadedimage:2]", False, tmp_path,
        )

        result = await _run_check_epub(session_factory)

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 5) == 1


class TestCheckEpubPendingShellBecomesTerminal:
    """Readable-but-unfinished EPUBs must reach a terminal state (2026-10-23).

    Production had 905 ``has_epub=1`` rows shaped exactly like this: a readable
    shell, zero illustrations on disk, the body *does* carry placeholders, txt
    untouched for months.  ``_no_images_ever_and_stale`` already described that
    case, but it was only consulted from the ``not epub_ok`` branch — so these
    rows were re-judged as pending every night, forever.
    """

    async def test_stale_shell_with_placeholders_becomes_no_images(
        self, session_factory, tmp_path,
    ):
        import os
        import time as _time

        txt = await _seed_pending(
            session_factory, 61, "正文 [uploadedimage:1]", False, tmp_path,
        )
        # A real EPUB *without* the illustration: readable, not complete.
        _write_real_epub(txt, 61, with_image=False)
        old = _time.time() - 200 * 86400
        os.utime(txt, (old, old))

        result = await _run_check_epub(session_factory)

        assert "确认为无图" in result.summary
        assert _get_status(session_factory, 61) == EpubStatus.NO_IMAGES

    async def test_fresh_shell_with_placeholders_stays_pending(
        self, session_factory, tmp_path,
    ):
        """Fresh txt = the images may still arrive; never give up early."""
        txt = await _seed_pending(
            session_factory, 62, "正文 [uploadedimage:1]", False, tmp_path,
        )
        _write_real_epub(txt, 62, with_image=False)

        result = await _run_check_epub(session_factory)

        assert "仍待处理" in result.summary
        assert _get_status(session_factory, 62) == 1

    async def test_complete_epub_with_stale_txt_is_not_downgraded(
        self, session_factory, tmp_path,
    ):
        """ORDER MATTERS: ``is_complete`` wins over the stale predicate.

        One production row is a finished, legitimately image-less EPUB whose
        txt is ancient — it satisfies the stale predicate too.  Demoting it
        would destroy a good book, so the stale check must stay *after*
        ``is_complete``.
        """
        import os
        import time as _time

        txt = await _seed_pending(
            session_factory, 63, "正文 [uploadedimage:1]", False, tmp_path,
        )
        _write_real_epub(txt, 63, with_image=True)
        old = _time.time() - 400 * 86400
        os.utime(txt, (old, old))

        result = await _run_check_epub(session_factory)

        # Promoted to DONE, never demoted to NO_IMAGES.
        assert _get_status(session_factory, 63) == EpubStatus.DONE
        assert "确认为无图" not in result.summary


class TestCheckEpubDoneMustBeComplete:
    """``DONE`` now means ``state.is_complete``, not merely "a readable zip".

    Three production rows shipped an EPUB whose XHTML still carried literal
    ``[pixivimage:…]`` markers: the reader saw raw text.  The download endpoint
    serves whatever ``has_epub=2`` points at (``NovelCard`` picks the epub
    format from that value), so "readable" was not a good enough verdict.
    """

    async def test_done_with_raw_markers_is_demoted(
        self, session_factory, tmp_path,
    ):
        # epub=False: the case builder supplies the *broken* container itself.
        txt = await _seed(
            session_factory, 64, "正文 [pixivimage:999]", False, tmp_path,
            status=EpubStatus.DONE,
        )
        _write_placeholder_epub(txt)          # readable zip, markers intact

        result = await _run_check_epub(session_factory)

        assert "回退为待处理" in result.summary
        assert _get_status(session_factory, 64) == EpubStatus.PENDING

    async def test_done_shell_with_gone_images_becomes_no_images(
        self, session_factory, tmp_path,
    ):
        """A finished-looking shell whose illustrations can never land."""
        import os
        import time as _time

        txt = await _seed(
            session_factory, 65, "正文 [uploadedimage:1]", False, tmp_path,
            status=EpubStatus.DONE,
        )
        _write_real_epub(txt, 65, with_image=False)
        old = _time.time() - 200 * 86400
        os.utime(txt, (old, old))

        await _run_check_epub(session_factory)

        assert _get_status(session_factory, 65) == EpubStatus.NO_IMAGES

    async def test_complete_epub_stays_done(self, session_factory, tmp_path):
        txt = await _seed(
            session_factory, 66, "正文 [uploadedimage:1]", False, tmp_path,
            status=EpubStatus.DONE,
        )
        _write_real_epub(txt, 66, with_image=True)

        await _run_check_epub(session_factory)

        assert _get_status(session_factory, 66) == EpubStatus.DONE


class TestCheckEpubMissingSource:
    """``path`` set but the text file is gone → ``MISSING_SOURCE`` (4).

    The production backlog was 684 such rows: metadata written by an older
    ingest that never downloaded the body, no EPUB, no image, no failure
    record, and status ``0`` — a state the reconciler never reported and never
    left.  Classifying them makes them visible; re-downloading stays a user
    action.
    """

    async def test_missing_text_file_becomes_missing_source(
        self, session_factory, tmp_path,
    ):
        # Seed a row, then delete the file behind its back.
        txt = await _seed(
            session_factory, 71, "正文 [uploadedimage:1]", False, tmp_path,
            status=EpubStatus.NO,
        )
        txt.unlink()

        result = await _run_check_epub(session_factory)

        assert "源文件丢失" in result.summary
        assert _get_status(session_factory, 71) == EpubStatus.MISSING_SOURCE

    async def test_missing_source_is_idempotent(
        self, session_factory, tmp_path,
    ):
        txt = await _seed(
            session_factory, 72, "正文", False, tmp_path,
            status=EpubStatus.MISSING_SOURCE,
        )
        txt.unlink()

        result = await _run_check_epub(session_factory)

        # Already terminal: not re-reported as a change.
        assert "源文件丢失" not in result.summary
        assert _get_status(session_factory, 72) == EpubStatus.MISSING_SOURCE

    async def test_present_text_is_not_marked_missing(
        self, session_factory, tmp_path,
    ):
        await _seed(
            session_factory, 73, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO,
        )

        await _run_check_epub(session_factory)

        assert _get_status(session_factory, 73) == EpubStatus.NO_IMAGES


class TestSearchIndexMaintenanceTasks:
    """rebuild_fts (REINDEX) + check_fts (index health) maintenance tasks."""

    _CREATE_INDEX = (
        "CREATE INDEX novel_search_bigm ON novel "
        "USING gin (search_lower gin_bigm_ops)"
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
        assert "novel_search_bigm" in result.summary

    async def test_check_fts_task_reports_healthy(self, session_factory):
        self._seed(session_factory)
        await rebuild_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        result = await check_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert "健康" in result.summary
        assert "小说 1 本" in result.summary

    async def test_check_fts_task_reports_missing_index(
        self, session_factory, pg_engine,
    ):
        # Drop the bigram GIN index, then restore it (the search column is a
        # generated column, so "missing" is the only unhealthy state left).
        with pg_engine.begin() as conn:
            conn.execute(text("DROP INDEX novel_search_bigm"))
        try:
            result = await check_fts(TaskContext(uow=SqlUnitOfWork(session_factory)))
            assert "索引不存在" in result.summary
        finally:
            with pg_engine.begin() as conn:
                conn.execute(text(self._CREATE_INDEX))


class TestCheckEpubFullTableScan:
    """每一轮都扫全表：不再有「游标以下」的行（2026-10-23 重写）。

    旧设计按 ``id > cursor`` 取 60 000 行、每块推进游标，只在「游标之上零行」时
    归零。新书每天入库，那个分支几乎不触发，于是游标只增不减：**游标以下的行
    永远不会再被检查**（生产积压 1 594 行），而任务每天照报「全表完成」。

    实测整表一轮只要几秒（终端行只 stat、DONE 只验 zip），所以覆盖面不再需要
    分片来换。这里钉住新语义：一轮 = 全表，且游标残留会被清掉。
    """

    async def test_single_run_covers_the_whole_table(self, session_factory, tmp_path):
        for nid in (41, 42, 43):
            await _seed(
                session_factory, nid, "纯文字无图", False, tmp_path,
                status=EpubStatus.NO,
            )

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        # 一轮就把三本都分类完 —— 不需要第二次运行
        for nid in (41, 42, 43):
            assert _get_status(session_factory, nid) == EpubStatus.NO_IMAGES
        assert "下轮从" not in result.summary
        assert "全表 3 行" in result.summary

    async def test_rows_below_an_old_cursor_are_still_examined(
        self, session_factory, tmp_path,
    ):
        """A leftover cursor must never strand rows again.

        Regression for the production shape: cursor at the table's tail, rows
        below it in ``0``/``1`` that the old design would never revisit.
        """
        from copixiv.core.draft import NovelDraft  # noqa: F401  (doc anchor)
        from copixiv.tasks.maintenance import _write_cursor

        for nid in (51, 52):
            await _seed(
                session_factory, nid, "纯文字无图", False, tmp_path,
                status=EpubStatus.NO,
            )

        # A stale cursor pointing past every seeded row.
        wait_cursor_writer = _write_cursor(
            SqlUnitOfWork(session_factory), 9_999_999,
        )
        await wait_cursor_writer

        result = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        assert _get_status(session_factory, 51) == EpubStatus.NO_IMAGES
        assert _get_status(session_factory, 52) == EpubStatus.NO_IMAGES
        assert "全表 2 行" in result.summary
        # The legacy cursor is cleared so nothing downstream trusts it.
        assert await _read_cursor(
            SqlUnitOfWork(session_factory)
        ) == 0

    async def test_rerun_reports_no_change_without_claiming_nothing_to_do(
        self, session_factory, tmp_path,
    ):
        await _seed(
            session_factory, 44, "纯文字无图", False, tmp_path,
            status=EpubStatus.NO,
        )

        await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))
        second = await check_epub(TaskContext(uow=SqlUnitOfWork(session_factory)))

        # 「本轮无变化」≠「没东西可查」——行数仍然是全表行数。
        assert "本轮无变化" in second.summary
        assert "全表 1 行" in second.summary
        assert _get_status(session_factory, 44) == EpubStatus.NO_IMAGES


# ---------------------------------------------------------------------------
# fix_series_index —— 章节号 / 系列 id 修复
# ---------------------------------------------------------------------------

# 系列 16242552 的实测名册顺序（**从旧到新** = 作者的章节顺序）。
# 权威号 = 名册 1 基位置：第 1 本 → 1，第 N 本 → N。2026-09-24 用 pixiv 真数据
# 逐本核对过：导航权威号 1..9 与标题 第一幕/第二幕/第2.5幕/第三幕/第四幕/第五幕/
# 第5.5幕/第六幕/第七幕 一一对应。注意这条顺序**既不是** id 序**也不是**
# create_time 序（第 3 位"第2.5幕"的 id 与发布时间都比第 4 位晚）——库里的值不是
# 真值，它正是本任务要修的对象，所以测试一律拿名册位置断言。
_SERIES_ROSTER = [
    28670265, 28701995, 28943643, 28761898, 28820329,
    28886104, 29049733, 29092404, 29173929,
]
# {novel_id: 权威章节号}，直接由实测名册位置写死
_SERIES_ROSTER_INDEX = {
    nid: pos for pos, nid in enumerate(_SERIES_ROSTER, start=1)
}


def _info(nid: int, series_id: int | None, series_name: str | None = None):
    """名册条目（真实载荷形状的替身）。

    生产里条目是 ``pixivpy3`` 的 ``JsonDict``；这里用 SimpleNamespace 是"能属性
    访问"的那一档，另见 ``test_roster_payload_as_plain_dict``（普通 dict 一档）。
    """
    return SimpleNamespace(
        id=nid,
        title=f"n{nid}",
        caption=None,
        user=SimpleNamespace(id=1, name="作者"),
        series=(
            SimpleNamespace(
                id=series_id, title=series_name or f"s{series_id}", index=None,
            )
            if series_id is not None else None
        ),
        total_bookmarks=0,
        total_view=0,
        text_length=0,
        create_date="2020-01-01 00:00:00",
        tags=[],
    )


def _info_dict(nid: int, series_id: int, series_name: str | None = None) -> dict:
    """名册条目的**普通 dict** 形状（键名照抄 ``/v2/novel/series`` 的真实载荷）。"""
    return {
        "id": nid,
        "title": f"n{nid}",
        "caption": "",
        "user": {"id": 1, "name": "作者"},
        "series": {"id": series_id, "title": series_name or f"s{series_id}"},
        "total_bookmarks": 0,
        "total_view": 0,
        "text_length": 0,
        "create_date": "2020-01-01 00:00:00",
        "tags": [{"name": "标签A"}],
    }


def _webview(nid: int, *, series_id=None, series_title=None, nav=None, user_id=1):
    """``webview_novel`` 的响应形状（``guess_series_order`` 的读法）。"""
    return SimpleNamespace(
        id=nid,
        user_id=user_id,
        series_id=series_id,
        series_title=series_title,
        series_navigation=nav,
    )


class _FakeSeriesClient:
    """``novel_series`` / ``webview_novel`` 替身：记录调用，可注入失败。"""

    def __init__(
        self, series=None, webviews=None, fail_series=(), fail_webview=(),
    ):
        self.series = series or {}
        self.webviews = webviews or {}
        self.fail_series = set(fail_series)
        self.fail_webview = set(fail_webview)
        self.series_calls: list[int] = []
        self.webview_calls: list[int] = []

    async def novel_series(self, series_id: int, fetch_all: bool = False) -> dict:
        self.series_calls.append(series_id)
        if series_id in self.fail_series:
            raise RuntimeError("pixiv unavailable")
        return {"novels": list(self.series.get(series_id, [])), "next_url": None}

    async def webview_novel(self, novel_id: int):
        self.webview_calls.append(novel_id)
        if novel_id in self.fail_webview:
            raise RuntimeError("pixiv unavailable")
        return self.webviews.get(novel_id)


def _seed_novel(
    sf, nid: int, *, series_id=None, series_name=None, series_index=None,
    author_id=1,
) -> None:
    """插一行小说（带系列列）；``series`` 占位行按 FK 需要一起插。"""
    with sf() as s:
        if series_id is not None and s.get(Series, series_id) is None:
            s.add(Series(series_id=series_id, series_name=series_name))
        s.add(Novel(
            id=nid, title=f"n{nid}", author_id=author_id,
            path=f"/tmp/{nid}.txt", series_id=series_id,
            series_name=series_name, series_index=series_index,
        ))
        s.commit()


def _series_cols(sf, nid: int) -> tuple:
    """``(series_id, series_name, series_index)``。"""
    with sf() as s:
        row = s.get(Novel, nid)
        return row.series_id, row.series_name, row.series_index


def _ctx(sf, client) -> TaskContext:
    return TaskContext(uow=SqlUnitOfWork(sf), client=client)


async def _cursor(sf, key: str) -> int:
    return await _read_cursor(SqlUnitOfWork(sf), key=key)


class TestFixSeriesIndexRosterOrder:
    """① 编号：名册顺序（从旧到新）= 作者章节顺序，章节号 = 1 基位置。"""

    async def test_roster_position_is_authoritative(self, session_factory):
        for nid in _SERIES_ROSTER:
            _seed_novel(
                session_factory, nid, series_id=16242552,
                series_name="s16242552",
            )
        client = _FakeSeriesClient(series={
            16242552: [_info(n, 16242552) for n in _SERIES_ROSTER],
        })

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert client.series_calls == [16242552]
        assert "系列章节号修复" in result.summary
        # 权威号 = 名册 1 基位置（第 1 本 → 1，第 N 本 → N）
        got = {nid: _series_cols(session_factory, nid)[2] for nid in _SERIES_ROSTER}
        assert got == _SERIES_ROSTER_INDEX
        # 三条钉死方向的断言（实测：第一幕 → 1、第2.5幕 → 3、第七幕 → 9）
        assert _SERIES_ROSTER_INDEX[28670265] == 1    # 名册第一本 = 第 1 章
        assert _SERIES_ROSTER_INDEX[28943643] == 3    # id 更晚，但插在第二幕之后
        assert _SERIES_ROSTER_INDEX[28886104] == 6    # 第五幕
        assert _SERIES_ROSTER_INDEX[29173929] == 9    # 名册最后一本 = 第 N 章

    async def test_roster_payload_as_plain_dict(self, session_factory):
        """名册条目是**普通 dict** 也要能跑（不依赖上游的对象类型）。

        生产载荷的顶层是 ``pixivpy3.utils.ParsedJson``、条目是 ``JsonDict``（dict
        子类，``__getattr__`` 回落到键访问）——``build_from_novel_info`` 的属性访问
        能跑通纯属上游实现细节，换成普通 dict 就 ``AttributeError``。本任务只从条目
        取号（``safe_get``），draft 以库内现值为底，所以三种形状都成立。
        """
        for nid in (121, 122):
            _seed_novel(session_factory, nid, series_id=911, series_name="s911")
        client = _FakeSeriesClient(series={
            911: [_info_dict(121, 911), _info_dict(122, 911)],
        })

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert "系列章节号修复" in result.summary
        assert _series_cols(session_factory, 121)[2] == 1
        assert _series_cols(session_factory, 122)[2] == 2
        # 条目里的 title / tags 一个字都不许写进库：库内原值必须原样
        with session_factory() as s:
            row = s.get(Novel, 121)
            assert row.title == "n121"
            assert row.tags == []
            assert row.text == 0

    async def test_roster_novel_not_in_library_is_not_inserted(
        self, session_factory,
    ):
        """名册里有、库里没有的一本：不建行（旧版会把它当新小说插进去）。"""
        _seed_novel(session_factory, 110, series_id=910, series_name="s910")
        rival = 29999999
        client = _FakeSeriesClient(series={
            910: [_info(110, 910), _info(rival, 910)],
        })

        await fix_series_index(FixSeriesIndexArgs(), _ctx(session_factory, client))

        # 名册位置 1 → 110 是 #1；陌生 id 一个字都不写
        assert _series_cols(session_factory, 110)[2] == 1
        with session_factory() as s:
            assert s.get(Novel, rival) is None

    async def test_fills_null_index_without_force_update(
        self, session_factory, monkeypatch,
    ):
        """号为空的行：直接补（``series_index`` 不在覆盖白名单里，靠 NULL 填充规则）。"""
        from copixiv.features.novels import ingest

        for nid in (101, 102):
            _seed_novel(session_factory, nid, series_id=900, series_name="s900")
        client = _FakeSeriesClient(series={
            900: [_info(101, 900), _info(102, 900)],      # 名册顺序 = 章节顺序
        })
        batches: list[tuple[list[int], list[str]]] = []
        real = ingest.batch_upsert

        async def spy(novels, uow, force_update=None):
            batches.append(([n.id for n in novels], list(force_update or [])))
            return await real(novels, uow, force_update)

        monkeypatch.setattr(ingest, "batch_upsert", spy)

        await fix_series_index(FixSeriesIndexArgs(), _ctx(session_factory, client))

        assert _series_cols(session_factory, 101)[2] == 1
        assert _series_cols(session_factory, 102)[2] == 2
        # 补空路径没有（也不需要）force_update
        assert batches == [([101, 102], [])]

    async def test_corrects_wrong_index_only_with_force_update(
        self, session_factory, monkeypatch,
    ):
        """号是错值的行：必须带 ``force_update=["series_index"]`` 才写得进去。

        候选系列的门槛是"系列里至少有一本缺号"，所以纠正总是搭着补空一起走：
        库里 101 / 102 的号是旧版 ``novels.sort(key=id)`` 的产物（恰好反了），
        103 缺号；名册顺序才是权威 → 101 → #1、102 → #2、103 → #3。
        """
        from copixiv.features.novels import ingest

        _seed_novel(
            session_factory, 101, series_id=901, series_name="s901",
            series_index=2,
        )
        _seed_novel(
            session_factory, 102, series_id=901, series_name="s901",
            series_index=1,
        )
        _seed_novel(session_factory, 103, series_id=901, series_name="s901")
        client = _FakeSeriesClient(series={901: [
            _info(101, 901), _info(102, 901), _info(103, 901),
        ]})
        batches: list[tuple[list[int], list[str]]] = []
        real = ingest.batch_upsert

        async def spy(novels, uow, force_update=None):
            batches.append(([n.id for n in novels], list(force_update or [])))
            return await real(novels, uow, force_update)

        monkeypatch.setattr(ingest, "batch_upsert", spy)

        await fix_series_index(FixSeriesIndexArgs(), _ctx(session_factory, client))

        assert _series_cols(session_factory, 101)[2] == 1
        assert _series_cols(session_factory, 102)[2] == 2
        assert _series_cols(session_factory, 103)[2] == 3
        # 两批：补空不带 force_update，纠正带
        assert batches == [
            ([103], []),
            ([101, 102], ["series_index"]),
        ]

    async def test_repo_rule_without_force_update_leaves_value(
        self, session_factory,
    ):
        """机制本身：非 NULL 的 ``series_index`` 不带 force_update 写不进去。

        这是上面那条 spy 断言的依据——"纠正错值"这条路径非 force_update 不可。
        """
        from copixiv.db.write_lock import run_write_transaction
        from copixiv.features.novels.ingest import batch_upsert

        _seed_novel(
            session_factory, 103, series_id=902, series_name="s902",
            series_index=7,
        )
        draft = NovelDraft(
            id=103, title="n103", author_id=1, series_id=902,
            series_name="s902", series_index=3,
        )
        uow = SqlUnitOfWork(session_factory)
        await run_write_transaction(uow, lambda uw: batch_upsert([draft], uw))
        assert _series_cols(session_factory, 103)[2] == 7      # 没写进去

        draft2 = NovelDraft(
            id=103, title="n103", author_id=1, series_id=902,
            series_name="s902", series_index=3,
        )
        uow = SqlUnitOfWork(session_factory)
        await run_write_transaction(
            uow,
            lambda uw: batch_upsert([draft2], uw, force_update=["series_index"]),
        )
        assert _series_cols(session_factory, 103)[2] == 3      # 写进去了


class TestFixSeriesIndexOrphans:
    """② 孤儿号：``series_id`` 被抹成 NULL、号还留着（2026-07-13 前的化石）。"""

    async def test_orphan_with_series_id_is_glued_back(self, session_factory):
        """查到 series_id → 并进名册批次挂回，同系列别的行一起修。"""
        # 库里挂着两本：号都写错了；孤儿 500 的 series_id 是 NULL
        _seed_novel(
            session_factory, 100, series_id=555, series_name="s555",
            series_index=9,
        )
        _seed_novel(
            session_factory, 300, series_id=555, series_name="s555",
            series_index=9,
        )
        _seed_novel(
            session_factory, 500, series_name="光与影", series_index=7,
        )
        client = _FakeSeriesClient(
            series={555: [_info(500, 555), _info(300, 555), _info(100, 555)]},
            webviews={500: _webview(500, series_id=555, series_title="网页名")},
        )

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert client.webview_calls == [500]
        assert client.series_calls == [555]              # 同一系列只拉一次名册
        # 挂回：身份三列取自 webview 响应（覆盖 NULL），号由名册位置决定（第 1 位 → #1）
        assert _series_cols(session_factory, 500) == (555, "网页名", 1)
        # 同一批次顺带纠正了同系列的两行错值（名册位置 2 / 3）
        assert _series_cols(session_factory, 300)[2] == 2
        assert _series_cols(session_factory, 100)[2] == 3
        assert "孤儿挂回 1 本" in result.summary

    async def test_orphan_without_author_uses_webview_author(self, session_factory):
        """库内没有 author_id 的孤儿：拿 webview 的 user_id 兜底才写得进去。

        生产实测 1,594 本孤儿里有 13 本 ``author_id IS NULL``——而
        ``persist_novels`` 会把 draft 的 None 拿去建作者占位行（撞 NOT NULL）。
        """
        _seed_novel(
            session_factory, 506, series_name="光与影", series_index=4,
            author_id=None,
        )
        payload = _webview(506, user_id=4242)
        client = _FakeSeriesClient(webviews={506: payload})

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        with session_factory() as s:
            row = s.get(Novel, 506)
            assert (row.author_id, row.series_name, row.series_index) == (
                4242, "光与影", None,
            )
        assert "孤儿清号 1 本" in result.summary

    async def test_orphan_without_series_clears_index_keeps_name(
        self, session_factory,
    ):
        """series_id 为 NULL → 作品确实已不在任何系列：清号、保留系列名。"""
        _seed_novel(session_factory, 501, series_name="光与影", series_index=3)
        client = _FakeSeriesClient(webviews={501: _webview(501)})

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert client.series_calls == []
        assert _series_cols(session_factory, 501) == (None, "光与影", None)
        assert "孤儿清号 1 本" in result.summary

    async def test_orphan_request_failure_changes_nothing(
        self, session_factory,
    ):
        """请求失败 → 什么都不改，但游标照样推进（失败 = 查过了，不再钉住游标）。

        失败行留在候选集里，跑完一轮归零后下一轮会再查它——见第二个 assert 后的
        第二次调用。
        """
        _seed_novel(session_factory, 502, series_name="光与影", series_index=3)
        _seed_novel(session_factory, 503, series_name="另一系列", series_index=2)
        client = _FakeSeriesClient(
            webviews={502: _webview(502), 503: _webview(503)},
            fail_webview={502},
        )

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert _series_cols(session_factory, 502) == (None, "光与影", 3)   # 没动
        assert _series_cols(session_factory, 503)[2] is None               # 后面的照跑
        assert "失败 1" in result.summary
        # 失败行留在游标之后：归零后下一轮从头来，先重试 502
        assert await _cursor(session_factory, _ORPHAN_CURSOR_KEY) == 0

        client.fail_webview.clear()
        await fix_series_index(FixSeriesIndexArgs(), _ctx(session_factory, client))
        assert _series_cols(session_factory, 502) == (None, "光与影", None)

    async def test_single_book_series_uses_navigation_not_roster(
        self, session_factory,
    ):
        """库里这个系列只有这一本、且导航能算出号 → 不拉名册，直接挂回。"""
        _seed_novel(session_factory, 504, series_name="旧名", series_index=9)
        client = _FakeSeriesClient(webviews={504: _webview(
            504, series_id=777, series_title="s777",
            nav={"prevNovel": {"contentOrder": 2}, "nextNovel": None},
        )})

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert client.series_calls == []                     # 省掉名册请求
        assert _series_cols(session_factory, 504) == (777, "s777", 3)
        assert "孤儿挂回 1 本" in result.summary

    async def test_roster_failure_does_not_glue(self, session_factory):
        """名册拉不到 → 不挂回（不猜位置），行留着下轮重试。"""
        _seed_novel(session_factory, 505, series_name="旧名", series_index=4)
        client = _FakeSeriesClient(
            fail_series={888},
            webviews={505: _webview(505, series_id=888, series_title="s888")},
        )

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert _series_cols(session_factory, 505) == (None, "旧名", 4)
        assert "API 请求全部失败" in result.summary


class TestFixSeriesIndexCursor:
    """⑤ 游标 + 分批：先落库再推进、续跑、走完一轮归零。"""

    async def test_series_cursor_advances_then_resumes(self, session_factory):
        for nid, sid in ((601, 801), (602, 802)):
            _seed_novel(session_factory, nid, series_id=sid, series_name=f"s{sid}")
        client = _FakeSeriesClient(series={
            801: [_info(601, 801)], 802: [_info(602, 802)],
        })

        first = await fix_series_index(
            FixSeriesIndexArgs(max_series=1), _ctx(session_factory, client),
        )

        assert client.series_calls == [801]                 # 上限 1
        assert "本轮达上限" in first.summary
        assert _series_cols(session_factory, 601)[2] == 1   # 先落库
        assert await _cursor(session_factory, _SERIES_CURSOR_KEY) == 801
        assert _series_cols(session_factory, 602)[2] is None

        second = await fix_series_index(
            FixSeriesIndexArgs(max_series=1), _ctx(session_factory, client),
        )

        assert client.series_calls == [801, 802]            # 从游标续
        assert _series_cols(session_factory, 602)[2] == 1
        # 一轮走完候选集 → 归零，下一轮从头复查
        assert await _cursor(session_factory, _SERIES_CURSOR_KEY) == 0
        # check_epub 的游标键不受影响（各推各的）
        assert await _cursor(session_factory, "check_epub.cursor") == 0

        third = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )
        assert "无需修复" in third.summary
        assert client.series_calls == [801, 802]            # 候选集空了，不发请求

    async def test_orphan_cursor_advances_then_wraps(self, session_factory):
        for nid in (611, 612):
            _seed_novel(session_factory, nid, series_name="光与影", series_index=1)
        client = _FakeSeriesClient(
            webviews={611: _webview(611), 612: _webview(612)},
        )

        first = await fix_series_index(
            FixSeriesIndexArgs(max_orphans=1), _ctx(session_factory, client),
        )

        assert client.webview_calls == [611]
        assert _series_cols(session_factory, 611)[2] is None
        assert _series_cols(session_factory, 612)[2] == 1
        assert await _cursor(session_factory, _ORPHAN_CURSOR_KEY) == 611

        second = await fix_series_index(
            FixSeriesIndexArgs(max_orphans=1), _ctx(session_factory, client),
        )

        assert client.webview_calls == [611, 612]
        assert _series_cols(session_factory, 612)[2] is None
        # 跑完一轮归零，下一轮从头复查
        assert await _cursor(session_factory, _ORPHAN_CURSOR_KEY) == 0

    async def test_no_params_behaves_like_before(self, session_factory):
        """参数缺失 = 不限量（旧行为）：一轮把候选集走完并归零。"""
        _seed_novel(session_factory, 621, series_id=803, series_name="s803")
        client = _FakeSeriesClient(series={803: [_info(621, 803)]})

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert _series_cols(session_factory, 621)[2] == 1
        assert await _cursor(session_factory, _SERIES_CURSOR_KEY) == 0
        assert "本轮达上限" not in result.summary


class TestFixSeriesIndexSweepAll:
    """④ 全量复查（``sweep_all``）：号齐全但值是错的系列，默认没人管。"""

    async def test_default_does_not_touch_fully_indexed_series(
        self, session_factory,
    ):
        """默认关闭：号齐全的系列**发都不发请求**（旧行为原样保留）。"""
        _seed_novel(
            session_factory, 701, series_id=950, series_name="s950",
            series_index=2,
        )
        _seed_novel(
            session_factory, 702, series_id=950, series_name="s950",
            series_index=1,
        )
        client = _FakeSeriesClient(series={
            950: [_info(701, 950), _info(702, 950)],
        })

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),
        )

        assert client.series_calls == []
        assert "无需修复" in result.summary
        # 两行的号都是反的，但默认模式不动它们
        assert _series_cols(session_factory, 701)[2] == 2
        assert _series_cols(session_factory, 702)[2] == 1

    async def test_sweep_all_corrects_fully_indexed_series(
        self, session_factory, monkeypatch,
    ):
        """打开后：号齐全的系列也拉名册核对，错值按 force_update 纠正。"""
        from copixiv.features.novels import ingest

        _seed_novel(
            session_factory, 701, series_id=950, series_name="s950",
            series_index=2,                       # 错：名册位置 1
        )
        _seed_novel(
            session_factory, 702, series_id=950, series_name="s950",
            series_index=7,                       # 错：名册位置 2
        )
        client = _FakeSeriesClient(series={
            950: [_info(701, 950), _info(702, 950)],
        })
        batches: list[tuple[list[int], list[str]]] = []
        real = ingest.batch_upsert

        async def spy(novels, uow, force_update=None):
            batches.append(([n.id for n in novels], list(force_update or [])))
            return await real(novels, uow, force_update)

        monkeypatch.setattr(ingest, "batch_upsert", spy)

        result = await fix_series_index(
            FixSeriesIndexArgs(sweep_all=True), _ctx(session_factory, client),
        )

        assert client.series_calls == [950]
        assert _series_cols(session_factory, 701)[2] == 1
        assert _series_cols(session_factory, 702)[2] == 2
        # 只写"与名册位置不一致"的那两行，且必须 force_update
        assert batches == [([701, 702], ["series_index"])]
        assert "纠正 2 本" in result.summary
        assert "复查 1" in result.summary
        # 一轮走完候选集 → 复查游标归零
        assert await _cursor(session_factory, _SWEEP_CURSOR_KEY) == 0

        # 第二轮：号已一致 → 一个字都不写（幂等）
        batches.clear()
        again = await fix_series_index(
            FixSeriesIndexArgs(sweep_all=True), _ctx(session_factory, client),
        )
        assert batches == []
        assert "复查 1" in again.summary

    async def test_sweep_all_includes_single_book_series(self, session_factory):
        """单本系列也在候选内：库里只有一本不代表 pixiv 只有一本，名册才是权威。"""
        _seed_novel(
            session_factory, 703, series_id=951, series_name="s951",
            series_index=5,
        )
        client = _FakeSeriesClient(series={951: [_info(703, 951)]})

        await fix_series_index(
            FixSeriesIndexArgs(sweep_all=True), _ctx(session_factory, client),
        )

        assert client.series_calls == [951]
        assert _series_cols(session_factory, 703)[2] == 1

    async def test_sweep_all_does_not_double_fetch_null_series(
        self, session_factory,
    ):
        """有缺号的系列归 ① 管：两个分支的候选互补，同一轮只拉一次名册。"""
        _seed_novel(session_factory, 704, series_id=952, series_name="s952")
        _seed_novel(
            session_factory, 705, series_id=952, series_name="s952",
            series_index=1,
        )
        _seed_novel(session_factory, 706, series_id=953, series_name="s953")
        client = _FakeSeriesClient(series={
            952: [_info(704, 952), _info(705, 952)],
            953: [_info(706, 953)],
        })

        await fix_series_index(
            FixSeriesIndexArgs(sweep_all=True), _ctx(session_factory, client),
        )

        # 952 有缺号（① 管）、953 缺号（① 管）→ 复查分支没有候选
        assert sorted(client.series_calls) == [952, 953]
        assert _series_cols(session_factory, 704)[2] == 1
        assert _series_cols(session_factory, 705)[2] == 2
        assert _series_cols(session_factory, 706)[2] == 1

    async def test_sweep_cursor_resumes_with_cap(self, session_factory):
        """复查也有自己的游标：``max_sweep`` 切分后从断点续，跑完归零。"""
        for nid, sid in ((711, 960), (712, 961)):
            _seed_novel(
                session_factory, nid, series_id=sid, series_name=f"s{sid}",
                series_index=9,
            )
        client = _FakeSeriesClient(series={
            960: [_info(711, 960)], 961: [_info(712, 961)],
        })

        first = await fix_series_index(
            FixSeriesIndexArgs(sweep_all=True, max_sweep=1),
            _ctx(session_factory, client),
        )

        assert client.series_calls == [960]
        assert "本轮达上限" in first.summary
        assert _series_cols(session_factory, 711)[2] == 1
        assert await _cursor(session_factory, _SWEEP_CURSOR_KEY) == 960
        assert _series_cols(session_factory, 712)[2] == 9      # 没轮到

        second = await fix_series_index(
            FixSeriesIndexArgs(sweep_all=True, max_sweep=1),
            _ctx(session_factory, client),
        )

        assert client.series_calls == [960, 961]
        assert _series_cols(session_factory, 712)[2] == 1
        assert await _cursor(session_factory, _SWEEP_CURSOR_KEY) == 0


class TestFixSeriesIndexStarvation:
    """失败行饿死防线：配上"每轮上限"时，整页失败也必须能推进。"""

    async def test_series_page_all_failed_advances_cursor(self, session_factory):
        for nid, sid in ((801, 970), (802, 971)):
            _seed_novel(
                session_factory, nid, series_id=sid, series_name=f"s{sid}",
                series_index=None,
            )
        client = _FakeSeriesClient(
            series={971: [_info(802, 971)]}, fail_series={970},
        )

        # 上限 1：第一页只有永久失败的 970
        first = await fix_series_index(
            FixSeriesIndexArgs(max_series=1), _ctx(session_factory, client),
        )

        assert client.series_calls == [970]
        assert "API 请求全部失败" in first.summary      # 本轮一个都没处理成
        # 整页失败 → 游标越过它，否则 971 永远轮不到
        assert await _cursor(session_factory, _SERIES_CURSOR_KEY) == 970

        second = await fix_series_index(
            FixSeriesIndexArgs(max_series=1), _ctx(session_factory, client),
        )

        assert client.series_calls == [970, 971]
        assert _series_cols(session_factory, 802)[2] == 1

    async def test_mixed_page_advances_past_failed_series(self, session_factory):
        """页里有成功行时同样越过失败行：失败 = 查过了，不当场钉死游标。

        旧语义是"有成功行就不越过失败行，下一轮先重试它"；配上"每轮上限"时，
        页首一个永久失败行会把游标钉住 → 后面的行永远轮不到（饿死）。新语义一律
        推进，失败行留到整轮反查（候选集归零后）再复查。
        """
        for nid, sid in ((811, 980), (812, 981)):
            _seed_novel(
                session_factory, nid, series_id=sid, series_name=f"s{sid}",
            )
        client = _FakeSeriesClient(
            series={980: [_info(811, 980)], 981: [_info(812, 981)]},
            fail_series={980},
        )

        first = await fix_series_index(
            FixSeriesIndexArgs(max_series=2), _ctx(session_factory, client),
        )

        assert client.series_calls == [980, 981]
        assert _series_cols(session_factory, 812)[2] == 1      # 后面的照跑
        assert "失败 1" in first.summary
        # 整页扫完（未截断）→ 归零：失败行仍在候选集里，下一轮从头复查时重试
        assert await _cursor(session_factory, _SERIES_CURSOR_KEY) == 0

        client.fail_series.clear()
        await fix_series_index(
            FixSeriesIndexArgs(max_series=2), _ctx(session_factory, client),
        )
        # 整轮反查把 980 补上（它从未被移出候选集）
        assert _series_cols(session_factory, 811)[2] == 1

    async def test_orphan_page_all_failed_advances_cursor(self, session_factory):
        for nid in (821, 822):
            _seed_novel(
                session_factory, nid, series_name="光与影", series_index=3,
            )
        client = _FakeSeriesClient(
            webviews={821: _webview(821), 822: _webview(822)},
            fail_webview={821},
        )

        first = await fix_series_index(
            FixSeriesIndexArgs(max_orphans=1), _ctx(session_factory, client),
        )

        assert client.webview_calls == [821]
        assert await _cursor(session_factory, _ORPHAN_CURSOR_KEY) == 821

        second = await fix_series_index(
            FixSeriesIndexArgs(max_orphans=1), _ctx(session_factory, client),
        )

        assert client.webview_calls == [821, 822]
        assert _series_cols(session_factory, 822)[2] is None

    async def test_page_starting_with_permanent_failure_still_makes_progress(
        self, session_factory,
    ):
        """无上限 + 页首永久失败：本轮必须**越过它**继续修，而不是空转到超时。

        生产复现（2026-10-07，task_history id=1489）：一轮从系列 1244949 开始，
        页首就是 404，一路扫到 15764828 后 30 分钟被超时杀掉；因为整页从未扫完，
        游标一直是 0，下一轮又从 1244949 从头来。这里把 1243 个候选压成 2 个，
        断言"页首失败不影响后面的系列被修"——这就是新的推进规则在保护的东西。
        """
        _seed_novel(
            session_factory, 831, series_id=1244949, series_name="坏系列",
            series_index=None,
        )
        _seed_novel(
            session_factory, 832, series_id=1244950, series_name="好系列",
            series_index=None,
        )
        client = _FakeSeriesClient(
            series={1244950: [_info(832, 1244950)]},
            fail_series={1244949},
        )

        result = await fix_series_index(
            FixSeriesIndexArgs(), _ctx(session_factory, client),   # 不限量
        )

        assert client.series_calls == [1244949, 1244950]          # 失败也往下走
        assert _series_cols(session_factory, 831)[2] is None      # 失败行没动
        assert _series_cols(session_factory, 832)[2] == 1         # 后面的修好了
        assert "失败 1" in result.summary
