"""Behavioral tests for the in-memory cron ticker (A3: croniter + tick loop).

The old tests inspected ``scheduler.get_jobs()`` (an implementation
detail).  These pin behaviour instead:

1. ``next_occurrence`` computes the next cron occurrence in local time and
   rejects invalid expressions.
2. A fired cron row enqueues under its *display* name while resolving the
   task function by the registered *function* name.
3. Disabled rows and unknown functions never fire.
4. A missed window (>60s late) is skipped with a warning, never replayed.
5. A repeated tick with the same ``now`` does not double-fire.
6. Reloading preserves ``next_run`` while the cron expression is unchanged.

Timezone note: the previous scheduler treated 5-field crontabs as local
time (the server is CST), and A3 preserves that.  Tests use explicit aware
datetimes in the process-local timezone so they assert local-time behaviour
rather than UTC.
"""

import asyncio
from datetime import datetime

import pytest

from copixiv.db.models import ScheduledTask, TaskHistory
from copixiv.log import capture_logs
from copixiv.tasks.kernel import (
    CRON_MISFIRE_GRACE_SECONDS,
    CRON_TIMEZONE,
    CronTicker,
    TaskManagerSystem,
    discover_tasks,
    get_spec,
    next_occurrence,
)

DISPLAY_NAME = "我的自定义任务（早上三点）"
FUNCTION_NAME = "check_epub"

# Explicit aware datetimes in the process-local timezone (a fixed offset on
# this machine; CRON_TIMEZONE captures the same local offset).
LOCAL_TZ = CRON_TIMEZONE


def _local(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A 2026-05-* aware datetime in the process-local timezone."""
    return datetime(2026, 5, day, hour, minute, second, tzinfo=LOCAL_TZ)


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


def _ensure_task_registered() -> None:
    if get_spec(FUNCTION_NAME) is None:
        discover_tasks()


def _seed(
    session_factory,
    *,
    name: str = DISPLAY_NAME,
    task: str = FUNCTION_NAME,
    cron: str = "0 3 * * *",
    params: dict | None = None,
    is_enabled: bool = True,
) -> int:
    with session_factory() as session:
        row = ScheduledTask(
            name=name,
            task=task,
            cron=cron,
            params=params if params is not None else {"tag": "x"},
            is_enabled=is_enabled,
        )
        session.add(row)
        session.commit()
        return row.id


class _EnqueueRecorder:
    """Small callable matching ``manager.run_scheduled``'s signature."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, display_name, func_name, params=None):
        self.calls.append((display_name, func_name, params))


# ---------------------------------------------------------------------------
# next_occurrence — pure function
# ---------------------------------------------------------------------------


class TestNextOccurrence:
    def test_daily_same_day(self):
        after = datetime(2026, 5, 12, 2, 59, tzinfo=LOCAL_TZ)
        assert next_occurrence("0 3 * * *", after) == datetime(
            2026, 5, 12, 3, 0, tzinfo=LOCAL_TZ
        )

    def test_daily_rolls_to_next_day_after_the_occurrence(self):
        after = datetime(2026, 5, 12, 3, 1, tzinfo=LOCAL_TZ)
        assert next_occurrence("0 3 * * *", after) == datetime(
            2026, 5, 13, 3, 0, tzinfo=LOCAL_TZ
        )

    def test_monthly_rolls_to_next_month(self):
        after = datetime(2026, 1, 15, 12, 0, tzinfo=LOCAL_TZ)
        assert next_occurrence("30 2 1 * *", after) == datetime(
            2026, 2, 1, 2, 30, tzinfo=LOCAL_TZ
        )

    def test_aware_input_stays_aware_in_the_same_timezone(self):
        after = datetime(2026, 5, 12, 2, 59, tzinfo=LOCAL_TZ)
        nxt = next_occurrence("0 3 * * *", after)
        assert nxt.tzinfo is not None
        assert nxt.utcoffset() == LOCAL_TZ.utcoffset(None)

    def test_naive_input_is_treated_as_local_time(self):
        naive = datetime(2026, 5, 12, 2, 59)
        nxt = next_occurrence("0 3 * * *", naive)
        assert nxt.tzinfo is not None
        assert nxt.utcoffset() == LOCAL_TZ.utcoffset(None)
        assert nxt.replace(tzinfo=None) == datetime(2026, 5, 12, 3, 0)

    @pytest.mark.parametrize(
        "cron",
        [
            "not a cron",
            "0 3 * *",            # only 4 fields
            "0 0 3 * * *",        # 6 fields (croniter would accept seconds)
            "60 3 * * *",         # minute out of range
        ],
    )
    def test_invalid_expressions_raise_value_error(self, cron):
        with pytest.raises(ValueError):
            next_occurrence(cron, datetime(2026, 5, 12, 0, 0, tzinfo=LOCAL_TZ))


# ---------------------------------------------------------------------------
# Firing behaviour
# ---------------------------------------------------------------------------


class TestCronFiring:
    def test_fire_enqueues_display_and_function_name(self, session_factory):
        """Display name is the history key; function name resolves the spec."""
        _ensure_task_registered()
        _seed(session_factory)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))
        ticker._fire_due(_local(12, 3, 0, 5))

        assert recorder.calls == [
            (DISPLAY_NAME, FUNCTION_NAME, {"tag": "x"}),
        ]

    def test_disabled_row_is_not_loaded_and_never_fires(self, session_factory):
        _ensure_task_registered()
        _seed(session_factory, is_enabled=False)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))

        assert ticker._entries == {}
        ticker._fire_due(_local(13, 3, 0, 5))
        assert recorder.calls == []

    def test_unknown_function_warns_and_is_skipped(self, session_factory):
        _seed(session_factory, task="does_not_exist")

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        with capture_logs() as logs:
            ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))
            log_text = logs()

        assert ticker._entries == {}
        assert "unknown function" in log_text
        ticker._fire_due(_local(13, 3, 0, 5))
        assert recorder.calls == []

    def test_invalid_cron_row_warns_and_is_skipped(self, session_factory):
        _ensure_task_registered()
        _seed(session_factory, cron="not-a-cron")

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        with capture_logs() as logs:
            ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))
            log_text = logs()

        assert ticker._entries == {}
        assert "invalid cron" in log_text.lower()

    def test_missed_window_is_skipped_and_advanced(self, session_factory):
        """A >60s-late occurrence never enqueues; it advances past now."""
        _ensure_task_registered()
        _seed(session_factory)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))

        late_now = _local(12, 4, 0)
        with capture_logs() as logs:
            ticker._fire_due(late_now)
            log_text = logs().lower()

        assert recorder.calls == []
        assert "missed" in log_text
        assert "skipped" in log_text

        entry = next(iter(ticker._entries.values()))
        assert entry.next_run > late_now
        assert entry.next_run == _local(13, 3, 0)

    def test_grace_window_still_fires(self, session_factory):
        """Exactly 60s late is still on time (``late > grace`` skips)."""
        _ensure_task_registered()
        _seed(session_factory)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))
        ticker._fire_due(_local(12, 3, 1, 0))

        assert recorder.calls == [(DISPLAY_NAME, FUNCTION_NAME, {"tag": "x"})]
        assert CRON_MISFIRE_GRACE_SECONDS == 60

    def test_same_occurrence_does_not_double_fire(self, session_factory):
        _ensure_task_registered()
        _seed(session_factory)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))

        now = _local(12, 3, 0, 5)
        ticker._fire_due(now)
        ticker._fire_due(now)  # same tick re-entered / duplicate call

        assert len(recorder.calls) == 1

        # The next day's occurrence fires normally.
        ticker._fire_due(_local(13, 3, 0, 5))
        assert len(recorder.calls) == 2

    def test_enqueue_failure_does_not_kill_the_ticker(self, session_factory):
        _ensure_task_registered()
        _seed(session_factory)

        calls: list[int] = []

        def enqueue(display_name, func_name, params=None):
            calls.append(1)
            raise RuntimeError("db down")

        ticker = CronTicker(session_factory)
        ticker.load_cron_jobs(enqueue, now=_local(12, 2, 59))

        with capture_logs() as logs:
            ticker._fire_due(_local(12, 3, 0, 5))
            log_text = logs().lower()

        # The exception was contained and the entry still advanced.
        assert calls == [1]
        entry = next(iter(ticker._entries.values()))
        assert entry.next_run == _local(13, 3, 0)
        assert "failed to enqueue" in log_text


# ---------------------------------------------------------------------------
# Reload semantics
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_preserves_unchanged_next_run(self, session_factory):
        _ensure_task_registered()
        row_id = _seed(session_factory)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))
        original = ticker._entries[row_id].next_run
        assert original == _local(12, 3, 0)

        # An API edit that leaves the cron unchanged must not reset the timer.
        ticker.reload_cron_jobs(recorder, now=_local(12, 10, 0))
        assert ticker._entries[row_id].next_run == original

    def test_reload_recomputes_after_cron_change(self, session_factory):
        _ensure_task_registered()
        row_id = _seed(session_factory)

        ticker = CronTicker(session_factory)
        recorder = _EnqueueRecorder()
        ticker.load_cron_jobs(recorder, now=_local(12, 2, 59))
        assert ticker._entries[row_id].next_run == _local(12, 3, 0)

        with session_factory() as session:
            row = session.get(ScheduledTask, row_id)
            row.cron = "30 4 * * *"
            session.commit()

        ticker.reload_cron_jobs(recorder, now=_local(12, 10, 0))
        assert ticker._entries[row_id].next_run == _local(13, 4, 30)


# ---------------------------------------------------------------------------
# Manager lifecycle (async start/stop)
# ---------------------------------------------------------------------------


async def test_manager_start_stop_is_clean(session_factory):
    """start()/stop() must leave no ticker or worker task behind."""
    _ensure_task_registered()
    manager = TaskManagerSystem(session_factory=session_factory)

    await manager.start()
    assert manager._ticker._task is not None
    assert manager._worker_task is not None
    assert manager._loop is not None

    await manager.stop()
    assert manager._ticker._task is None
    assert manager._worker_task is None
    assert manager._loop is None
    assert [
        task for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ] == []


async def test_run_task_before_start_is_queued_then_runs(session_factory):
    """The sync enqueue API stays safe before the loop exists."""
    _ensure_task_registered()
    manager = TaskManagerSystem(session_factory=session_factory)

    async def noop(ctx):
        return None

    task_id = manager.run_task("test_noop", func=noop)
    with session_factory() as session:
        row = session.get(TaskHistory, task_id)
        assert row is not None
        assert row.status == "pending"

    try:
        await manager.start()
        for _ in range(250):
            with session_factory() as session:
                status = session.get(TaskHistory, task_id).status
            if status == "success":
                break
            await asyncio.sleep(0.02)
    finally:
        await manager.stop()

    with session_factory() as session:
        row = session.get(TaskHistory, task_id)
        assert row is not None
        assert row.status == "success"
