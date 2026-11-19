"""Layer 2 DB tests: Plan A author & series aggregate summary invariants across all write paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from copixiv.core.draft import build_novel
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.library.ingest import ingest_uploads
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.use_cases.batch_edit_metadata import (
    BatchEditMetadataUseCase,
)
from copixiv.features.novels.use_cases.delete_novel import DeleteNovelUseCase
from copixiv.features.novels.use_cases.update_metadata import (
    UpdateNovelMetadataUseCase,
)
from copixiv.storage.file_storage import FileStorage


def _assert_summaries_consistent(session) -> None:
    """Assert Author and Series summary columns match live GROUP BY over Novel."""
    novel_author_aggs = {
        row.author_id: (row.cnt, row.likes, row.views, row.texts)
        for row in session.execute(
            select(
                models.Novel.author_id,
                func.count(models.Novel.id).label("cnt"),
                func.coalesce(func.sum(models.Novel.like), 0).label("likes"),
                func.coalesce(func.sum(models.Novel.view), 0).label("views"),
                func.coalesce(func.sum(models.Novel.text), 0).label("texts"),
            )
            .where(models.Novel.author_id.isnot(None))
            .group_by(models.Novel.author_id)
        ).all()
    }
    for author in session.execute(select(models.Author)).scalars().all():
        expected = novel_author_aggs.get(author.author_id, (0, 0, 0, 0))
        assert (author.novel_count, author.like, author.view, author.text) == expected

    novel_series_aggs = {
        row.series_id: (row.cnt, row.likes, row.views, row.texts, row.max_author)
        for row in session.execute(
            select(
                models.Novel.series_id,
                func.count(models.Novel.id).label("cnt"),
                func.coalesce(func.sum(models.Novel.like), 0).label("likes"),
                func.coalesce(func.sum(models.Novel.view), 0).label("views"),
                func.coalesce(func.sum(models.Novel.text), 0).label("texts"),
                func.max(models.Novel.author_id).label("max_author"),
            )
            .where(models.Novel.series_id.isnot(None))
            .group_by(models.Novel.series_id)
        ).all()
    }
    for series in session.execute(select(models.Series)).scalars().all():
        if series.series_id in novel_series_aggs:
            cnt, likes, views, texts, max_author = novel_series_aggs[series.series_id]
            assert (series.novel_count, series.like, series.view, series.text) == (
                cnt,
                likes,
                views,
                texts,
            )
            assert series.author_id == max_author
            assert series.author_id is not None
        else:
            assert (series.novel_count, series.like, series.view, series.text) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_summary_invariants_across_all_write_paths(uow: SqlUnitOfWork, tmp_path: Path):
    storage = FileStorage(str(tmp_path))

    # 1. persist_novels: create Pixiv novels for Author 10/20 and Series 100/200
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
                build_novel(
                    id=1002,
                    title="P2",
                    author_id=10,
                    author_name="Author10",
                    like=30,
                    view=300,
                    text=2000,
                    series_id=100,
                    series_name="S100",
                    download_dir=str(tmp_path),
                ),
            ],
        )
        _assert_summaries_consistent(uow.session)

    # Reassign novel 1002 via persist_novels to Author 20 / Series 200 and change stats
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=1002,
                    title="P2",
                    author_id=20,
                    author_name="Author20",
                    like=40,
                    view=400,
                    text=2500,
                    series_id=200,
                    series_name="S200",
                    download_dir=str(tmp_path),
                ),
            ],
        )
        _assert_summaries_consistent(uow.session)

    # 2. ingest_uploads: ingest local books with preset_author_id=10, then re-ingest duplicate -> Author 20
    with uow.begin():
        res = ingest_uploads(
            uow.session,
            storage,
            [("LocalA.txt", "本地作品正文A".encode()), ("LocalB.txt", "本地作品正文B".encode())],
            preset_author_id=10,
            preset_author_name="Author10",
        )
        local_ids = [r.novel_id for r in res if r.novel_id is not None]
        assert len(local_ids) == 2
        _assert_summaries_consistent(uow.session)

    with uow.begin():
        ingest_uploads(
            uow.session,
            storage,
            [("LocalA.txt", "本地作品正文A".encode())],
            preset_author_id=20,
            preset_author_name="Author20",
        )
        _assert_summaries_consistent(uow.session)

    # 3. UpdateNovelMetadataUseCase: move local_ids[0] back to Author 10 / Series 100, then clear series
    uc_single = UpdateNovelMetadataUseCase(uow)
    await uc_single.execute(local_ids[0], {"author_id": 10, "series_id": 100})
    with uow.begin():
        _assert_summaries_consistent(uow.session)

    await uc_single.execute(local_ids[0], {"series_id": None})
    with uow.begin():
        _assert_summaries_consistent(uow.session)

    # 4. BatchEditMetadataUseCase: mixed [local_ids[0], local_ids[1], 1001 (pixiv)] -> Author 20 / Series 200
    uc_batch = BatchEditMetadataUseCase(uow)
    counts = await uc_batch.execute(
        [local_ids[0], local_ids[1], 1001],
        author_id=20,
        series_id=200,
        apply=True,
    )
    assert counts["matched"] == 3
    assert counts["applicable"] == 2
    assert counts["skipped"] == 1
    assert counts["affected"] == 2
    with uow.begin():
        _assert_summaries_consistent(uow.session)

    # 5. DeleteNovelUseCase & repo.delete_many
    DeleteNovelUseCase(uow, storage).execute(local_ids[0])
    with uow.begin():
        _assert_summaries_consistent(uow.session)

    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).delete_many([1001, 1002, local_ids[1]])
        _assert_summaries_consistent(uow.session)
        s100 = uow.session.get(models.Series, 100)
        assert s100 is not None
        assert s100.novel_count == 0


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
