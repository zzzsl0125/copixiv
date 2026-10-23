"""``prune_orphan_files`` — reclaim files no row points at.

Motivation (production, 2026-10-24): 4 130 ``.txt`` files (133.3 MB) and 142
``.epub`` files had no row pointing at them, left behind by deletions whose
file half is *best-effort* by design (``DeleteNovelUseCase``: DB first, files
second).  3 877 of those text files were older than 180 days.

The tests pin the safety conditions, because this task deletes files: the
dangerous failure is not "missed an orphan" but "removed a live book".
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from copixiv.core.models import EpubStatus
from copixiv.db.models import Author, Novel
from copixiv.db.uow import SqlUnitOfWork
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import PruneOrphanFilesArgs, prune_orphan_files


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


@pytest.fixture
def session_factory(session_factory):
    with session_factory() as s:
        s.add(Author(author_id=1))
        s.commit()
    return session_factory


def _seed_row(sf, novel_id: int, path: Path) -> None:
    with sf() as s:
        s.add(Novel(
            id=novel_id, title=f"n{novel_id}", author_id=1, path=str(path),
            like=1, text=10, has_epub=EpubStatus.NO_IMAGES,
        ))
        s.commit()


def _aged(path: Path, days: int = 30) -> Path:
    """Backdate a file so the ``min_age_days`` guard does not filter it."""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _ctx(sf, storage) -> TaskContext:
    return TaskContext(
        uow=SqlUnitOfWork(sf), session_factory=sf, file_storage=storage,
    )


class TestPruneOrphanFiles:
    async def test_dry_run_reports_without_deleting(self, tmp_path, session_factory):
        """Default is dry-run: an operator must see the list first."""
        storage = FileStorage(str(tmp_path))
        orphan = _aged(_write(tmp_path / "0000" / "旧书_111.txt"))

        result = await prune_orphan_files(
            PruneOrphanFilesArgs(), _ctx(session_factory, storage),
        )

        assert orphan.is_file(), "默认不删"
        assert "试运行" in result.summary
        assert "1 个文件" in result.summary

    async def test_apply_deletes_only_orphans(self, tmp_path, session_factory):
        storage = FileStorage(str(tmp_path))
        live = _write(tmp_path / "0000" / "在库_222.txt")
        _seed_row(session_factory, 222, live)
        orphan = _aged(_write(tmp_path / "0000" / "已删_111.txt"))

        await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert not orphan.exists(), "孤儿应被删除"
        assert live.is_file(), "在库的书一个字节都不能动"

    async def test_fresh_files_are_kept(self, tmp_path, session_factory):
        """A body is written *before* its row (plan → download → persist).

        Reclaiming a file that appeared seconds ago would delete a download
        that is still in flight, so anything inside ``min_age_days`` is skipped
        even when no row points at it yet.
        """
        storage = FileStorage(str(tmp_path))
        fresh = _write(tmp_path / "0000" / "在途_333.txt")

        result = await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert fresh.is_file()
        assert "没有可回收" in result.summary

    async def test_min_age_days_can_be_zero(self, tmp_path, session_factory):
        storage = FileStorage(str(tmp_path))
        fresh = _write(tmp_path / "0000" / "在途_333.txt")

        await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True, min_age_days=0),
            _ctx(session_factory, storage),
        )

        assert not fresh.exists()

    async def test_unmanaged_filenames_are_never_touched(
        self, tmp_path, session_factory,
    ):
        """Only managed names are candidates; a stray note is not ours.

        ``_id_of`` accepts ``{title}_{id}`` (novels) and ``{id}_u_{n}`` /
        ``{id}_c_cover`` (assets).  Anything else — ``NOTES.md``, a manual
        ``backup.txt`` — parses as no id and must be left alone even if it is
        ancient and has no row.
        """
        storage = FileStorage(str(tmp_path))
        notes = tmp_path / "NOTES.md"
        notes.write_text("operator notes", encoding="utf-8")
        _aged(notes)

        result = await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert notes.is_file()
        assert "没有可回收" in result.summary

    async def test_tmp_files_are_skipped(self, tmp_path, session_factory):
        """``.tmp`` belongs to an in-flight atomic write."""
        storage = FileStorage(str(tmp_path))
        tmp = _aged(_write(tmp_path / "0000" / "写作中_444.txt.tmp"))

        await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert tmp.is_file()

    async def test_epub_orphan_is_reclaimed_with_its_text(
        self, tmp_path, session_factory,
    ):
        storage = FileStorage(str(tmp_path))
        txt = _aged(_write(tmp_path / "0000" / "已删_555.txt"))
        epub = _aged(_write(tmp_path / "0000" / "已删_555.epub"))

        await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert not txt.exists() and not epub.exists()

    async def test_local_book_negative_id_is_live_when_row_exists(
        self, tmp_path, session_factory,
    ):
        """Local books use negative ids (``id = -(upload coordinate)``).

        A naive ``novel_id > 0`` guard would classify every uploaded book as an
        orphan and delete the user's own library.
        """
        storage = FileStorage(str(tmp_path))
        local_dir = tmp_path / "local"
        live = _write(local_dir / "我的书_-12345.txt")
        _seed_row(session_factory, -12345, live)

        await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert live.is_file(), "本地书有行，绝不是孤儿"

    async def test_reports_total_bytes(self, tmp_path, session_factory):
        storage = FileStorage(str(tmp_path))
        big = _aged(_write(tmp_path / "0000" / "大书_777.txt", body="x" * 2048))
        assert big.stat().st_size == 2048

        result = await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True), _ctx(session_factory, storage),
        )

        assert "释放" in result.summary and "MB" in result.summary

    async def test_missing_root_is_reported_not_crashed(
        self, tmp_path, session_factory,
    ):
        storage = FileStorage(str(tmp_path / "does-not-exist"))

        result = await prune_orphan_files(
            PruneOrphanFilesArgs(), _ctx(session_factory, storage),
        )

        assert "下载目录不存在" in result.summary

    async def test_no_file_storage_skips(self, session_factory):
        ctx = TaskContext(uow=SqlUnitOfWork(session_factory))
        result = await prune_orphan_files(PruneOrphanFilesArgs(), ctx)
        assert "未配置文件存储" in result.summary


def _write(path: Path, body: str = "正文") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestExportArchivesAreNotNovels:
    """``batch_export_<task_id>.zip`` sits in the download tree but is not a novel.

    It is a finished export archive that ``tasks/batch.py`` sweeps itself after
    24 h; the number in its name is a *task* id.  Treating it as ``{title}_{id}``
    would make this task delete an export the user may be downloading right
    now — on production the two live archives alone are 594 MB, dwarfing the
    133 MB of genuine orphan text.
    """

    async def test_export_archive_is_never_reclaimed(self, tmp_path, session_factory):
        storage = FileStorage(str(tmp_path))
        export = _aged(_write(tmp_path / "batch_export_1256.zip", body="PK" * 999))

        # Even with a row that happens to carry the same number as the task id,
        # the archive must not be considered: it is not a novel artifact.
        _seed_row(session_factory, 1256, tmp_path / "0000" / "别的书_1256.txt")

        result = await prune_orphan_files(
            PruneOrphanFilesArgs(apply=True, min_age_days=0),
            _ctx(session_factory, storage),
        )

        assert export.is_file(), "导出包归 batch.py 管，孤儿清理不得插手"
        assert "没有可回收" in result.summary

    async def test_export_archive_does_not_mask_its_task_id_row(
        self, tmp_path, session_factory,
    ):
        """The task id in the archive name must not be read as a novel id.

        If it were, a *live* novel whose id equals that task id would look
        "present" and hide a genuine orphan classification.
        """
        from copixiv.tasks.maintenance.files import _id_of

        assert _id_of("batch_export_1256") is None
        # A real novel name still parses, both shapes.
        assert _id_of("某书_12345") == 12345
        assert _id_of("12345_u_678") == 12345
