"""Task manager lifecycle tests — the full run_task success/failure path.

The M7 regression tests pin the duplicate-run guard; these pin what happens
AFTER enqueueing: status transitions (running → success/failed), result
serialisation, and the lowercase status wire contract.
"""

import asyncio
import json

import pytest
from sqlalchemy import select

from copixiv.core.models import TaskResult
from copixiv.db.models import TaskHistory
from copixiv.tasks.kernel import TaskManagerSystem


@pytest.fixture()
def factory(file_session_factory):
    return file_session_factory


async def _wait_for_terminal_status(factory, name: str) -> str:
    """Poll until the history row leaves pending/running (or timeout)."""
    for _ in range(100):
        with factory() as s:
            row = s.execute(
                select(TaskHistory).where(TaskHistory.name == name)
            ).scalars().first()
        if row is not None and row.status not in ("pending", "running"):
            return row.status
        await asyncio.sleep(0.05)
    raise AssertionError("task never reached a terminal status")


async def _success_task(**kwargs):
    return TaskResult(summary="下载完成: 新小说", new_novel_titles=["新小说"])


async def _failing_task(**kwargs):
    raise RuntimeError("boom")


async def test_success_task_records_success_and_result_json(factory):
    tm = TaskManagerSystem(session_factory=factory, client=None)
    tm.start()
    try:
        tm.run_task("lifecycle_ok", _success_task, {"id": 1})

        status = await _wait_for_terminal_status(factory, "lifecycle_ok")

        assert status == "success"
        assert status == status.lower()  # wire contract: lowercase status

        with factory() as s:
            row = s.execute(
                select(TaskHistory).where(TaskHistory.name == "lifecycle_ok")
            ).scalars().one()
        result = json.loads(row.result)
        assert result["summary"] == "下载完成: 新小说"
        assert result["new_novels_count"] == 1
        assert result["new_novel_titles"] == ["新小说"]
        assert isinstance(row.duration, float) and row.duration >= 0
    finally:
        tm.stop()


async def test_failed_task_records_failed_status(factory):
    tm = TaskManagerSystem(session_factory=factory, client=None)
    tm.start()
    try:
        tm.run_task("lifecycle_bad", _failing_task, {})

        status = await _wait_for_terminal_status(factory, "lifecycle_bad")

        assert status == "failed"
        with factory() as s:
            row = s.execute(
                select(TaskHistory).where(TaskHistory.name == "lifecycle_bad")
            ).scalars().one()
        # The result JSON still carries a normalised summary shape.
        result = json.loads(row.result)
        assert "summary" in result
        assert "new_novels_count" in result
    finally:
        tm.stop()


async def test_duplicate_guard_releases_after_completion(factory):
    """After a task finishes, the same name may be enqueued again."""
    tm = TaskManagerSystem(session_factory=factory, client=None)
    tm.start()
    try:
        tm.run_task("rerun_task", _success_task, {})
        await _wait_for_terminal_status(factory, "rerun_task")

        # Must NOT raise — the first run reached a terminal state.
        tm.run_task("rerun_task", _success_task, {})
        status = await _wait_for_terminal_status(factory, "rerun_task")
        assert status == "success"
    finally:
        tm.stop()


async def test_different_tasks_run_serially_not_concurrently(factory):
    """Global task lock: two differently-named tasks must never overlap.

    Without serialization, AsyncIOScheduler runs each enqueued job as an
    independent coroutine, so different-named tasks execute concurrently
    (the ``pending`` phase is too transient to see and the shorter task
    can finish before the longer one).  The process-wide ``_task_lock``
    makes execution strictly serial: while one task holds the lock the
    other waits with its history row still ``pending``.
    """
    state = {"in_flight": 0, "max_concurrent": 0}
    state_lock = asyncio.Lock()

    async def _probe(**kwargs):
        async with state_lock:
            state["in_flight"] += 1
            state["max_concurrent"] = max(
                state["max_concurrent"], state["in_flight"]
            )
        # Force an overlap window: if both ran concurrently the peak
        # in_flight would reach 2 during this sleep.
        await asyncio.sleep(0.15)
        async with state_lock:
            state["in_flight"] -= 1
        return TaskResult(summary="ok")

    tm = TaskManagerSystem(session_factory=factory, client=None)
    tm.start()
    try:
        tm.run_task("probe_a", _probe, {})
        tm.run_task("probe_b", _probe, {})

        for _ in range(100):
            with factory() as s:
                rows = s.execute(
                    select(TaskHistory).where(
                        TaskHistory.name.in_(("probe_a", "probe_b"))
                    )
                ).scalars().all()
            if len(rows) == 2 and all(
                r.status not in ("pending", "running") for r in rows
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("probe tasks never reached terminal status")

        assert state["max_concurrent"] == 1
    finally:
        tm.stop()
