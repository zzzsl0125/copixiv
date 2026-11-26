"""Layer 2 DB tests: Pixiv novel ingest pipeline, metadata edit policy, and batch operations."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.factories import make_local_novel

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import EpubStatus
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.field_policy import editable_fields
from copixiv.features.novels.ingest import ingest
from copixiv.features.novels.use_cases.batch_operations import (
    batch_delete,
    batch_tag,
)
from copixiv.features.novels.use_cases.update_metadata import (
    update_novel_metadata,
)
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.batch import BatchExportArgs, batch_export
from copixiv.tasks.executor import TaskContext


def _fake_webview(
    novel_id: int,
    *,
    title: str,
    author_id: int = 10,
    text: str = "这是中文正文内容",
    series_id: int | None = None,
    series_title: str | None = None,
):
    return SimpleNamespace(
        id=novel_id,
        title=title,
        user_id=author_id,
        rating={"bookmark": 120, "view": 500},
        caption="中文简介",
        series_id=series_id,
        series_title=series_title,
        series_navigation=None,
        cdate="2026-01-02T03:04:05+09:00",
        tags=["中文", "科幻"],
        text=text,
        images=[],
        illusts=[],
        cover_url=None,
    )


def _fake_novel_info(
    novel_id: int,
    *,
    title: str,
    author_id: int = 10,
    author_name: str = "Auth10",
    series=None,
):
    return SimpleNamespace(
        id=novel_id,
        title=title,
        caption="中文简介",
        tags=[SimpleNamespace(name="中文"), SimpleNamespace(name="日常")],
        user=SimpleNamespace(id=author_id, name=author_name),
        series=series,
        total_bookmarks=200,
        total_view=1000,
        text_length=len("这是中文正文内容"),
        create_date="2026-01-02T03:04:05+09:00",
    )


class _DummyAssetBatch:
    async def await_all(self) -> list[tuple[int, str]]:
        return []

    def drain_outcomes(self) -> dict[int, str]:
        return {}


class _DummyImageDownloader:
    def __init__(self) -> None:
        self.processed: list[int] = []

    def new_batch(self) -> _DummyAssetBatch:
        return _DummyAssetBatch()

    async def process_novel_assets(self, data, *, force: bool = False, batch=None) -> None:
        del force, batch
        self.processed.append(data.id)


@pytest.mark.asyncio
async def test_ingest_persists_new_updates_existing_metadata_and_records_failures(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    downloader = _DummyImageDownloader()
    fake_pixiv_client.webview_responses[101] = _fake_webview(101, title="Novel 101")
    fake_pixiv_client.user_detail_responses[10] = {"user": {"id": 10, "name": "AuthorTen"}}
    # Novel 102 returns None -> recorded in failed_novel
    fake_pixiv_client.webview_responses[102] = None

    outcome = await ingest(
        ids=[101, 102],
        session_factory=committed_session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=downloader,
    )
    assert outcome.new_count == 1
    assert outcome.new_titles == ["Novel 101"]
    assert outcome.failed_ids == [102]
    assert downloader.processed == [101]

    with committed_session_factory() as session:
        n101 = session.get(models.Novel, 101)
        assert n101 is not None
        assert n101.author_name == "AuthorTen"
        assert n101.has_epub == EpubStatus.NO_IMAGES
        assert Path(n101.path).is_file()
        author10 = session.get(models.Author, 10)
        assert author10 is not None
        assert author10.author_name == "AuthorTen"
        failed102 = session.execute(
            select(models.FailedNovel).where(
                models.FailedNovel.source == "pixiv",
                models.FailedNovel.source_key == "102",
            )
        ).scalar_one_or_none()
        assert failed102 is not None
        assert failed102.novel_id == 102

        # Mark 101 DONE manually; a metadata-only ingest via novelInfo must preserve DONE
        n101.has_epub = EpubStatus.DONE
        session.commit()

    info101 = _fake_novel_info(101, title="Novel 101 Updated")
    outcome2 = await ingest(
        [info101],
        session_factory=committed_session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=downloader,
    )
    assert outcome2.new_count == 0
    with committed_session_factory() as session:
        n101_after = session.get(models.Novel, 101)
        assert n101_after is not None
        assert n101_after.title == "Novel 101 Updated"
        assert n101_after.has_epub == EpubStatus.DONE


@pytest.mark.asyncio
async def test_update_novel_metadata_policy_and_unlink_semantics(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
):
    assert "title" in editable_fields("local")
    assert "title" not in editable_fields("pixiv")

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add(models.Author(author_id=77, author_name="LocalAuth", source_key="77"))
        uow.session.add(models.Series(series_id=88, series_name="LocalSeries", author_id=77, source_key="88"))
        uow.session.add(
            models.Novel(
                id=501,
                title="PixivBook",
                author_id=77,
                author_name="LocalAuth",
                path=str(tmp_path / "501.txt"),
                source="pixiv",
                source_key="501",
                has_epub=EpubStatus.NO_IMAGES,
            )
        )
        uow.session.add(
            make_local_novel(
                -501,
                title="LocalOrig",
                author_id=77,
                author_name="LocalAuth",
                path=str(tmp_path / "-501.txt"),
            )
        )

    with pytest.raises(ValidationError, match="没有要更新的字段"):
        await update_novel_metadata(-501, {}, uow=uow, client=fake_pixiv_client)
    with pytest.raises(ValidationError, match="章节号必须是正整数"):
        await update_novel_metadata(501, {"series_index": 0}, uow=uow, client=fake_pixiv_client)
    with pytest.raises(ValidationError, match="仅本地书可编辑"):
        await update_novel_metadata(501, {"title": "CannotChange"}, uow=uow, client=fake_pixiv_client)

    # Local novel can attach author/series, then unlink them with asymmetric semantics
    updated = await update_novel_metadata(
        -501,
        {
            "title": "LocalRenamed",
            "author_id": 77,
            "series_id": 88,
            "series_index": 3,
            "tags": ["TagA", "TagB/TagC"],
            "create_time": "2026-10-19",
        },
        uow=uow,
        client=fake_pixiv_client,
    )
    from copixiv.features.novels.repo import StoredNovel

    assert isinstance(updated, StoredNovel)
    assert not hasattr(updated, "caption")
    assert not hasattr(updated, "source_key")
    assert updated.title == "LocalRenamed"
    assert updated.series_name == "LocalSeries"
    assert updated.series_index == 3
    assert set(updated.tags) == {"taga", "tagb", "tagc"}
    assert updated.create_time is not None
    assert updated.create_time.astimezone(UTC) == datetime(2026, 10, 19, 0, 0, tzinfo=UTC)

    unlinked = await update_novel_metadata(
        -501,
        {"author_id": None, "series_id": None},
        uow=uow,
        client=fake_pixiv_client,
    )
    # Unlinking author preserves display name; unlinking series clears id, name, and index
    assert unlinked.author_id is None
    assert unlinked.author_name == "LocalAuth"
    assert unlinked.series_id is None
    assert unlinked.series_name is None
    assert unlinked.series_index is None


@pytest.mark.asyncio
async def test_batch_tag_delete_and_download_by_ids(
    committed_session_factory,
    tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    p1 = tmp_path / "b1.txt"
    p2 = tmp_path / "b2.txt"
    p1.write_text("body 1", encoding="utf-8")
    p2.write_text("body 2", encoding="utf-8")

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add(models.Author(author_id=10, author_name="Auth10", source_key="10"))
        uow.session.add_all(
            [
                models.Novel(
                    id=1001,
                    title="B1",
                    author_id=10,
                    author_name="Auth10",
                    path=str(p1),
                    text=6,
                    like=5,
                    view=20,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=["old"],
                    source="pixiv",
                    source_key="1001",
                ),
                models.Novel(
                    id=1002,
                    title="B2",
                    author_id=10,
                    author_name="Auth10",
                    path=str(p2),
                    text=6,
                    like=5,
                    view=20,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=[],
                    source="pixiv",
                    source_key="1002",
                ),
            ]
        )
        uow.session.add(
            models.FailedNovel(source="pixiv", source_key="1001", novel_id=1001, error_message="err")
        )
        tag_repo = SQLAlchemyTagRepository(uow.session)
        tag_repo.create_alias({"source": "alias_src", "target": "alias_dst"})

    added = batch_tag("add_tags", [1001, 1002], ["alias_src"], uow=uow)
    assert added == 2
    removed = batch_tag("remove_tags", [1001], ["old"], uow=uow)
    assert removed == 1

    with uow.begin():
        tags_1001 = uow.session.get(models.Novel, 1001).tags
        assert "alias_dst" in tags_1001
        assert "old" not in tags_1001

    # Batch export by explicit IDs (missing IDs ignored; unknown-only fails with ValidationError)
    with uow.begin():
        task_row = models.TaskHistory(
            name="export_by_ids",
            task_func="batch_export",
            status="running",
            start_time=datetime.now(UTC),
        )
        uow.session.add(task_row)
        uow.session.flush()
        exp_task_id = task_row.id

    ctx = TaskContext(
        uow=uow,
        session_factory=committed_session_factory,
        file_storage=storage,
        task_id=exp_task_id,
    )
    export_res = await batch_export(
        BatchExportArgs(novel_ids=[1001, 1002, 999999], format_mode="txt"),
        ctx,
    )
    assert "批量导出完成：共 2/3 篇" in export_res.summary
    zip_path = tmp_path / f"batch_export_{exp_task_id}.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        assert len(zf.namelist()) == 2

    with pytest.raises(ValidationError):
        await batch_export(BatchExportArgs(novel_ids=[999999]), ctx)

    # Batch delete cleans DB rows, failed_novel ledger, and files on disk
    affected = batch_delete([1001, 1002, 999999], uow=uow, file_storage=storage)
    assert affected == 2
    assert not p1.exists()
    assert not p2.exists()
    with uow.begin():
        assert uow.session.get(models.Novel, 1001) is None
        assert uow.session.get(models.FailedNovel, 1001) is None
        author = uow.session.get(models.Author, 10)
        assert author is not None


@pytest.mark.asyncio
async def test_sync_novel_preserves_author_name_on_fetch_failure_and_clears_series_index(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
):
    from copixiv.features.novels.use_cases.sync_novel import sync_novel

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add(models.Author(author_id=20, author_name=None, source_key="20"))
        uow.session.add(models.Series(series_id=300, series_name="OldSeries", author_id=20, source_key="300"))
        uow.session.flush()
        uow.session.add(
            models.Novel(
                id=2001,
                title="OldTitle",
                author_id=20,
                author_name="KeepMe",
                series_id=300,
                series_name="OldSeries",
                series_index=5,
                path=str(tmp_path / "2001.txt"),
                source="pixiv",
                source_key="2001",
            )
        )

    fake_pixiv_client.user_detail_responses[20] = None
    # Novel 2001 was removed from series on Pixiv (series_id=None)
    fake_pixiv_client.webview_responses[2001] = _fake_webview(
        2001,
        title="NewTitle",
        author_id=20,
    )

    updated = await sync_novel(
        2001,
        uow=uow,
        client=fake_pixiv_client,
        download_dir=str(tmp_path),
    )
    assert updated.title == "NewTitle"
    assert updated.author_name == "KeepMe"
    assert updated.series_id is None
    assert updated.series_index is None


def test_delete_novel_single_lookup_and_oserror_resilience(
    committed_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from copixiv.features.novels.use_cases.delete_novel import delete_novel

    uow = SqlUnitOfWork(committed_session_factory)
    storage = FileStorage(str(tmp_path))
    file_path = storage.save_novel_text(3001, "ToDel", "body")

    with uow.begin():
        uow.session.add(models.Author(author_id=30, author_name="Auth30", source_key="30"))
        uow.session.flush()
        uow.session.add(
            models.Novel(
                id=3001,
                title="ToDel",
                author_id=30,
                author_name="Auth30",
                path=str(file_path),
                source="pixiv",
                source_key="3001",
            )
        )

    def _boom(_path: str) -> None:
        raise OSError("disk read-only")

    monkeypatch.setattr(storage, "delete_novel_files", _boom)

    delete_novel(3001, uow=uow, file_storage=storage)  # Must not raise OSError after DB commit

    with uow.begin():
        assert uow.session.get(models.Novel, 3001) is None

    with pytest.raises(NotFoundError):
        delete_novel(3001, uow=uow, file_storage=storage)


@pytest.mark.asyncio
async def test_ingest_merges_done_ids_and_skips_get_skip_ids_on_redownload(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from copixiv.features.failures.repo import FailedNovelRepository

    class _DoneBatch:
        def __init__(self) -> None:
            self.outcomes: dict[int, str] = {}

        async def await_all(self) -> list[tuple[int, str]]:
            return []

        def drain_outcomes(self) -> dict[int, str]:
            out, self.outcomes = self.outcomes, {}
            return out

    class _DoneImageDownloader:
        def new_batch(self) -> _DoneBatch:
            return _DoneBatch()

        async def process_novel_assets(self, data, *, force: bool = False, batch=None) -> None:
            del force
            batch.outcomes[data.id] = "done"

    def _forbid_get_skip_ids(self, ids):
        del self, ids
        raise AssertionError("get_skip_ids must not be called when redownload=True")

    monkeypatch.setattr(FailedNovelRepository, "get_skip_ids", _forbid_get_skip_ids)

    storage = FileStorage(str(tmp_path))
    fake_pixiv_client.webview_responses[4001] = _fake_webview(
        4001,
        title="WithImg",
        author_id=40,
        text="正文 [uploadedimage:1]",
    )
    fake_pixiv_client.user_detail_responses[40] = {"user": {"id": 40, "name": "Auth40"}}

    outcome = await ingest(
        novels=[_fake_novel_info(4001, title="WithImg", author_id=40, author_name="Auth40")],
        session_factory=committed_session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=_DoneImageDownloader(),
        force=True,
    )
    assert outcome.new_count == 1
    with committed_session_factory() as session:
        n4001 = session.get(models.Novel, 4001)
        assert n4001 is not None
        assert n4001.has_epub == EpubStatus.DONE


@pytest.mark.asyncio
async def test_ingest_existing_meta_resolves_missing_series_index_and_skips_retired_or_indexed(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    downloader = _DummyImageDownloader()

    with committed_session_factory() as session:
        session.add(models.Author(author_id=50, author_name="Auth50", source_key="50"))
        session.add_all(
            [
                models.Series(series_id=500, series_name="S500", author_id=50, source_key="500"),
                models.Series(series_id=501, series_name="S501", author_id=50, source_key="501"),
                models.SeriesDeadLetter(
                    series_id=501,
                    consecutive_failures=3,
                    last_error="roster_absent",
                    retired_at=datetime.now(UTC),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                # Standalone novel later added to series 500 -> must resolve index
                models.Novel(
                    id=5001,
                    title="Standalone->Series",
                    author_id=50,
                    author_name="Auth50",
                    source_key="5001",
                ),
                # Already indexed in series 500 -> must NOT trigger roster fetch on its own
                models.Novel(
                    id=5002,
                    title="AlreadyIndexed",
                    author_id=50,
                    author_name="Auth50",
                    series_id=500,
                    series_name="S500",
                    series_index=1,
                    source_key="5002",
                ),
                # Missing index in retired series 501 -> must NOT poll roster every round
                models.Novel(
                    id=5003,
                    title="RetiredSeries",
                    author_id=50,
                    author_name="Auth50",
                    series_id=501,
                    series_name="S501",
                    series_index=None,
                    source_key="5003",
                ),
            ]
        )
        session.commit()

    fake_pixiv_client.series_responses[500] = {"novels": [SimpleNamespace(id=5002), SimpleNamespace(id=5001)]}

    # First run: only 5001 needs roster resolution -> fetches series 500 once, skips 501
    await ingest(
        [
            _fake_novel_info(
                5001,
                title="Standalone->Series",
                author_id=50,
                series=SimpleNamespace(id=500, title="S500"),
            ),
            _fake_novel_info(
                5002,
                title="AlreadyIndexed",
                author_id=50,
                series=SimpleNamespace(id=500, title="S500"),
            ),
            _fake_novel_info(
                5003,
                title="RetiredSeries",
                author_id=50,
                series=SimpleNamespace(id=501, title="S501"),
            ),
        ],
        session_factory=committed_session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=downloader,
    )
    assert fake_pixiv_client.calls == [("novel_series", (500, True))]

    with committed_session_factory() as session:
        n5001 = session.get(models.Novel, 5001)
        assert n5001 is not None
        assert n5001.series_id == 500
        assert n5001.series_index == 2

    # Second run: 5001 is now indexed -> zero novel_series calls
    fake_pixiv_client.calls.clear()
    await ingest(
        [
            _fake_novel_info(
                5001,
                title="Standalone->Series",
                author_id=50,
                series=SimpleNamespace(id=500, title="S500"),
            ),
            _fake_novel_info(
                5003,
                title="RetiredSeries",
                author_id=50,
                series=SimpleNamespace(id=501, title="S501"),
            ),
        ],
        session_factory=committed_session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=downloader,
    )
    assert fake_pixiv_client.calls == []


@pytest.mark.asyncio
async def test_sync_novel_resolves_series_index_when_added_to_or_moved_between_series(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
):
    from copixiv.features.novels.use_cases.sync_novel import sync_novel

    uow = SqlUnitOfWork(committed_session_factory)
    with uow.begin():
        uow.session.add(models.Author(author_id=60, author_name="Auth60", source_key="60"))
        uow.session.add(models.Series(series_id=600, series_name="OldS", author_id=60, source_key="600"))
        uow.session.flush()
        uow.session.add(
            models.Novel(
                id=6001,
                title="Moved",
                author_id=60,
                author_name="Auth60",
                series_id=600,
                series_name="OldS",
                series_index=5,
                path=str(tmp_path / "6001.txt"),
                source="pixiv",
                source_key="6001",
            )
        )

    fake_pixiv_client.webview_responses[6001] = _fake_webview(
        6001,
        title="Moved",
        author_id=60,
        series_id=601,
        series_title="NewS",
    )
    fake_pixiv_client.series_responses[601] = {"novels": [SimpleNamespace(id=6000), SimpleNamespace(id=6001)]}

    updated = await sync_novel(
        6001,
        uow=uow,
        client=fake_pixiv_client,
        download_dir=str(tmp_path),
    )
    assert updated.series_id == 601
    assert updated.series_name == "NewS"
    assert updated.series_index == 2


@pytest.mark.asyncio
async def test_ingest_retries_existing_novel_with_retryable_failure_and_stops_at_max(
    committed_session_factory,
    fake_pixiv_client,
    tmp_path: Path,
):
    from copixiv.features.failures.repo import MAX_RETRIES

    storage = FileStorage(str(tmp_path))
    downloader = _DummyImageDownloader()

    with committed_session_factory() as session:
        session.add(models.Author(author_id=70, author_name="Auth70", source_key="70"))
        session.flush()
        session.add_all(
            [
                models.Novel(
                    id=7001,
                    title="RetryableAsset",
                    author_id=70,
                    author_name="Auth70",
                    has_epub=EpubStatus.PENDING,
                    source_key="7001",
                ),
                models.Novel(
                    id=7002,
                    title="ExhaustedAsset",
                    author_id=70,
                    author_name="Auth70",
                    has_epub=EpubStatus.PENDING,
                    source_key="7002",
                ),
            ]
        )
        session.add_all(
            [
                models.FailedNovel(
                    source="pixiv",
                    source_key="7001",
                    novel_id=7001,
                    error_message="EPUB 不完整",
                    failed_times=1,
                ),
                models.FailedNovel(
                    source="pixiv",
                    source_key="7002",
                    novel_id=7002,
                    error_message="EPUB 不完整",
                    failed_times=MAX_RETRIES,
                ),
            ]
        )
        session.commit()

    fake_pixiv_client.webview_responses[7001] = _fake_webview(7001, title="RetryableAsset", author_id=70)
    await ingest(
        [
            _fake_novel_info(7001, title="RetryableAsset", author_id=70, author_name="Auth70"),
            _fake_novel_info(7002, title="ExhaustedAsset", author_id=70, author_name="Auth70"),
        ],
        session_factory=committed_session_factory,
        client=fake_pixiv_client,
        file_storage=storage,
        image_downloader=downloader,
    )
    assert ("webview_novel", 7001) in fake_pixiv_client.calls
    assert ("webview_novel", 7002) not in fake_pixiv_client.calls
    with committed_session_factory() as session:
        assert (
            session.execute(
                select(models.FailedNovel).where(
                    models.FailedNovel.source == "pixiv",
                    models.FailedNovel.source_key == "7001",
                )
            ).scalar_one_or_none()
            is None
        )
        assert (
            session.execute(
                select(models.FailedNovel).where(
                    models.FailedNovel.source == "pixiv",
                    models.FailedNovel.source_key == "7002",
                )
            ).scalar_one_or_none()
            is not None
        )
