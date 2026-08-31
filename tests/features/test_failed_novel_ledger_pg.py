"""Failure-ledger semantics on PostgreSQL (postgres-migration).

Locks the two invariants that motivated removing the ``failed_novel`` FK:

1. A download failure MUST be recordable for a novel that has never been
   persisted to ``novel`` (the ingest pipeline downloads BEFORE persisting:
   plan -> download -> persist), and
2. deleting a novel cleans its ledger rows explicitly (the ledger has no FK
   cascade by design).

Uses dedicated novel ids / seeded rows only — never TRUNCATEs (the
session-scoped ``seeded_db`` sample must stay intact for sibling files).
"""

import pytest
from sqlalchemy import select

from copixiv.db.models import FailedNovel, Novel
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.novels.repo import SQLAlchemyNovelRepository


@pytest.fixture
def a_novel(session_factory):
    """Insert a dedicated novel row (with author placeholder) for ledger tests."""
    author_id = 999_999_990
    novel_id = 190_000_099
    with session_factory() as s:
        SQLAlchemyAuthorRepository(s).ensure_exists({author_id})
        s.add(Novel(
            id=novel_id, title="ledger-test", author_id=author_id,
            tags=["LEDGER_TAG"], shuffle=7, like=7, text=7,
        ))
        s.flush()
        s.commit()
    return novel_id


async def test_record_failure_for_never_persisted_novel(session_factory):
    """A failure for a novel_id with NO novel row must be recorded (no FK)."""
    with session_factory() as s:
        FailedNovelRepository(s).record(
            999_999_991, "download", "boom", title="某本未入库小说"
        )
        s.commit()
        row = s.execute(
            select(FailedNovel).where(FailedNovel.novel_id == 999_999_991)
        ).scalar_one()
        assert row.failure_type == "download"
        assert row.failed_times == 1
        assert row.title == "某本未入库小说"
        # cleanup
        s.delete(row)
        s.commit()


async def test_record_failure_twice_increments(session_factory):
    """Repeated failures bump failed_times (ON CONFLICT DO UPDATE)."""
    with session_factory() as s:
        repo = FailedNovelRepository(s)
        repo.record(999_999_992, "download", "boom1")
        repo.record(999_999_992, "download", "boom2")
        s.commit()
        row = s.execute(
            select(FailedNovel).where(FailedNovel.novel_id == 999_999_992)
        ).scalar_one()
        assert row.failed_times == 2
        assert row.error_message == "boom2"
        # cleanup
        s.delete(row)
        s.commit()


async def test_delete_novel_cleans_ledger_row(a_novel, session_factory):
    """Deleting a novel removes its failed_novel row explicitly."""
    with session_factory() as s:
        repo = SQLAlchemyNovelRepository(s)
        # plant a ledger row for the dedicated novel
        FailedNovelRepository(s).record(a_novel, "download", "x")
        s.commit()
        assert s.execute(
            select(FailedNovel).where(FailedNovel.novel_id == a_novel)
        ).scalar_one() is not None

        await repo.delete(a_novel)
        s.commit()
        assert s.execute(
            select(FailedNovel).where(FailedNovel.novel_id == a_novel)
        ).scalar_one_or_none() is None
