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
from copixiv.tasks.kernel import TaskHistoryRecorder, TaskManagerSystem

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


# ---------------------------------------------------------------------------
# Queue lanes: fetching tasks must not block local work
# ---------------------------------------------------------------------------


async def test_fetch_task_does_not_block_a_local_task(session_factory):
    """Two lanes over one queue: a long fetch must not starve an export.

    Under a single worker a 30-minute ranking run delayed every export behind
    it.  The queue stays FIFO *per lane*, so a local task enqueued behind a
    running fetch starts immediately instead of waiting for it.
    """
    manager = TaskManagerSystem(session_factory=session_factory)
    order: list[str] = []
    fetch_started = asyncio.Event()

    async def fetch(ctx):
        fetch_started.set()
        await asyncio.sleep(0.5)          # long "network" work
        order.append("fetch")

    async def local(ctx):
        order.append("local")

    try:
        await manager.start()
        # ``novel_ranking`` is a fetching function name → fetch lane.
        fetch_id = manager.run_task("novel_ranking", func=fetch)
        await asyncio.wait_for(fetch_started.wait(), timeout=2)
        local_id = manager.run_task("batch_export", func=local)

        await _wait_terminal(session_factory, [local_id], timeout=3)
        assert order == ["local"], "the local task must not wait for the fetch"

        await _wait_terminal(session_factory, [fetch_id], timeout=5)
        assert sorted(order) == ["fetch", "local"]
    finally:
        await manager.stop()


async def test_rows_within_one_lane_stay_serialized(session_factory):
    """Splitting the queue must not introduce intra-lane concurrency."""
    manager = TaskManagerSystem(session_factory=session_factory)
    state = {"active": 0, "max_active": 0}

    async def fetch(ctx):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1

    task_ids = [
        manager.run_task(f"novel_fetch_{i}", func=fetch) for i in range(3)
    ]
    try:
        await manager.start()
        await _wait_terminal(session_factory, task_ids)
        assert state["max_active"] == 1
    finally:
        await manager.stop()


async def test_general_lane_skips_fetch_rows_in_sql(session_factory):
    """The general lane must not stall behind a pending fetch row.

    A post-filter would have been enough to *avoid* the wrong row, but it also
    stops the lane whenever the oldest pending row belongs to the other lane —
    so the exclusion has to be in the query.
    """
    from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
    from copixiv.tasks.kernel import FETCH_TASK_PREFIXES

    fetch_id = _add_pending_row(
        session_factory, name="fetch", task_func="novel_ranking", arguments="{}",
    )
    local_id = _add_pending_row(
        session_factory, name="local", task_func="batch_export", arguments="{}",
    )

    with session_factory() as session:
        repo = SQLAlchemyTaskRepository(session)
        general = repo.get_next_pending_sync(
            exclude_funcs=list(FETCH_TASK_PREFIXES),
        )
        assert general is not None and general.id == local_id
        assert local_id > fetch_id
        fetch = repo.get_next_pending_sync(
            task_funcs=list(FETCH_TASK_PREFIXES),
        )
        assert fetch is not None and fetch.id == fetch_id


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


# ---------------------------------------------------------------------------
# 7: JSONB encoding + progress-only writes
# ---------------------------------------------------------------------------


async def test_recovered_row_stores_result_as_jsonb_object(session_factory):
    """``recover_stale`` must write a JSON *object*, not a JSON string.

    It bypasses ``update_task_sync`` (which parses a string back into a dict),
    so a ``json.dumps`` payload lands as ``jsonb_typeof = 'string'`` and
    ``result->>'summary'`` returns NULL — the UI shows an empty summary.
    """
    stale_id = _add_pending_row(
        session_factory, name="stale", task_func="x", arguments="{}",
        status="running",
    )

    recorder = TaskHistoryRecorder(session_factory)
    recorder.recover_stale()

    with session_factory() as session:
        row = session.get(TaskHistory, stale_id)
        assert row.status == "interrupted"
        assert isinstance(row.result, dict), f"expected dict, got {type(row.result)}"
        assert row.result["summary"]


async def test_progress_write_does_not_change_status(session_factory):
    """A progress-only write must never resurrect a finished row.

    The batch tasks report progress via a fire-and-forget
    ``run_coroutine_threadsafe``, so a late progress write can land after the
    executor recorded success.  Writing ``"running"`` there left the row
    running forever (and its button disabled).
    """
    from datetime import datetime, timezone

    with session_factory() as session:
        row = TaskHistory(
            name="finished", task_func="x", arguments="{}",
            status="success", start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        task_id = row.id

    recorder = TaskHistoryRecorder(session_factory)
    import asyncio
    asyncio.get_event_loop_policy()  # noqa: B018  (keeps import used)
    await recorder.update(task_id, None, progress="打包中 5/10 篇")

    with session_factory() as session:
        row = session.get(TaskHistory, task_id)
        assert row.status == "success", "progress write must not change status"
        assert row.end_time is not None, "end_time must survive"
        assert row.progress == "打包中 5/10 篇"


async def test_live_pending_is_pruned_and_does_not_shield_rows(session_factory):
    """Abandoned live-pending ids must not permanently escape recovery.

    ``recover_stale`` excludes ``_live_pending``.  Ids only left that set on a
    status write, so an enqueued-but-never-started row stayed excluded forever
    — it would sit ``pending`` while every recovery pass skipped it.
    """
    manager = TaskManagerSystem(session_factory=session_factory)

    async def noop(ctx):
        pass

    abandoned = manager.run_task("a4_abandoned", func=noop)
    assert abandoned in manager._recorder._live_pending

    # Simulate the row being resolved by someone else (never picked up by the
    # worker) — the id is now stale bookkeeping.
    with session_factory() as session:
        row = session.get(TaskHistory, abandoned)
        row.status = "interrupted"
        session.commit()

    manager._recorder.recover_stale()

    assert abandoned not in manager._recorder._live_pending, (
        "pruning must drop ids whose rows are no longer pending"
    )


async def test_stop_clears_adhoc_specs(session_factory):
    """Ad-hoc specs are an enqueue→run handoff, not a lifetime registry."""
    manager = TaskManagerSystem(session_factory=session_factory)

    async def noop(ctx):
        pass

    manager.run_task("a4_stop_adhoc", func=noop)
    assert manager._adhoc_specs

    try:
        await manager.start()
    finally:
        await manager.stop()

    assert manager._adhoc_specs == {}
    assert manager._recorder._live_pending == set()
