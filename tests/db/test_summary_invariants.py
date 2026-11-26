"""Layer 2 DB tests: parent name sync, passive upsert churn guard, and batch edit dry-run."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.factories import make_local_novel

from copixiv.core.draft import build_novel
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.library.ingest import ingest_uploads
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.use_cases.batch_edit_metadata import (
    batch_edit_metadata,
)
from copixiv.features.novels.use_cases.update_metadata import (
    update_novel_metadata,
)
from copixiv.storage.file_storage import FileStorage


@pytest.mark.asyncio
async def test_parent_name_sync_across_write_paths(uow: SqlUnitOfWork, tmp_path: Path):
    storage = FileStorage(str(tmp_path))

    # 1. persist_novels: syncs Author.author_name, Series.series_name, Series.author_id
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=1001,
                    title="P1",
                    author_id=10,
                    author_name="Author10",
                    like=50,
                    view=500,
                    text=1000,
                    series_id=100,
                    series_name="S100",
                    download_dir=str(tmp_path),
                ),
            ],
        )
        a10 = uow.session.get(models.Author, 10)
        s100 = uow.session.get(models.Series, 100)
        assert a10 is not None
        assert a10.author_name == "Author10"
        assert s100 is not None
        assert s100.series_name == "S100"
        assert s100.author_id == 10

    # 2. ingest_uploads: syncs preset author name
    with uow.begin():
        res = ingest_uploads(
            uow.session,
            storage,
            [("LocalA.txt", "本地作品正文A".encode()), ("LocalB.txt", "本地作品正文B".encode())],
            preset_author_id=20,
            preset_author_name="Author20",
        )
        local_ids = [r.novel_id for r in res if r.novel_id is not None]
        assert len(local_ids) == 2
        a20 = uow.session.get(models.Author, 20)
        assert a20 is not None
        assert a20.author_name == "Author20"

    # 3. update_novel_metadata: syncs author/series display names
    await update_novel_metadata(local_ids[0], {"author_id": 10, "series_id": 100}, uow=uow)
    with uow.begin():
        novel = uow.session.get(models.Novel, local_ids[0])
        assert novel is not None
        assert novel.author_name == "Author10"
        assert novel.series_name == "S100"

    # 4. batch_edit_metadata: mixed [local_ids[0], local_ids[1], 1001 (pixiv)] -> Author 20 / Series 100
    counts = await batch_edit_metadata(
        [local_ids[0], local_ids[1], 1001],
        uow=uow,
        author_id=20,
        series_id=100,
        apply=True,
    )
    assert counts == {"matched": 3, "applicable": 2, "skipped": 1, "affected": 2}


def test_passive_upsert_skips_view_and_small_like_churn(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=7001, title="Quiet", author_id=1, author_name="A", like=100, view=500, text=1000
                )
            ],
        )

    # Pure view bump + tiny like jitter (<5) without force_update -> skipped
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=7001, title="Quiet", author_id=1, author_name="A", like=103, view=950, text=1000
                )
            ],
        )
        row = uow.session.get(models.Novel, 7001)
        assert row is not None
        assert row.like == 100
        assert row.view == 500

    # Like delta >= 5 -> triggers update (refreshing both like and view)
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=7001, title="Quiet", author_id=1, author_name="A", like=106, view=980, text=1000
                )
            ],
        )
        row = uow.session.get(models.Novel, 7001)
        assert row is not None
        assert row.like == 106
        assert row.view == 980

    # Explicit force_update -> always applies even for tiny deltas
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=7001, title="Quiet", author_id=1, author_name="A", like=107, view=999, text=1000
                )
            ],
            force_update=["author_id"],
        )
        row = uow.session.get(models.Novel, 7001)
        assert row is not None
        assert row.like == 107
        assert row.view == 999


@pytest.mark.asyncio
async def test_batch_edit_metadata_dry_run_with_empty_fields(uow: SqlUnitOfWork):
    with uow.begin():
        uow.session.add(models.Author(author_id=1, author_name="Author", source_key="1"))
        uow.session.flush()
        local_novel = make_local_novel(-1, title="LocalBook", author_id=1, source_key="dryrun1")
        uow.session.add(local_novel)
        uow.session.add(
            models.Novel(id=8001, title="PixivBook", source="pixiv", source_key="8001", path="p/8001.txt")
        )
        local_id = local_novel.id

    counts = await batch_edit_metadata([local_id, 8001], uow=uow, apply=False)
    assert counts == {
        "matched": 2,
        "applicable": 1,
        "skipped": 1,
        "affected": 0,
    }
