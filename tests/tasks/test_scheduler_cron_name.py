"""Regression: cron trigger must resolve the *function* name, not the
display name.

The frontend invites users to give a scheduled task a free-form display
name (``TaskEditModal.vue``).  The old code re-resolved the registry by
that display name at fire time, so any custom name raised
``ValidationError`` inside APScheduler and the job failed silently with
no history row.  ``run_task_now`` always used the function name — the
cron path now matches it.
"""

from copixiv.infrastructure.database.models import ScheduledTask
from copixiv.tasks.registry import discover_tasks, get_spec
from copixiv.tasks.scheduler import CronScheduler

DISPLAY_NAME = "我的自定义任务（早上三点）"
FUNCTION_NAME = "check_epub"


def _ensure_task_registered() -> None:
    if get_spec(FUNCTION_NAME) is None:
        discover_tasks()


def test_trigger_passes_function_name_to_enqueue():
    """The fire-time callback enqueues by function name, not display name."""
    _ensure_task_registered()
    scheduler = CronScheduler(session_factory=None)

    calls: list[tuple] = []

    def enqueue(name, params=None):
        calls.append((name, params))

    scheduler._trigger_cron_job(
        FUNCTION_NAME, DISPLAY_NAME, enqueue, {"tag": "x"}
    )

    assert calls == [(FUNCTION_NAME, {"tag": "x"})]


def test_cron_job_from_db_fires_with_function_name(file_session_factory):
    """A scheduled row with a custom display name still resolves on fire."""
    _ensure_task_registered()

    with file_session_factory() as session:
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

    scheduler = CronScheduler(session_factory=file_session_factory)
    calls: list[tuple] = []

    def enqueue(name, params=None):
        calls.append((name, params))

    scheduler.load_cron_jobs(enqueue)

    cron_jobs = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("cron_")]
    assert len(cron_jobs) == 1

    # Simulate an APScheduler fire: the job calls func(*args).
    job = cron_jobs[0]
    job.func(*job.args)

    assert calls == [(FUNCTION_NAME, {"tag": "x"})]
