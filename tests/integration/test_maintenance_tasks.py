"""Layer 3 integration tests: maintenance tasks (check_epub, fix_series_index, prune_orphan_files, prune_task_history)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.factories import make_local_novel, write_epub_zip

from copixiv.core.draft import build_novel
from copixiv.core.models import EpubStatus
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.pixiv.errors import PixivApiError
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.maintenance.epub import check_epub
from copixiv.tasks.maintenance.files import PruneOrphanFilesArgs, prune_orphan_files
from copixiv.tasks.maintenance.history import PruneTaskHistoryArgs, prune_task_history
from copixiv.tasks.maintenance.series import FixSeriesIndexArgs, fix_series_index


@pytest.mark.asyncio
async def test_check_epub_covers_positive_and_negative_ids(uow: SqlUnitOfWork, tmp_path: Path):
    # 1. Positive ID: PENDING with plain text -> NO_IMAGES(3)
    txt_noimg = tmp_path / "plain_101.txt"
    txt_noimg.write_text("纯文本无图", encoding="utf-8")

    # 2. Positive ID: DONE with EPUB carrying raw marker -> reverted to PENDING(1)
    txt_raw = tmp_path / "raw_102.txt"
    txt_raw.write_text("正文 [pixivimage:999]", encoding="utf-8")
    write_epub_zip(
        txt_raw.with_suffix(".epub"),
        xhtml_body="<p>[pixivimage:999]</p>",
        with_image=True,
    )

    # 3. Positive ID: PENDING with complete EPUB -> DONE(2)
    txt_ready = tmp_path / "ready_103.txt"
    txt_ready.write_text("正文 [uploadedimage:1]", encoding="utf-8")
    write_epub_zip(
        txt_ready.with_suffix(".epub"),
        xhtml_body='<p><img src="images/1.jpg"/></p>',
        with_image=True,
    )

    # 4. Negative local ID: missing source txt -> MISSING_SOURCE(4)
    neg_missing = tmp_path / "local_missing_-50.txt"

    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(id=101, title="P101", author_id=1, author_name="A", has_epub=EpubStatus.PENDING),
                build_novel(id=102, title="P102", author_id=1, author_name="A", has_epub=EpubStatus.DONE),
                build_novel(id=103, title="P103", author_id=1, author_name="A", has_epub=EpubStatus.PENDING),
            ],
        )
        uow.session.get(models.Novel, 101).path = str(txt_noimg)
        uow.session.get(models.Novel, 102).path = str(txt_raw)
        uow.session.get(models.Novel, 103).path = str(txt_ready)
        uow.session.add(
            make_local_novel(
                -50,
                title="LocalMissing",
                author_id=1,
                author_name="A",
                path=str(neg_missing),
                text=10,
                has_epub=EpubStatus.NO_IMAGES,
                source_key="sha_missing_local",
            )
        )

    await check_epub(TaskContext(uow=uow))

    with uow.begin():
        assert uow.session.get(models.Novel, 101).has_epub == EpubStatus.NO_IMAGES
        assert uow.session.get(models.Novel, 102).has_epub == EpubStatus.PENDING
        assert uow.session.get(models.Novel, 103).has_epub == EpubStatus.DONE
        assert uow.session.get(models.Novel, -50).has_epub == EpubStatus.MISSING_SOURCE


@pytest.mark.asyncio
async def test_fix_series_index_single_multi_orphan_and_dead_letter(
    uow: SqlUnitOfWork,
    fake_pixiv_client,
):
    with uow.begin():
        persist_novels(
            uow,
            [
                # Single-book series 10 -> filled to 1 locally without Pixiv call
                build_novel(
                    id=1, title="Solo", author_id=1, author_name="A", series_id=10, series_name="S10"
                ),
                # Multi-book series 20 -> roster sets order [202 -> 1, 201 -> 2]
                build_novel(
                    id=201,
                    title="M2",
                    author_id=1,
                    author_name="A",
                    series_id=20,
                    series_name="S20",
                    series_index=1,
                ),
                build_novel(
                    id=202,
                    title="M1",
                    author_id=1,
                    author_name="A",
                    series_id=20,
                    series_name="S20",
                    series_index=None,
                ),
                # Orphan row: series_id=None, series_index=5 -> webview has no series -> cleared
                build_novel(
                    id=301,
                    title="Orphan",
                    author_id=1,
                    author_name="A",
                    series_id=None,
                    series_name="Old",
                    series_index=5,
                ),
                # Dead series 40 -> 3 consecutive 404s retire it
                build_novel(
                    id=401, title="Dead1", author_id=1, author_name="A", series_id=40, series_name="S40"
                ),
                build_novel(
                    id=402, title="Dead2", author_id=1, author_name="A", series_id=40, series_name="S40"
                ),
            ],
        )

    fake_pixiv_client.series_responses[20] = {"novels": [SimpleNamespace(id=202), SimpleNamespace(id=201)]}
    fake_pixiv_client.webview_responses[301] = {"id": 301, "series_id": None, "user_id": 1}
    fake_pixiv_client.errors[("novel_series", 40)] = PixivApiError("gone", status_code=404)

    ctx = TaskContext(uow=uow, client=fake_pixiv_client)
    await fix_series_index(FixSeriesIndexArgs(), ctx)

    with uow.begin():
        assert uow.session.get(models.Novel, 1).series_index == 1
        assert uow.session.get(models.Novel, 202).series_index == 1
        assert uow.session.get(models.Novel, 201).series_index == 2
        assert uow.session.get(models.Novel, 301).series_index is None
        ledger = uow.session.get(models.SeriesDeadLetter, 40)
        assert ledger is not None
        assert ledger.consecutive_failures == 1
        assert ledger.retired_at is None

    # Rounds 2 and 3 retire series 40
    await fix_series_index(FixSeriesIndexArgs(), ctx)
    await fix_series_index(FixSeriesIndexArgs(), ctx)
    with uow.begin():
        ledger = uow.session.get(models.SeriesDeadLetter, 40)
        assert ledger is not None
        assert ledger.consecutive_failures == 3
        assert ledger.retired_at is not None

    # Round 4 must NOT request series 40 again
    fake_pixiv_client.calls.clear()
    await fix_series_index(FixSeriesIndexArgs(), ctx)
    assert ("novel_series", (40, True)) not in fake_pixiv_client.calls


@pytest.mark.asyncio
async def test_prune_orphan_files_respects_db_exports_age_and_dry_run(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    dl = tmp_path / "0000"
    dl.mkdir(parents=True)
    kept_db = dl / "keep_10.txt"
    kept_db.write_text("in db", encoding="utf-8")
    kept_export = tmp_path / "batch_export_99.zip"
    kept_export.write_bytes(b"zip")
    kept_fresh = dl / "fresh_999.txt"
    kept_fresh.write_text("too new", encoding="utf-8")
    old_orphan = dl / "orphan_888.txt"
    old_orphan.write_text("stale orphan", encoding="utf-8")

    old_ts = time.time() - 10 * 86400
    for p in (kept_db, kept_export, old_orphan):
        os.utime(p, (old_ts, old_ts))

    with uow.begin():
        persist_novels(uow, [build_novel(id=10, title="Keep", author_id=1, author_name="A")])
        uow.session.get(models.Novel, 10).path = str(kept_db)

    ctx = TaskContext(uow=uow, file_storage=FileStorage(str(tmp_path)))

    # Dry-run keeps old_orphan
    await prune_orphan_files(PruneOrphanFilesArgs(apply=False, min_age_days=7), ctx)
    assert old_orphan.exists()

    # Apply deletes only old_orphan
    await prune_orphan_files(PruneOrphanFilesArgs(apply=True, min_age_days=7), ctx)
    assert not old_orphan.exists()
    assert kept_db.exists()
    assert kept_export.exists()
    assert kept_fresh.exists()


@pytest.mark.asyncio
async def test_prune_task_history_keep_zero_and_keep_n_protect_active(uow: SqlUnitOfWork):
    now = datetime.now(UTC)
    old_time = now - timedelta(days=200)
    recent_time = now - timedelta(days=5)

    with uow.begin():
        for idx, (status, st_time) in enumerate(
            [
                ("completed", old_time),
                ("failed", old_time),
                ("completed", old_time),
                ("completed", recent_time),
                ("pending", old_time),
                ("running", old_time),
            ],
            start=1,
        ):
            uow.session.add(
                models.TaskHistory(
                    name=f"t{idx}",
                    task_func="check_epub",
                    status=status,
                    start_time=st_time,
                )
            )

    ctx = TaskContext(uow=uow)

    # keep=4: table has 6 rows -> newest 4 (ids >= 3rd from bottom) survive, old id=1,2 deleted
    await prune_task_history(PruneTaskHistoryArgs(apply=True, keep=4, max_age_days=30), ctx)
    with uow.begin():
        remaining = list(
            uow.session.execute(select(models.TaskHistory.name).order_by(models.TaskHistory.id))
            .scalars()
            .all()
        )
        assert remaining == ["t3", "t4", "t5", "t6"]

    # keep=0: removes remaining old terminal row t3, protects recent t4 and active t5(pending)/t6(running)
    await prune_task_history(PruneTaskHistoryArgs(apply=True, keep=0, max_age_days=30), ctx)
    with uow.begin():
        remaining = list(
            uow.session.execute(select(models.TaskHistory.name).order_by(models.TaskHistory.id))
            .scalars()
            .all()
        )
        assert remaining == ["t4", "t5", "t6"]
