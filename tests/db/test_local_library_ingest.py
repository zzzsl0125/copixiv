"""Layer 2 DB tests: local library ingest (TXT/EPUB), SHA-256 dedup, negative axis monotonicity, and preset author."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.factories import make_simple_epub

from copixiv.core import naming as formats
from copixiv.core.models import EpubStatus
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.library.ingest import (
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    ingest_uploads,
)
from copixiv.features.library.repo import next_local_id
from copixiv.features.system.repo import SQLAlchemySettingRepository
from copixiv.storage.file_storage import FileStorage


def test_local_ingest_utf8_gb18030_epub_and_monotonic_negative_ids(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    epub_bytes = make_simple_epub("EPUB标题", "EPUB作者", "第三本书的正文内容")

    with uow.begin():
        results = ingest_uploads(
            uow.session,
            storage,
            [
                ("《第一本书》作者：甲.txt", "第一本UTF8正文\r\n第二行".encode()),
                ("第二本书.txt", "这是GB18030编码的中文小说正文内容，足够长以通过检测。".encode("gb18030")),
                ("ignored_name.epub", epub_bytes),
                ("bad.epub", b"not-a-zip"),
            ],
        )

        assert [r.status for r in results] == [
            STATUS_CREATED,
            STATUS_CREATED,
            STATUS_CREATED,
            STATUS_FAILED,
        ]
        created_ids = [r.novel_id for r in results[:3]]
        assert all(nid is not None and nid < 0 for nid in created_ids)
        # Negative coordinates strictly decrease (abs(id) monotonically increases)
        assert created_ids[0] > created_ids[1] > created_ids[2]

        n_txt = uow.session.get(models.Novel, created_ids[0])
        n_epub = uow.session.get(models.Novel, created_ids[2])
        assert n_txt is not None
        assert n_txt.source == "local"
        assert n_txt.has_epub == EpubStatus.NO_IMAGES
        assert Path(n_txt.path).is_file()

        assert n_epub is not None
        assert n_epub.title == "EPUB标题"
        assert n_epub.author_name == "EPUB作者"
        assert n_epub.has_epub == EpubStatus.DONE
        assert formats.sibling_path(Path(n_epub.path), formats.EPUB).is_file()


def test_local_ingest_dedup_attaches_epub_and_updates_preset_author(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    body = "完全相同的规范化正文内容"

    with uow.begin():
        r1 = ingest_uploads(
            uow.session,
            storage,
            [("orig.txt", body.encode("utf-8"))],
        )
        assert r1[0].status == STATUS_CREATED
        nid = r1[0].novel_id
        assert nid is not None

    # Re-upload identical body as EPUB + preset_author_id=900
    epub_same_body = make_simple_epub("Orig", "Auth", body)
    with uow.begin():
        r2 = ingest_uploads(
            uow.session,
            storage,
            [("dup.epub", epub_same_body)],
            preset_author_id=900,
            preset_author_name="预设作者",
        )
        assert r2[0].status == STATUS_DUPLICATE
        assert r2[0].novel_id == nid
        assert r2[0].epub_attached is True

        novel = uow.session.get(models.Novel, nid)
        assert novel is not None
        assert novel.author_id == 900
        assert novel.author_name == "预设作者"
        assert novel.has_epub == EpubStatus.DONE
        assert formats.sibling_path(Path(novel.path), formats.EPUB).is_file()

        author = uow.session.get(models.Author, 900)
        assert author is not None
        assert author.author_name == "预设作者"


def test_setting_get_bool_respects_default_and_concurrent_next_local_id(
    committed_session_factory,
):
    with committed_session_factory() as session:
        repo = SQLAlchemySettingRepository(session)
        repo.set_value("k_invalid", "maybe")
        repo.set_value("k_false", "false")
        repo.set_value("k_true", "ON")
        session.commit()

    with committed_session_factory() as session:
        repo = SQLAlchemySettingRepository(session)
        assert repo.get_bool("k_invalid", default=True) is True
        assert repo.get_bool("k_invalid", default=False) is False
        assert repo.get_bool("k_false", default=True) is False
        assert repo.get_bool("k_true", default=False) is True

    # Concurrent next_local_id calls advancing the sequence must return strictly distinct negative IDs
    barrier = threading.Barrier(4)

    def _allocate() -> int:
        barrier.wait(timeout=5)
        with committed_session_factory() as session:
            nid = next_local_id(session)
            session.commit()
            return nid

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: _allocate(), range(4)))
    assert all(nid < 0 for nid in ids)
    assert len(set(ids)) == 4


@pytest.mark.asyncio
async def test_epub_reconcile_does_not_downgrade_local_epub(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    from copixiv.features.novels.use_cases.epub_reconcile import reconcile_epub_status

    storage = FileStorage(str(tmp_path))
    epub_bytes = make_simple_epub("本地EPUB", "本地作者", "正文内容无images目录")

    with uow.begin():
        results = ingest_uploads(uow.session, storage, [("local.epub", epub_bytes)])
        local_id = results[0].novel_id
        assert local_id is not None

    await reconcile_epub_status(uow)

    with uow.begin():
        novel = uow.session.get(models.Novel, local_id)
        assert novel is not None
        assert novel.has_epub == EpubStatus.DONE


def test_local_ingest_rollback_discards_created_files(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    storage = FileStorage(str(tmp_path))
    created_paths: list[Path] = []

    def _run_and_fail() -> None:
        with uow.begin():
            results = ingest_uploads(
                uow.session,
                storage,
                [("rollback_book.txt", "回滚测试正文内容".encode())],
            )
            assert results[0].status == STATUS_CREATED
            novel = uow.session.get(models.Novel, results[0].novel_id)
            assert novel is not None
            created_paths.append(Path(novel.path))
            assert created_paths[0].is_file()
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _run_and_fail()

    assert not created_paths[0].exists()


@pytest.mark.asyncio
async def test_batch_download_truncation_sorts_by_abs_id(
    uow: SqlUnitOfWork,
    tmp_path: Path,
):
    from datetime import UTC, datetime

    from copixiv.db.base import ensure_parent_rows
    from copixiv.features.novels.use_cases.batch_download import BatchDownloadUseCase

    storage = FileStorage(str(tmp_path))
    with uow.begin():
        ensure_parent_rows(uow.session, author_ids={1})
        p1 = storage.save_novel_text(10_000_000, "PixivOld", "old body")
        p2 = storage.save_novel_text(25_000_000, "PixivNew", "new body")
        p3 = storage.local_novel_path(-30_000_000, "LocalLatest")
        p3.parent.mkdir(parents=True, exist_ok=True)
        p3.write_text("local body", encoding="utf-8")

        now = datetime.now(UTC)
        uow.session.add_all(
            [
                models.Novel(
                    id=10_000_000,
                    title="PixivOld",
                    author_id=1,
                    author_name="A",
                    path=str(p1),
                    like=0,
                    view=0,
                    text=8,
                    create_time=now,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=[],
                    is_favourite=False,
                    source="pixiv",
                ),
                models.Novel(
                    id=25_000_000,
                    title="PixivNew",
                    author_id=1,
                    author_name="A",
                    path=str(p2),
                    like=0,
                    view=0,
                    text=8,
                    create_time=now,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=[],
                    is_favourite=False,
                    source="pixiv",
                ),
                models.Novel(
                    id=-30_000_000,
                    title="LocalLatest",
                    author_id=None,
                    author_name="本地",
                    path=str(p3),
                    like=0,
                    view=0,
                    text=10,
                    create_time=now,
                    has_epub=EpubStatus.NO_IMAGES,
                    tags=[],
                    is_favourite=False,
                    source="local",
                    source_key="abs-sort-test",
                ),
            ]
        )

    uc = BatchDownloadUseCase(uow, download_dir=str(tmp_path))
    result = await uc.execute(novel_ids=[10_000_000, 25_000_000, -30_000_000], limit=2)
    try:
        assert result.added_count == 2
        import zipfile

        with zipfile.ZipFile(result.zip_buffer) as zf:
            names = "\n".join(zf.namelist())
        assert "-30000000" in names
        assert "25000000" in names
        assert "10000000" not in names
    finally:
        result.zip_buffer.close()
