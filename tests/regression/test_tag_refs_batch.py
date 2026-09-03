"""Regression: author_name updates must not touch tag rows, and the
statement-level tag trigger must aggregate a whole batch in one delta set.

Post-MVCC design (no global write lock): ``sync_tag_refs`` only fires on
``INSERT`` / ``UPDATE OF tags`` / ``DELETE`` and maintains
``reference_count`` set-based via transition tables.  These tests pin the
two properties that caused the daily cron lock churn:

1. ``update_author_name`` (and any non-tags novel write) never updates the
   ``tag`` table — the trigger is declared ``UPDATE OF tags``.
2. A single SQL statement changing tags for many novels is accounted for
   exactly once per distinct (novel, tag) pair, with no double-count for
   duplicate array elements and no per-row tag lock churn.
"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from copixiv.db.models import Author, Novel, Tag
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import rebuild_tag_counts

_ALL_TABLES = (
    "novel, author, series, tag, tag_alias, tag_preference, "
    "failed_novel, novel_search, scheduled_task, task_history, "
    "token, setting, search_history"
)


@pytest.fixture(autouse=True)
def _isolated_db(pg_engine):
    """Truncate application tables before AND after each test.

    ``conftest.clean_db`` only truncates before the test; these tests also
    clean up after themselves so a later test file (e.g. the maintenance
    suite, which seeds Author(1) unconditionally) sees an empty DB.
    """
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    yield
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def _author(session_factory):
    with session_factory() as s:
        s.add(Author(author_id=1, author_name="作者"))
        s.commit()


def _tag_counts(session_factory) -> dict[str, int]:
    with session_factory() as s:
        return {
            row.name: row.reference_count
            for row in s.execute(
                text("SELECT name, reference_count FROM tag")
            ).all()
        }


class TestAuthorNameUpdateDoesNotTouchTags:
    async def test_author_name_update_keeps_tag_rows_untouched(
        self, session_factory, clean_db, _author,
    ):
        """A guard trigger on tag proves ``update_author_name`` performs no
        tag UPDATE at all — the regression that used to fire the per-row
        trigger thousands of times per cron run."""
        with session_factory() as s:
            s.add(Novel(id=1, title="a", author_id=1, author_name="old", tags=["R-18"]))
            s.add(Novel(id=2, title="b", author_id=1, author_name="old", tags=["R-18"]))
            s.commit()

        with session_factory() as s:
            s.execute(text(
                """
                CREATE OR REPLACE FUNCTION forbid_tag_update() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION 'tag row update forbidden in this test';
                END; $$;
                """
            ))
            s.execute(text(
                "CREATE TRIGGER trg_forbid_tag_update "
                "BEFORE UPDATE ON tag FOR EACH ROW "
                "EXECUTE FUNCTION forbid_tag_update()"
            ))
            s.commit()

        try:
            uow = SqlUnitOfWork(session_factory)
            async with uow.begin():
                await SQLAlchemyAuthorRepository(uow.session).update_author_name(1, "new")
        finally:
            with session_factory() as s:
                s.execute(text("DROP TRIGGER IF EXISTS trg_forbid_tag_update ON tag"))
                s.execute(text("DROP FUNCTION IF EXISTS forbid_tag_update()"))
                s.commit()

        counts = _tag_counts(session_factory)
        assert counts["R-18"] == 2, "author-name writeback must not change tag counts"
        with session_factory() as s:
            names = {
                row.author_name
                for row in s.execute(
                    text("SELECT author_name FROM novel WHERE author_id = 1")
                ).all()
            }
            assert names == {"new"}


class TestStatementLevelBatchTrigger:
    async def test_multi_row_statement_updates_counts_exactly(
        self, session_factory, clean_db, _author,
    ):
        with session_factory() as s:
            s.add(Novel(id=1, title="n1", author_id=1, tags=["A"]))
            s.add(Novel(id=2, title="n2", author_id=1, tags=["A", "B"]))
            s.add(Novel(id=3, title="n3", author_id=1, tags=["C"]))
            s.commit()

        assert _tag_counts(session_factory) == {"A": 2, "B": 1, "C": 1}

        with session_factory() as s:
            s.execute(
                text("UPDATE novel SET tags = ARRAY['B'] WHERE id IN (1, 2, 3)")
            )
            s.commit()

        assert _tag_counts(session_factory) == {"A": 0, "B": 3, "C": 0}


class TestRebuildTagCounts:
    async def test_rebuild_tag_counts_uses_novel_tags_array(
        self, session_factory, clean_db, _author,
    ):
        """The maintenance fallback must recompute from ``novel.tags`` (the
        ``novel_tag`` table no longer exists)."""
        with session_factory() as s:
            s.add(Novel(id=1, title="a", author_id=1, tags=["A"]))
            s.add(Novel(id=2, title="b", author_id=1, tags=["A", "B"]))
            s.commit()

        # Simulate drift: wrong counts + an orphan tag.
        with session_factory() as s:
            s.execute(text("UPDATE tag SET reference_count = 999 WHERE name IN ('A', 'B')"))
            s.add(Tag(name="ORPHAN", reference_count=123))
            s.commit()

        result = await rebuild_tag_counts(
            TaskContext(uow=SqlUnitOfWork(session_factory))
        )

        assert "标签引用计数重建" in result.summary
        counts = _tag_counts(session_factory)
        assert counts["A"] == 2
        assert counts["B"] == 1
        assert counts["ORPHAN"] == 0


class TestRunWriteTransactionRetry:
    """Unit-test the LockNotAvailable retry boundary (no real DB needed)."""

    @staticmethod
    def _lock_error() -> OperationalError:
        class _StubPgError(Exception):
            pgcode = "55P03"

        return OperationalError("stmt", {}, _StubPgError("could not obtain lock"))

    async def test_retries_once_then_succeeds(self):
        class _FakeUow:
            def __init__(self):
                self.calls = 0

            @asynccontextmanager
            async def begin(self):
                yield self

        uow = _FakeUow()

        async def fn(uw):
            uw.calls += 1
            if uw.calls == 1:
                raise self._lock_error()
            return "ok"

        result = await run_write_transaction(uow, fn, base_delay=0.0)
        assert result == "ok"
        assert uow.calls == 2

    async def test_exhausts_attempts_then_raises(self):
        class _FakeUow:
            def __init__(self):
                self.calls = 0

            @asynccontextmanager
            async def begin(self):
                yield self

        uow = _FakeUow()

        async def fn(_uw):
            uow.calls += 1
            raise self._lock_error()

        with pytest.raises(OperationalError):
            await run_write_transaction(uow, fn, max_attempts=3, base_delay=0.0)
        assert uow.calls == 3
