"""Layer 2 DB tests: delete_author_and_data cascades, FailedNovel cleanup, and shared series handling."""

from __future__ import annotations

from sqlalchemy import select

from copixiv.core.draft import build_novel
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.novels.ingest import persist_novels


def test_delete_author_cleans_novels_failures_empty_series_and_recomputes_shared_series(
    uow: SqlUnitOfWork,
):
    with uow.begin():
        persist_novels(
            uow,
            [
                # Author 10 owns Series 100 exclusively (200 shared with Author 20)
                build_novel(
                    id=101,
                    title="A10_S100",
                    author_id=10,
                    author_name="A10",
                    series_id=100,
                    series_name="SoloSeries",
                    like=10,
                    view=100,
                    text=500,
                    download_dir="dl",
                ),
                build_novel(
                    id=102,
                    title="A10_S200",
                    author_id=10,
                    author_name="A10",
                    series_id=200,
                    series_name="SharedSeries",
                    like=20,
                    view=200,
                    text=600,
                    download_dir="dl",
                ),
                build_novel(
                    id=201,
                    title="A20_S200",
                    author_id=20,
                    author_name="A20",
                    series_id=200,
                    series_name="SharedSeries",
                    like=35,
                    view=350,
                    text=700,
                    download_dir="dl",
                ),
            ],
        )
        # Force Series 200.author_id = 10 to exercise detaching owned shared series
        s200 = uow.session.get(models.Series, 200)
        assert s200 is not None
        s200.author_id = 10

        uow.session.add(models.FailedNovel(novel_id=101, error_message="404", failed_times=1))
        uow.session.flush()

    with uow.begin():
        paths = SQLAlchemyAuthorRepository(uow.session).delete_author_and_data(10)
        assert len(paths) == 2
        assert any("101.txt" in p for p in paths)
        assert any("102.txt" in p for p in paths)

        assert uow.session.get(models.Author, 10) is None
        assert uow.session.get(models.Novel, 101) is None
        assert uow.session.get(models.Novel, 102) is None
        assert (
            uow.session.execute(
                select(models.FailedNovel).where(models.FailedNovel.novel_id == 101)
            ).scalar_one_or_none()
            is None
        )
        # Empty series 100 was deleted
        assert uow.session.get(models.Series, 100) is None

        # Shared series 200 remains with remaining author 20
        s200 = uow.session.get(models.Series, 200)
        assert s200 is not None
        assert s200.author_id == 20
