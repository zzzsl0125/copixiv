"""``prune_task_history`` — trim the audit/queue table by two rules at once.

``task_history`` is both the audit trail and the task queue, and nothing ever
deleted from it: production held 1,566 rows / 13 MB of ``result``, the largest
single ``result`` 525 kB.  The queue query (``status='pending' ORDER BY id``)
is the one read whose cost tracks total history size, so trimming is a
correctness-of-performance concern, not just disk hygiene.

A row survives when **either** rule holds — among the newest ``keep`` rows, or
younger than ``max_age_days``.  Both matter: age alone wipes a quiet month, count
alone wipes the only record of a busy week.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from copixiv.db.models import Setting, TaskHistory
from copixiv.db.uow import SqlUnitOfWork
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.maintenance import (
    RETENTION_SETTING_KEY,
    PruneTaskHistoryArgs,
    prune_task_history,
)


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    yield


def _row(sf, task_id: int, *, days_old: int, status: str = "success") -> None:
    started = datetime.now(timezone.utc) - timedelta(days=days_old)
    with sf() as s:
        s.add(TaskHistory(
            id=task_id, name=f"t{task_id}", task_func="x", status=status,
            start_time=started,
            end_time=started + timedelta(seconds=1),
            result={"summary": "s", "log": "x" * 100},
        ))
        s.commit()


def _ids(sf) -> set[int]:
    with sf() as s:
        return set(s.execute(text("select id from task_history")).scalars().all())


def _ctx(sf) -> TaskContext:
    return TaskContext(uow=SqlUnitOfWork(sf), session_factory=sf)


class TestPruneTaskHistory:
    async def test_dry_run_deletes_nothing(self, session_factory):
        for i in range(1, 6):
            _row(session_factory, i, days_old=400)

        result = await prune_task_history(
            PruneTaskHistoryArgs(keep=1, max_age_days=30), _ctx(session_factory),
        )

        assert _ids(session_factory) == {1, 2, 3, 4, 5}
        assert "试运行" in result.summary
        assert "可删除 4 行" in result.summary

    async def test_apply_keeps_newest_n_and_recent_rows(self, session_factory):
        """Both rules are unions: newest-3 plus anything inside 30 days."""
        for i in range(1, 6):
            _row(session_factory, i, days_old=400)        # 1..5 all ancient
        _row(session_factory, 6, days_old=5)              # recent → kept

        await prune_task_history(
            PruneTaskHistoryArgs(apply=True, keep=3, max_age_days=30),
            _ctx(session_factory),
        )

        # newest 3 = {4,5,6}; within 30 days = {6} → union {4,5,6}
        assert _ids(session_factory) == {4, 5, 6}

    async def test_pending_and_running_rows_are_never_deleted(
        self, session_factory,
    ):
        """Live queue entries are not history, whatever their age."""
        for i in range(1, 4):
            _row(session_factory, i, days_old=400)
        _row(session_factory, 7, days_old=400, status="pending")
        _row(session_factory, 8, days_old=400, status="running")

        await prune_task_history(
            PruneTaskHistoryArgs(apply=True, keep=3, max_age_days=30),
            _ctx(session_factory),
        )

        remaining = _ids(session_factory)
        assert {7, 8} <= remaining, "队列行不能被当历史裁掉"
        # newest-3 = {7, 8, 3}, so the one completed row inside that window
        # also survives; 1 and 2 are both old and below the boundary.
        assert 3 in remaining
        assert remaining == {3, 7, 8}

    async def test_setting_row_supplies_the_policy(self, session_factory):
        with session_factory() as s:
            s.add(Setting(
                key=RETENTION_SETTING_KEY,
                value=json.dumps({"keep": 2, "max_age_days": 10}),
            ))
            s.commit()
        for i in range(1, 5):
            _row(session_factory, i, days_old=400)

        await prune_task_history(
            PruneTaskHistoryArgs(apply=True), _ctx(session_factory),
        )

        assert _ids(session_factory) == {3, 4}     # keep=2 from the setting

    async def test_args_override_the_setting(self, session_factory):
        with session_factory() as s:
            s.add(Setting(
                key=RETENTION_SETTING_KEY,
                value=json.dumps({"keep": 99, "max_age_days": 999}),
            ))
            s.commit()
        for i in range(1, 5):
            _row(session_factory, i, days_old=400)

        await prune_task_history(
            PruneTaskHistoryArgs(apply=True, keep=1, max_age_days=1),
            _ctx(session_factory),
        )

        assert _ids(session_factory) == {4}

    async def test_malformed_setting_falls_back_to_defaults(
        self, session_factory,
    ):
        with session_factory() as s:
            s.add(Setting(key=RETENTION_SETTING_KEY, value="not-json"))
            s.commit()
        for i in range(1, 4):
            _row(session_factory, i, days_old=400)

        result = await prune_task_history(
            PruneTaskHistoryArgs(), _ctx(session_factory),
        )

        # Defaults (keep 2000 / 180 days) delete nothing from 3 ancient rows.
        assert "无需裁剪" in result.summary

    async def test_nothing_to_do_reports_cleanly(self, session_factory):
        _row(session_factory, 1, days_old=1)

        result = await prune_task_history(
            PruneTaskHistoryArgs(apply=True, keep=100, max_age_days=180),
            _ctx(session_factory),
        )

        assert "无需裁剪" in result.summary
        assert _ids(session_factory) == {1}


class TestLogTruncation:
    """A task's stored log is capped; the tail (where the error is) is kept."""

    def test_short_log_is_verbatim(self):
        from copixiv.log import capture_logs, logger

        with capture_logs() as get_logs:
            logger.info("hello")
        text = get_logs()
        assert "hello" in text
        assert "日志过长" not in text

    def test_huge_log_is_truncated_to_the_tail(self):
        """One production row stored 1.26 M characters of log.

        The cap keeps the *end*: when a task fails, the exception and the last
        actions before it are what an operator needs, not the setup noise at the
        top of a 30-minute run.
        """
        from copixiv.log import MAX_TASK_LOG_BYTES, capture_logs, logger

        with capture_logs() as get_logs:
            logger.info("FIRST_LINE_MARKER")
            for i in range(12_000):
                logger.info(f"filler line {i} " + "x" * 60)
            logger.error("LAST_LINE_MARKER")
        text = get_logs()

        assert len(text.encode("utf-8")) <= MAX_TASK_LOG_BYTES + 200
        assert "LAST_LINE_MARKER" in text, "尾部（真正的错误）必须保留"
        assert "FIRST_LINE_MARKER" not in text, "头部应被裁掉"
        assert "日志过长" in text, "必须说明日志被截断"

    def test_truncation_cuts_on_a_line_boundary(self):
        from copixiv.log import capture_logs, logger

        with capture_logs() as get_logs:
            for i in range(12_000):
                logger.info(f"line-{i} " + "y" * 60)
        body = get_logs().split("\n", 1)[1]     # drop the notice line
        assert body.startswith("2026-") or body.startswith("line-"), (
            f"截断处应是完整行，实际开头：{body[:60]!r}"
        )
