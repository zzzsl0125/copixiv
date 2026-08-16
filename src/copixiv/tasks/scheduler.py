"""Cron scheduler — APScheduler ownership for the task kernel.

Lifecycle (start/stop), the scheduler-wide error listener, and cron jobs
derived from the ``scheduled_tasks`` table.  Actual execution is delegated
back to the manager through the *enqueue* callback, so this class knows
nothing about history rows or contexts.
"""

from __future__ import annotations

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from copixiv.domain.exceptions import TaskAlreadyRunningError
from copixiv.log import logger
from copixiv.tasks.history import TaskHistoryRecorder
from copixiv.tasks.registry import get_spec


class CronScheduler:
    """Owns the AsyncIOScheduler and the cron jobs built from the DB."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_listener(
            self._on_job_error,
            EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler (cron jobs are loaded separately)."""
        self.scheduler.start()

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        if self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("Error shutting down scheduler.")
        logger.info("CronScheduler stopped.")

    # ------------------------------------------------------------------
    # Cron job management
    # ------------------------------------------------------------------

    def reload_cron_jobs(self, enqueue) -> None:
        """Remove all cron jobs and reload from the database.

        *enqueue* is ``manager.run_task`` — the single execution entry
        point shared with manual runs.
        """
        for job in self.scheduler.get_jobs():
            if job.id.startswith("cron_"):
                job.remove()
        self.load_cron_jobs(enqueue)

    def load_cron_jobs(self, enqueue) -> None:
        """Read ``scheduled_tasks`` and register a cron job per enabled row."""
        from copixiv.infrastructure.repositories.task import (
            SQLAlchemyTaskRepository,
        )

        with self._session_factory() as session:
            try:
                tasks = SQLAlchemyTaskRepository(
                    session
                ).get_scheduled_tasks_sync()
            except Exception:
                logger.exception(
                    "Failed to load scheduled tasks from database."
                )
                return

            for task in tasks:
                if not task.is_enabled:
                    continue

                spec = get_spec(task.task)
                if spec is None:
                    logger.warning(
                        "Scheduled task '{}' references unknown function "
                        "'{}' — skipped.",
                        task.name,
                        task.task,
                    )
                    continue

                params = TaskHistoryRecorder.parse_params(task.params)

                try:
                    self.scheduler.add_job(
                        self._trigger_cron_job,
                        trigger=CronTrigger.from_crontab(task.cron),
                        id=f"cron_{task.id}",
                        args=(task.name, enqueue, params),
                        replace_existing=True,
                        max_instances=1,
                        misfire_grace_time=60,
                    )
                    logger.info(
                        "Registered cron job: {} (id={}, cron='{}')",
                        task.name,
                        task.id,
                        task.cron,
                    )
                except Exception:
                    logger.exception(
                        "Failed to register cron job '%s' (id=%s).",
                        task.name,
                        task.id,
                    )

    def _trigger_cron_job(self, name: str, enqueue, params: dict) -> None:
        """Fires when a cron trigger is hit.  Enqueues via the manager.

        A cron firing while a manual run of the same task is still active
        is skipped (the duplicate-run guard raises) instead of stacking a
        concurrent duplicate.
        """
        logger.info("Cron triggered: {}", name)
        try:
            enqueue(name, params=params)
        except TaskAlreadyRunningError:
            logger.warning(
                "Cron for '{}' skipped — a run is already pending/running.",
                name,
            )

    # ------------------------------------------------------------------
    # APScheduler error listener
    # ------------------------------------------------------------------

    def _on_job_error(self, event) -> None:
        code_names = {
            EVENT_JOB_ERROR: "JOB_ERROR",
            EVENT_JOB_MISSED: "JOB_MISSED",
            EVENT_JOB_MAX_INSTANCES: "JOB_MAX_INSTANCES",
        }
        code_label = code_names.get(event.code, f"UNKNOWN({event.code})")
        logger.error(
            "APScheduler event: type={}, job_id={}, exception={}, "
            "scheduled_run_time={}",
            code_label,
            event.job_id,
            getattr(event, "exception", None),
            getattr(event, "scheduled_run_time", None),
        )
