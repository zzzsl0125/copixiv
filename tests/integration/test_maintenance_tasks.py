"""Layer 3 integration tests: maintenance tasks (check_epub, fix_series_index, prune_orphan_files)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.factories import make_local_novel, write_epub_zip

from copixiv.core.draft import build_novel
from copixiv.core.models import EpubStatus
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.use_cases.discovery import fetch_author_novels
from copixiv.pixiv.errors import PixivApiError
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.executor import TaskContext
from copixiv.tasks.maintenance.epub import check_epub
from copixiv.tasks.maintenance.files import PruneOrphanFilesArgs, prune_orphan_files
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
        uow.session.add_all(
            [
                models.FailedNovel(
                    source="pixiv",
                    source_key="101",
                    novel_id=101,
                    error_message="stale img err",
                    failed_times=2,
                ),
                models.FailedNovel(
                    source="pixiv",
                    source_key="102",
                    novel_id=102,
                    error_message="reverted err",
                    failed_times=1,
                ),
                models.FailedNovel(
                    source="pixiv",
                    source_key="103",
                    novel_id=103,
                    error_message="resolved img err",
                    failed_times=1,
                ),
            ]
        )

    await check_epub(TaskContext(uow=uow))

    from sqlalchemy import select

    def _get_failed(session, nid: int):
        return session.execute(
            select(models.FailedNovel).where(
                models.FailedNovel.source == "pixiv",
                models.FailedNovel.source_key == str(nid),
            )
        ).scalar_one_or_none()

    with uow.begin():
        assert uow.session.get(models.Novel, 101).has_epub == EpubStatus.NO_IMAGES
        assert uow.session.get(models.Novel, 102).has_epub == EpubStatus.PENDING
        assert uow.session.get(models.Novel, 103).has_epub == EpubStatus.DONE
        assert uow.session.get(models.Novel, -50).has_epub == EpubStatus.MISSING_SOURCE
        assert _get_failed(uow.session, 101) is None
        assert _get_failed(uow.session, 102) is not None
        assert _get_failed(uow.session, 103) is None


def test_fix_series_index_args_accepts_legacy_extra_fields():
    from copixiv.tasks import registry

    legacy = FixSeriesIndexArgs.model_validate(
        {"max_sweep": "", "sweep_all": False, "max_series": "200", "max_orphans": "200"}
    )
    assert legacy.max_series == 200
    assert FixSeriesIndexArgs().max_series == 200
    assert FixSeriesIndexArgs.model_validate({"max_series": ""}).max_series is None
    spec = registry.get_spec("fix_series_index")
    assert spec is not None
    assert spec.notify is False


@pytest.mark.asyncio
async def test_fix_series_index_single_multi_and_dead_letter(
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
    fake_pixiv_client.errors[("novel_series", 40)] = PixivApiError("gone", status_code=404)

    ctx = TaskContext(uow=uow, client=fake_pixiv_client)
    await fix_series_index(FixSeriesIndexArgs(), ctx)

    with uow.begin():
        assert uow.session.get(models.Novel, 1).series_index == 1
        assert uow.session.get(models.Novel, 202).series_index == 1
        assert uow.session.get(models.Novel, 201).series_index == 2
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
    expired_export = tmp_path / "batch_export_99.zip"
    expired_export.write_bytes(b"zip")
    fresh_export = tmp_path / "batch_export_100.zip"
    fresh_export.write_bytes(b"zip-new")
    kept_fresh = dl / "fresh_999.txt"
    kept_fresh.write_text("too new", encoding="utf-8")
    old_orphan = dl / "orphan_888.txt"
    old_orphan.write_text("stale orphan", encoding="utf-8")
    stale_renamed = dl / "old_title_10.txt"
    stale_renamed.write_text("stale renamed copy", encoding="utf-8")
    unmanaged = dl / "notes.txt"
    unmanaged.write_text("user notes", encoding="utf-8")

    old_ts = time.time() - 10 * 86400
    for p in (kept_db, expired_export, old_orphan, stale_renamed, unmanaged):
        os.utime(p, (old_ts, old_ts))

    with uow.begin():
        persist_novels(uow, [build_novel(id=10, title="Keep", author_id=1, author_name="A")])
        uow.session.get(models.Novel, 10).path = str(kept_db)

    ctx = TaskContext(uow=uow, file_storage=FileStorage(str(tmp_path)))

    # Dry-run keeps old_orphan and expired_export
    await prune_orphan_files(PruneOrphanFilesArgs(apply=False, min_age_days=7), ctx)
    assert old_orphan.exists()
    assert expired_export.exists()
    assert stale_renamed.exists()

    # Apply deletes orphans + stale unreferenced copy of known id, keeps DB/fresh/unmanaged files
    await prune_orphan_files(PruneOrphanFilesArgs(apply=True, min_age_days=7), ctx)
    assert not old_orphan.exists()
    assert not expired_export.exists()
    assert not stale_renamed.exists()
    assert kept_db.exists()
    assert fresh_export.exists()
    assert kept_fresh.exists()
    assert unmanaged.exists()


@pytest.mark.asyncio
async def test_prune_orphan_files_protects_legacy_paths_and_handles_negative_local_ids(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    dl = tmp_path / "0000"
    local_dir = tmp_path / "local"
    dl.mkdir(parents=True)
    local_dir.mkdir(parents=True)

    # Legacy-named novel (id=36, filename ends with '_1' which is not in DB) + sibling .epub
    legacy_txt = dl / "36.凤羽的夏天_1.txt"
    legacy_txt.write_text("legacy text", encoding="utf-8")
    legacy_epub = dl / "36.凤羽的夏天_1.epub"
    legacy_epub.write_bytes(b"legacy epub")

    # Negative local ID in DB + sibling .epub
    local_kept_txt = local_dir / "本地小说_-50.txt"
    local_kept_txt.write_text("local kept", encoding="utf-8")
    local_kept_epub = local_dir / "本地小说_-50.epub"
    local_kept_epub.write_bytes(b"local epub")

    # Orphan negative local ID not in DB
    local_orphan = local_dir / "orphan_local_-999.txt"
    local_orphan.write_text("local orphan", encoding="utf-8")

    old_ts = time.time() - 10 * 86400
    for p in (legacy_txt, legacy_epub, local_kept_txt, local_kept_epub, local_orphan):
        os.utime(p, (old_ts, old_ts))

    with uow.begin():
        persist_novels(uow, [build_novel(id=36, title="凤羽的夏天", author_id=1, author_name="A")])
        uow.session.get(models.Novel, 36).path = str(legacy_txt)
        uow.session.add(
            make_local_novel(
                -50,
                title="本地小说",
                author_id=1,
                author_name="A",
                path=str(local_kept_txt),
                text=10,
                source_key="sha_local_kept",
            )
        )

    ctx = TaskContext(uow=uow, file_storage=FileStorage(str(tmp_path)))
    await prune_orphan_files(PruneOrphanFilesArgs(apply=True, min_age_days=7), ctx)

    assert legacy_txt.exists()
    assert legacy_epub.exists()
    assert local_kept_txt.exists()
    assert local_kept_epub.exists()
    assert not local_orphan.exists()


@pytest.mark.asyncio
async def test_prune_orphan_files_cleans_empty_dirs_while_protecting_sources_and_fresh_dirs(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    from copixiv.sources import LocalSourcePlugin, PixivSourcePlugin, SourceRegistry
    from copixiv.sources.esj import EsjSourcePlugin

    pixiv_dir = tmp_path / "pixiv"
    pixiv_old_series = pixiv_dir / "old_series_1"
    esj_dir = tmp_path / "esj"
    esj_old_empty = esj_dir / "old_empty_book"
    esj_fresh_empty = esj_dir / "fresh_empty_book"
    local_dir = tmp_path / "local"
    legacy_dir = tmp_path / "0000"
    legacy_orphan_sub = legacy_dir / "sub_orphan"
    legacy_kept_sub = legacy_dir / "sub_kept"

    for d in (
        pixiv_old_series,
        esj_old_empty,
        esj_fresh_empty,
        local_dir,
        legacy_orphan_sub,
        legacy_kept_sub,
    ):
        d.mkdir(parents=True)

    old_orphan_1 = pixiv_old_series / "orphan_100.txt"
    old_orphan_1.write_text("orphan 1", encoding="utf-8")
    old_orphan_2 = legacy_orphan_sub / "orphan_200.txt"
    old_orphan_2.write_text("orphan 2", encoding="utf-8")
    unmanaged = legacy_kept_sub / "notes.txt"
    unmanaged.write_text("keep", encoding="utf-8")

    old_ts = time.time() - 10 * 86400
    for p in (
        old_orphan_1,
        old_orphan_2,
        unmanaged,
        pixiv_old_series,
        esj_old_empty,
        local_dir,
        legacy_orphan_sub,
    ):
        os.utime(p, (old_ts, old_ts))

    registry = SourceRegistry([PixivSourcePlugin(), LocalSourcePlugin(), EsjSourcePlugin()])
    ctx = TaskContext(
        uow=uow,
        file_storage=FileStorage(str(tmp_path)),
        source_registry=registry,
    )

    dry_res = await prune_orphan_files(PruneOrphanFilesArgs(apply=False, min_age_days=7), ctx)
    assert "空目录" in dry_res.summary
    assert old_orphan_1.exists()
    assert pixiv_old_series.is_dir()
    assert esj_old_empty.is_dir()

    apply_res = await prune_orphan_files(PruneOrphanFilesArgs(apply=True, min_age_days=7), ctx)
    assert "空目录" in apply_res.summary

    assert not old_orphan_1.exists()
    assert not old_orphan_2.exists()
    assert not pixiv_old_series.exists()
    assert not esj_old_empty.exists()
    assert not legacy_orphan_sub.exists()

    # Top-level source dirs, fresh empty dirs, and dirs with unmanaged files survive
    assert pixiv_dir.is_dir()
    assert esj_dir.is_dir()
    assert esj_fresh_empty.is_dir()
    assert local_dir.is_dir()
    assert legacy_dir.is_dir()
    assert legacy_kept_sub.is_dir()
    assert unmanaged.is_file()


@pytest.mark.asyncio
async def test_fix_series_index_skips_roster_absent_series_and_respects_max_series(
    uow: SqlUnitOfWork,
    fake_pixiv_client,
):
    with uow.begin():
        persist_novels(
            uow,
            [
                # Partially cataloged series 60: novel 601 is in roster, novel 602 is deleted (absent)
                build_novel(
                    id=601,
                    title="Present",
                    author_id=1,
                    author_name="A",
                    series_id=60,
                    series_name="S60",
                    series_index=None,
                ),
                build_novel(
                    id=602,
                    title="DeletedFromRoster",
                    author_id=1,
                    author_name="A",
                    series_id=60,
                    series_name="S60",
                    series_index=None,
                ),
                # Second series 70 -> deferred when max_series=1, completed on round 2
                build_novel(
                    id=701,
                    title="S70-1",
                    author_id=1,
                    author_name="A",
                    series_id=70,
                    series_name="S70",
                    series_index=None,
                ),
                build_novel(
                    id=702,
                    title="S70-2",
                    author_id=1,
                    author_name="A",
                    series_id=70,
                    series_name="S70",
                    series_index=None,
                ),
            ],
        )

    fake_pixiv_client.series_responses[60] = {"novels": [SimpleNamespace(id=601)]}
    fake_pixiv_client.series_responses[70] = {"novels": [SimpleNamespace(id=701), SimpleNamespace(id=702)]}

    ctx = TaskContext(uow=uow, client=fake_pixiv_client)
    res1 = await fix_series_index(FixSeriesIndexArgs(max_series=1), ctx)
    assert "本轮达上限" in res1.summary

    with uow.begin():
        assert uow.session.get(models.Novel, 601).series_index == 1
        assert uow.session.get(models.Novel, 602).series_index is None
        assert uow.session.get(models.Novel, 701).series_index is None
        assert uow.session.get(models.Novel, 702).series_index is None
        assert ("novel_series", (70, True)) not in fake_pixiv_client.calls

    # Round 2: series 60 is retired (not requested again); series 70 is repaired
    fake_pixiv_client.calls.clear()
    res2 = await fix_series_index(FixSeriesIndexArgs(max_series=1), ctx)
    assert "本轮达上限" not in res2.summary
    assert ("novel_series", (60, True)) not in fake_pixiv_client.calls
    assert ("novel_series", (70, True)) in fake_pixiv_client.calls
    with uow.begin():
        assert uow.session.get(models.Novel, 701).series_index == 1
        assert uow.session.get(models.Novel, 702).series_index == 2


@pytest.mark.asyncio
async def test_fetch_author_novels_skips_updated_today_and_processes_stale(
    uow: SqlUnitOfWork,
    fake_pixiv_client,
    tmp_path: Path,
):
    now = datetime.now(UTC)
    with uow.begin():
        uow.session.add_all(
            [
                models.Author(author_id=101, author_name="FreshAuthor", last_update=now, source_key="101"),
                models.Author(
                    author_id=102,
                    author_name="StaleAuthor",
                    last_update=now - timedelta(days=2),
                    source_key="102",
                ),
            ]
        )

    storage = FileStorage(tmp_path)
    fake_pixiv_client.user_novels_responses[102] = {"novels": []}

    # Author updated today -> skipped when force=False
    res_fresh = await fetch_author_novels(
        101,
        force=False,
        uow=uow,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert "跳过" in res_fresh.summary
    assert ("user_novels", (101, True)) not in fake_pixiv_client.calls

    # Author not updated today -> processed when force=False
    res_stale = await fetch_author_novels(
        102,
        force=False,
        uow=uow,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert "跳过" not in res_stale.summary
    assert ("user_novels", (102, True)) in fake_pixiv_client.calls
    assert ("user_detail", 102) not in fake_pixiv_client.calls

    # force=True ignores was_updated_today AND forces redownload of existing novels
    from copixiv.features.novels.use_cases.discovery import fan_out_author_fetch

    with uow.begin():
        persist_novels(
            uow,
            [build_novel(id=1011, title="中文已有", author_id=101, author_name="FreshAuthor")],
        )

    fake_pixiv_client.calls.clear()
    fake_pixiv_client.user_novels_responses[101] = {
        "novels": [
            SimpleNamespace(
                id=1011,
                title="中文已有",
                caption="中文",
                tags=[SimpleNamespace(name="中文")],
                user=SimpleNamespace(id=101, name="FreshAuthor"),
                series=None,
                total_bookmarks=10,
                total_view=20,
                text_length=4,
                create_date="2026-01-01T00:00:00+09:00",
            )
        ]
    }
    fake_pixiv_client.webview_responses[1011] = SimpleNamespace(
        id=1011,
        title="中文已有",
        user_id=101,
        rating={"bookmark": 10, "view": 20},
        caption="中文",
        series_id=None,
        series_title=None,
        series_navigation=None,
        cdate="2026-01-01T00:00:00+09:00",
        tags=["中文"],
        text="重抓正文",
        images=[],
        illusts=[],
        cover_url=None,
    )
    seen: set[int] = set()
    await fan_out_author_fetch(
        [fake_pixiv_client.user_novels_responses[101]["novels"][0]],
        force=True,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
        child_uow_factory=lambda: SqlUnitOfWork(uow.session_factory),
        seen_authors=seen,
    )
    assert ("user_novels", (101, True)) in fake_pixiv_client.calls
    assert ("webview_novel", 1011) in fake_pixiv_client.calls
    assert seen == {101}

    # Same author on a second ranking/search slice in the same run is skipped
    fake_pixiv_client.calls.clear()
    await fan_out_author_fetch(
        [fake_pixiv_client.user_novels_responses[101]["novels"][0]],
        force=True,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
        child_uow_factory=lambda: SqlUnitOfWork(uow.session_factory),
        seen_authors=seen,
    )
    assert fake_pixiv_client.calls == []

    # 23-hour cooldown: 23h05m ago is eligible (avoids daily cron drift); 22h ago is skipped
    with uow.begin():
        uow.session.add_all(
            [
                models.Author(
                    author_id=103,
                    author_name="DriftOk",
                    last_update=now - timedelta(hours=23, minutes=5),
                    source_key="103",
                ),
                models.Author(
                    author_id=104,
                    author_name="Cooldown",
                    last_update=now - timedelta(hours=22),
                    source_key="104",
                ),
            ]
        )
    fake_pixiv_client.user_novels_responses[103] = {"novels": []}
    fake_pixiv_client.calls.clear()
    res_drift = await fetch_author_novels(
        103,
        force=False,
        uow=uow,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert "跳过" not in res_drift.summary
    assert ("user_novels", (103, True)) in fake_pixiv_client.calls

    fake_pixiv_client.calls.clear()
    res_cool = await fetch_author_novels(
        104,
        force=False,
        uow=uow,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert "跳过" in res_cool.summary
    assert fake_pixiv_client.calls == []

    # Brand-new author with only non-Chinese works still gets a placeholder row + last_update
    fake_pixiv_client.user_novels_responses[105] = {
        "user": {"id": 105, "name": "JpOnly"},
        "novels": [
            SimpleNamespace(
                id=99001,
                title="これは日本語の小説です",
                caption="日本語",
                tags=[],
                user=SimpleNamespace(id=105, name="JpOnly"),
            )
        ],
    }
    fake_pixiv_client.calls.clear()
    await fetch_author_novels(
        105,
        force=False,
        uow=uow,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert ("user_follow_add", 105) in fake_pixiv_client.calls
    fake_pixiv_client.calls.clear()
    res_jp_repeat = await fetch_author_novels(
        105,
        force=False,
        uow=uow,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert "跳过" in res_jp_repeat.summary
    assert fake_pixiv_client.calls == []


def test_batch_export_preview_uses_highest_id_limit_1(
    db_session_factory,
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    from fastapi.testclient import TestClient
    from tests.factories import make_test_app

    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(id=10, title="Ten", author_id=1, author_name="Auth"),
                build_novel(id=50, title="Fifty", author_id=1, author_name="Auth"),
                build_novel(id=30, title="Thirty", author_id=1, author_name="Auth"),
            ],
        )

    app = make_test_app(db_session_factory, tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        ok = client.post(
            "/api/novels/batch-export/preview",
            json={"novel_ids": [10, 50, 30], "naming_template": "{title}_{id}"},
        )
        assert ok.status_code == 200
        assert ok.json() == {"path": "Fifty_50.txt"}

        missing = client.post(
            "/api/novels/batch-export/preview",
            json={"novel_ids": [9999], "naming_template": "{title}_{id}"},
        )
        assert missing.status_code == 200
        assert missing.json() == {"path": None}

        empty = client.post(
            "/api/novels/batch-export/preview",
            json={"novel_ids": []},
        )
        assert empty.status_code == 200
        assert empty.json() == {"path": None}

        invalid_tpl = client.post(
            "/api/novels/batch-export/preview",
            json={"novel_ids": [10], "naming_template": "{title}"},
        )
        assert invalid_tpl.status_code == 400


@pytest.mark.asyncio
async def test_retry_failed_chunk_batches(
    uow: SqlUnitOfWork,
    fake_pixiv_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from copixiv.features.novels.ingest import IngestOutcome
    from copixiv.features.novels.use_cases import discovery as discovery_mod

    storage = FileStorage(str(tmp_path))
    retry_calls: list[list[int]] = []

    async def _fake_retry_ingest(*, ids, force, **_kwargs):
        retry_calls.append(list(ids))
        assert force is True
        return IngestOutcome(new_titles=[f"N{i}" for i in ids])

    monkeypatch.setattr(discovery_mod, "ingest", _fake_retry_ingest)
    res_retry = await discovery_mod.retry_failed_novels(
        list(range(1, 61)),
        session_factory=uow.session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=None,
    )
    assert [len(c) for c in retry_calls] == [50, 10]
    assert "成功 60/60" in res_retry.summary
