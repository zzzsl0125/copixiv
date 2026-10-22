"""Behavioural tests for the single queue worker (A4).

``task_history`` *is* the queue: ``enqueue`` inserts a ``pending`` row and
wakes one worker coroutine, which repeatedly takes the oldest pending row
(``ORDER BY id LIMIT 1``) and runs it to completion.  There is no lock and
no per-enqueue asyncio task — serialization is structural.

Pinned here:

1. FIFO order — two enqueues complete in enqueue order.
2. Nothing lost — every enqueued row runs, even with a batch of 5.
3. Structural serialization — task bodies never overlap (max_active == 1).
4. Crash recovery — leftover pending/running rows become ``interrupted``
   before the worker starts and are never executed.
5. Params parsing — invalid TEXT arguments fail the row with a clear
   summary (the body never runs); unknown task functions fail too.
6. Pre-start enqueue — a row inserted before ``start()`` is left pending
   and the worker runs it once started.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from copixiv.db.models import TaskHistory
from copixiv.tasks import kernel
from copixiv.tasks.kernel import TaskManagerSystem

TERMINAL = ("success", "failed", "interrupted")


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


def _statuses(session_factory, task_ids: list[int]) -> list[str | None]:
    statuses: list[str | None] = []
    with session_factory() as session:
        for task_id in task_ids:
            row = session.get(TaskHistory, task_id)
            statuses.append(row.status if row is not None else None)
    return statuses


def _summary(session_factory, task_id: int) -> str:
    with session_factory() as session:
        row = session.get(TaskHistory, task_id)
        assert row is not None
        result = row.result if isinstance(row.result, dict) else {}
        return str(result.get("summary", ""))


async def _wait_terminal(
    session_factory, task_ids: list[int], *, timeout: float = 5.0
) -> list[str | None]:
    """Poll the DB until every row is terminal (or fail after *timeout*)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    statuses = _statuses(session_factory, task_ids)
    while loop.time() < deadline:
        statuses = _statuses(session_factory, task_ids)
        if all(status in TERMINAL for status in statuses):
            return statuses
        await asyncio.sleep(0.02)
    raise AssertionError(f"tasks did not finish in {timeout}s: {statuses}")


def _add_pending_row(
    session_factory,
    *,
    name: str,
    task_func: str | None,
    arguments: str | None,
    status: str = "pending",
) -> int:
    with session_factory() as session:
        row = TaskHistory(
            name=name,
            task_func=task_func,
            arguments=arguments,
            status=status,
            start_time=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        return row.id


def _append_after_delay(target: list[str], value: str, delay: float = 0.05):
    async def run(ctx):
        await asyncio.sleep(delay)
        target.append(value)
    return run


# ---------------------------------------------------------------------------
# 1 + 2: order and no loss
# ---------------------------------------------------------------------------


async def test_fifo_order_of_enqueued_runs(session_factory):
    """Two pre-start enqueues complete in enqueue (id) order."""
    manager = TaskManagerSystem(session_factory=session_factory)
    order: list[str] = []

    first = manager.run_task(
        "a4_first", func=_append_after_delay(order, "first"),
    )
    second = manager.run_task(
        "a4_second", func=_append_after_delay(order, "second"),
    )
    assert _statuses(session_factory, [first, second]) == ["pending", "pending"]

    try:
        await manager.start()
        statuses = await _wait_terminal(session_factory, [first, second])
        assert statuses == ["success", "success"]
        assert order == ["first", "second"]
    finally:
        await manager.stop()


async def test_no_enqueued_run_is_lost(session_factory):
    """A batch of 5 enqueues all execute (queue never drops a row)."""
    manager = TaskManagerSystem(session_factory=session_factory)
    executed: list[int] = []

    def make(index: int):
        async def run(ctx):
            await asyncio.sleep(0.01)
            executed.append(index)
        return run

    task_ids = [
        manager.run_task(f"a4_batch_{index}", func=make(index))
        for index in range(5)
    ]

    try:
        await manager.start()
        statuses = await _wait_terminal(session_factory, task_ids)
        assert statuses == ["success"] * 5
        assert executed == [0, 1, 2, 3, 4]
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# 3: serialization is structural
# ---------------------------------------------------------------------------


async def test_task_bodies_never_overlap(session_factory):
    """One worker means max_active == 1, even with 3 queued rows."""
    manager = TaskManagerSystem(session_factory=session_factory)
    state = {"active": 0, "max_active": 0}

    async def run(ctx):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1

    task_ids = [
        manager.run_task(f"a4_serial_{index}", func=run) for index in range(3)
    ]

    try:
        await manager.start()
        statuses = await _wait_terminal(session_factory, task_ids)
        assert statuses == ["success"] * 3
        assert state["max_active"] == 1
        assert state["active"] == 0
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# 4: crash recovery
# ---------------------------------------------------------------------------


async def test_stale_pending_and_running_rows_are_interrupted(session_factory):
    """Recovery runs before the worker: leftovers are interrupted, not run."""
    calls: list[int] = []

    @kernel.register("a4_recover_probe")
    async def probe(ctx):
        calls.append(1)

    try:
        pending_id = _add_pending_row(
            session_factory, name="probe-pending",
            task_func="a4_recover_probe", arguments="not json",
        )
        running_id = _add_pending_row(
            session_factory, name="probe-running",
            task_func="a4_recover_probe", arguments=json.dumps({}),
            status="running",
        )

        manager = TaskManagerSystem(session_factory=session_factory)
        try:
            await manager.start()
            # Recovery is synchronous inside start(); the worker has not run
            # yet, so this is the immediate post-start state.
            assert _statuses(
                session_factory, [pending_id, running_id]
            ) == ["interrupted", "interrupted"]
            # Give a (broken) worker a chance to run them.
            await asyncio.sleep(0.2)
            assert _statuses(
                session_factory, [pending_id, running_id]
            ) == ["interrupted", "interrupted"]
            assert calls == []
        finally:
            await manager.stop()

        with session_factory() as session:
            assert session.query(TaskHistory).filter(
                TaskHistory.status.in_(("pending", "running"))
            ).count() == 0
    finally:
        kernel.unregister("a4_recover_probe")


# ---------------------------------------------------------------------------
# 5: TEXT params parsing
# ---------------------------------------------------------------------------


async def test_invalid_arguments_fail_the_row_without_running(session_factory):
    """Invalid JSON arguments fail the row; the task body never runs."""
    calls: list[int] = []

    @kernel.register("a4_params_probe")
    async def probe(ctx):
        calls.append(1)

    manager = TaskManagerSystem(session_factory=session_factory)
    try:
        await manager.start()
        # Inserted after start() (recovery already ran) without waking the
        # worker: the 1s polling fallback must pick it up.
        task_id = _add_pending_row(
            session_factory, name="bad-params",
            task_func="a4_params_probe", arguments="not json",
        )
        statuses = await _wait_terminal(session_factory, [task_id])
        assert statuses == ["failed"]
        assert "参数" in _summary(session_factory, task_id)
        assert calls == []
    finally:
        await manager.stop()
        kernel.unregister("a4_params_probe")


async def test_unknown_task_func_fails_the_row(session_factory):
    """A row whose task_func is not registered fails with a clear summary."""
    manager = TaskManagerSystem(session_factory=session_factory)
    try:
        await manager.start()
        task_id = _add_pending_row(
            session_factory, name="ghost",
            task_func="a4_missing_function", arguments=json.dumps({}),
        )
        statuses = await _wait_terminal(session_factory, [task_id])
        assert statuses == ["failed"]
        assert "未知任务函数" in _summary(session_factory, task_id)
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# 6: enqueue before start()
# ---------------------------------------------------------------------------


async def test_enqueue_before_start_runs_after_start(session_factory):
    """A pre-start enqueue stays pending and the worker runs it on start()."""
    manager = TaskManagerSystem(session_factory=session_factory)
    ran: list[int] = []

    async def run(ctx):
        ran.append(1)

    task_id = manager.run_task("a4_prestart", func=run)
    assert _statuses(session_factory, [task_id]) == ["pending"]

    try:
        await manager.start()
        statuses = await _wait_terminal(session_factory, [task_id])
        assert statuses == ["success"]
        assert ran == [1]
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# Sync API enqueue path: threadpool -> call_soon_threadsafe wake-up
# ---------------------------------------------------------------------------


async def test_enqueue_from_threadpool_is_picked_up(session_factory):
    """Sync endpoints run in a threadpool; the wake-up must cross threads."""
    manager = TaskManagerSystem(session_factory=session_factory)
    ran: list[int] = []

    async def run(ctx):
        ran.append(1)

    try:
        await manager.start()
        task_id = await asyncio.to_thread(
            manager.run_task, "a4_threaded", run,
        )
        statuses = await _wait_terminal(session_factory, [task_id])
        assert statuses == ["success"]
        assert ran == [1]
    finally:
        await manager.stop()
