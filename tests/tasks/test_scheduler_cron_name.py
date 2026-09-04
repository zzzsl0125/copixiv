"""Regression: cron trigger must record the *display* name in history while
resolving the task function by its *function* name.

The frontend invites users to give a scheduled task a free-form display
name (``TaskEditModal.vue``).  Two requirements must both hold when a cron
job fires:

1. The history row is recorded under that display name (``task.name``) so
   the task-history list shows the user's label, not the function name.
2. The task function is resolved by ``task.task`` (the registered function
   name), so a custom display name can never break the registry lookup.

The earlier ``run_task_now`` path already did both.  The cron path
(enqueue = ``manager.run_scheduled``) must match it exactly — passing
``(display_name, function_name, params)`` to the enqueue callback.
"""

import pytest

from copixiv.db.models import ScheduledTask
from copixiv.tasks.kernel import discover_tasks, get_spec
from copixiv.tasks.kernel import CronScheduler

DISPLAY_NAME = "我的自定义任务（早上三点）"
FUNCTION_NAME = "check_epub"


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """Shared PG database, emptied before each test."""
    yield


def _ensure_task_registered() -> None:
    if get_spec(FUNCTION_NAME) is None:
        discover_tasks()


def test_trigger_passes_display_and_function_name_to_enqueue():
    """The fire-time callback enqueues (display_name, function_name, params).

    The display name is the history/recording key; the function name is the
    registry-resolution key.  Enqueue is ``manager.run_scheduled``, whose
    signature is ``(display_name, func_name, params)``.
    """
    _ensure_task_registered()
    scheduler = CronScheduler(session_factory=None)

    calls: list[tuple] = []

    def enqueue(display_name, func_name, params=None):
        calls.append((display_name, func_name, params))

    scheduler._trigger_cron_job(
        FUNCTION_NAME, DISPLAY_NAME, enqueue, {"tag": "x"}
    )

    # display name first (recorded in history), function name second
    # (resolves the task), params last.
    assert calls == [(DISPLAY_NAME, FUNCTION_NAME, {"tag": "x"})]


def test_cron_job_from_db_fires_with_display_name_recorded(session_factory):
    """A scheduled row with a custom display name still resolves on fire
    and records the display name (not the function name) for history."""
    _ensure_task_registered()

    with session_factory() as session:
        session.add(
            ScheduledTask(
                name=DISPLAY_NAME,
                task=FUNCTION_NAME,
                cron="0 3 * * *",
                params={"tag": "x"},
                is_enabled=True,
            )
        )
        session.commit()

    scheduler = CronScheduler(session_factory=session_factory)
    calls: list[tuple] = []

    def enqueue(display_name, func_name, params=None):
        calls.append((display_name, func_name, params))

    scheduler.load_cron_jobs(enqueue)

    cron_jobs = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("cron_")]
    assert len(cron_jobs) == 1

    # Simulate an APScheduler fire: the job calls func(*args).
    job = cron_jobs[0]
    job.func(*job.args)

    assert calls == [(DISPLAY_NAME, FUNCTION_NAME, {"tag": "x"})]
