"""``refetch_missing_source`` — bring back novels whose body file vanished.

``check_epub`` labels these rows ``MISSING_SOURCE`` (684 in production, all pixiv
books whose text file is gone with no renamed sibling anywhere).  Labeling is the
right thing for the reconciler — it stays offline and cheap — but nothing was
actually downloading them again, so the label became a full stop.

A successful re-download fixes the status through the ordinary path, which is
what makes the task worth running: ``novel_fetch(redownload=True)`` rewrites the
body, the draft carries a fresh ``has_epub``, and that column is in
``_upsert_rows``' overwrite whitelist, so the terminal ``4`` is replaced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from copixiv.core.models import EpubStatus
from copixiv.core.models import TaskResult
from copixiv.db.models import Author, FailedNovel, Novel
from copixiv.db.uow import SqlUnitOfWork
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import (
    RefetchMissingSourceArgs,
    refetch_missing_source,
)


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


@pytest.fixture
def session_factory(session_factory):
    with session_factory() as s:
        s.add(Author(author_id=1))
        s.commit()
    return session_factory


def _seed(sf, nid: int, status=EpubStatus.MISSING_SOURCE) -> None:
    with sf() as s:
        s.add(Novel(
            id=nid, title=f"n{nid}", author_id=1, path=f"/gone/{nid}.txt",
            like=1, text=10, has_epub=status,
        ))
        s.commit()


def _ledger(sf, nid: int, times: int = 1) -> None:
    from datetime import datetime, timezone

    with sf() as s:
        s.add(FailedNovel(
            novel_id=nid, failure_type="download", error_message="gone",
            failed_times=times, last_failed_at=datetime.now(timezone.utc),
        ))
        s.commit()


def _ctx(sf, *results, calls=None) -> TaskContext:
    """A context whose ``novel_fetch`` is replaced by a scripted stub."""
    import copixiv.tasks.novels as novels_mod

    async def _fake_fetch(args, ctx):        # noqa: ARG001
        if calls is not None:
            calls.append(args.id)
        outcome = results[0] if results else TaskResult(summary="ok")
        return outcome

    novels_mod.novel_fetch = _fake_fetch        # monkeypatch at module level
    return TaskContext(
        uow=SqlUnitOfWork(sf), session_factory=sf, client=object(),
        file_storage=object(),
    )


class TestRefetchMissingSource:
    async def test_dry_run_reports_candidates_without_network(
        self, session_factory,
    ):
        for nid in (1001, 1002):
            _seed(session_factory, nid)
        calls: list[int] = []

        result = await refetch_missing_source(
            RefetchMissingSourceArgs(),
            _ctx(session_factory, calls=calls),
        )

        assert calls == [], "试运行绝不可以发请求"
        assert "试运行" in result.summary
        assert "本轮候选 2 本" in result.summary

    async def test_apply_refetches_each_candidate(self, session_factory):
        for nid in (1011, 1012):
            _seed(session_factory, nid)
        calls: list[int] = []

        await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True),
            _ctx(session_factory, calls=calls),
        )

        assert sorted(calls) == [1011, 1012]

    async def test_ledger_skip_set_is_honoured(self, session_factory):
        """A permanently-dead work must cost one attempt, not one per night."""
        _seed(session_factory, 1021)
        _seed(session_factory, 1022)
        _ledger(session_factory, 1021, times=3)   # gave up on it
        calls: list[int] = []

        await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True),
            _ctx(session_factory, calls=calls),
        )

        assert calls == [1022], "台账里已放弃的行不该再抓"

    async def test_force_recent_days_skips_a_flapping_row(
        self, session_factory,
    ):
        """Recent failure → not retried again inside the window."""
        _seed(session_factory, 1031)
        _ledger(session_factory, 1031)
        calls: list[int] = []

        await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True, force_recent_days=7),
            _ctx(session_factory, calls=calls),
        )

        assert calls == []

    async def test_limit_caps_one_run(self, session_factory):
        for nid in range(1041, 1046):
            _seed(session_factory, nid)
        calls: list[int] = []

        await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True, limit=2),
            _ctx(session_factory, calls=calls),
        )

        assert calls == [1041, 1042]

    async def test_local_books_are_never_candidates(self, session_factory):
        """Negative ids are uploaded files; there is no pixiv id to fetch."""
        _seed(session_factory, -12345)
        _seed(session_factory, 1051)
        calls: list[int] = []

        await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True),
            _ctx(session_factory, calls=calls),
        )

        assert calls == [1051]

    async def test_failures_are_reported_not_hidden(self, session_factory):
        _seed(session_factory, 1061)
        _seed(session_factory, 1062)
        import copixiv.tasks.novels as novels_mod

        calls: list[int] = []

        async def _flaky(args, ctx):        # noqa: ARG001
            calls.append(args.id)
            if args.id == 1061:
                return TaskResult(summary="失败", failed_ids=[1061])
            return TaskResult(summary="ok")

        novels_mod.novel_fetch = _flaky
        ctx = TaskContext(
            uow=SqlUnitOfWork(session_factory),
            session_factory=session_factory, client=object(),
            file_storage=object(),
        )

        result = await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True), ctx,
        )

        assert result.failed_ids == [1061]
        assert "成功 1/2" in result.summary

    async def test_missing_client_skips_cleanly(self, session_factory):
        _seed(session_factory, 1071)
        ctx = TaskContext(
            uow=SqlUnitOfWork(session_factory), session_factory=session_factory,
        )

        result = await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True), ctx,
        )

        assert "缺少 pixiv 客户端" in result.summary

    async def test_no_candidates_reports_cleanly(self, session_factory):
        _seed(session_factory, 1081, status=EpubStatus.DONE)

        result = await refetch_missing_source(
            RefetchMissingSourceArgs(apply=True),
            _ctx(session_factory, calls=[]),
        )

        assert "没有可抓的候选" in result.summary
