"""Task manager — cron scheduler + manual execution for background tasks.

Port of V1's ``core/task_manager.py`` adapted to V2's dependency-injection
architecture.  Uses APScheduler's AsyncIOScheduler on the main event loop.

Tasks return :class:`TaskResult` so the manager (and downstream notifier)
knows whether a task discovered novels or performed maintenance — no more
guessing based on ``isinstance(result, list)``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MISSED,
    EVENT_JOB_MAX_INSTANCES,
)

from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.exceptions import TaskAlreadyRunningError
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.tasks.registry import get_task
import copixiv.tasks.novel_tasks  # ensure @register decorators fire  # noqa: F401

from copixiv.app.logger import logger, capture_logs


class TaskManagerSystem:
    """Manages background tasks and scheduled cron jobs via APScheduler.

    On ``start()``, reads ``scheduled_tasks`` from the database and registers
    a cron job for each enabled row.  Manual runs (via the API) are also
    dispatched through this manager so that history is recorded consistently.

    Dependencies (PixivClient, FileStorage, etc.) are captured at construction
    time and injected into task functions automatically by matching parameter
    names.  Only dependencies that a task actually declares are injected —
    there is no ``**kwargs`` catch-all in task signatures, so a typo in a
    dependency name is a hard error instead of silent failure.
    """

    def __init__(
        self,
        session_factory,
        client=None,
        file_storage=None,
        image_downloader=None,
        epub_builder=None,
        config=None,
        notifier=None,
    ):
        from copixiv.infrastructure.database.write_lock import DbWriteLock

        self._session_factory = session_factory
        self._notifier = notifier
        self._deps = {
            "client": client,
            "file_storage": file_storage,
            "image_downloader": image_downloader,
            "epub_builder": epub_builder,
            "config": config,
            "write_lock": DbWriteLock(),
        }
        # Strip Nones — tasks that don't need a dep simply don't declare it.
        self._deps = {k: v for k, v in self._deps.items() if v is not None}

        # In-process duplicate-run guard (task name → in-flight).
        # Checked-and-set inside the sync run_task(), so it is atomic on
        # the event loop; cleared by the wrapper's ``finally``.
        self._running_names: set[str] = set()

        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_listener(
            self._on_job_error,
            EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler, recover stale tasks, load cron jobs."""
        self.scheduler.start()
        self._recover_stale_tasks()
        self._load_cron_jobs()
        logger.info("TaskManagerSystem started — cron jobs loaded.")

    def _recover_stale_tasks(self) -> None:
        """Mark tasks stuck in pending/running as interrupted.

        A process crash (or ``stop()`` with ``wait=False``) leaves
        ``task_history`` rows in pending/running forever — without this,
        the UI shows them as eternally running and the duplicate-run
        guard keeps treating the task name as busy.
        """
        from sqlalchemy import select as _select, update as _update
        from copixiv.infrastructure.database import models

        try:
            with self._session_factory() as session:
                stale_ids = session.execute(
                    _select(models.TaskHistory.id).where(
                        models.TaskHistory.status.in_(("pending", "running"))
                    )
                ).scalars().all()
                if not stale_ids:
                    return
                session.execute(
                    _update(models.TaskHistory)
                    .where(models.TaskHistory.id.in_(stale_ids))
                    .values(
                        status="interrupted",
                        end_time=datetime.now().astimezone().isoformat(),
                        result=json.dumps(
                            {"summary": "服务重启导致中断"}, ensure_ascii=False,
                        ),
                    )
                )
                session.commit()
                logger.warning(
                    "Recovered {} stale task history rows → interrupted.",
                    len(stale_ids),
                )
        except Exception:
            logger.exception("Failed to recover stale task history rows.")

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        if self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("Error shutting down scheduler.")
        logger.info("TaskManagerSystem stopped.")

    # ------------------------------------------------------------------
    # Cron job management
    # ------------------------------------------------------------------

    def reload_cron_jobs(self) -> None:
        """Remove all cron jobs and reload from the database."""
        for job in self.scheduler.get_jobs():
            if job.id.startswith("cron_"):
                job.remove()
        self._load_cron_jobs()

    def _load_cron_jobs(self) -> None:
        """Read ``scheduled_tasks`` table and register cron jobs."""
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository

        with self._session_factory() as session:
            try:
                tasks = SQLAlchemyTaskRepository(session).get_scheduled_tasks_sync()
            except Exception:
                logger.exception("Failed to load scheduled tasks from database.")
                return

            for task in tasks:
                if not task.is_enabled:
                    continue

                func = get_task(task.task)
                if func is None:
                    logger.warning(
                        "Scheduled task '{}' references unknown function '{}' — skipped.",
                        task.name,
                        task.task,
                    )
                    continue

                params = self._parse_json(task.params)

                try:
                    self.scheduler.add_job(
                        self._trigger_cron_job,
                        trigger=CronTrigger.from_crontab(task.cron),
                        id=f"cron_{task.id}",
                        args=(task.name, func, params),
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

    def _trigger_cron_job(
        self, name: str, func: Callable, params: dict
    ) -> None:
        """Fires when a cron trigger is hit.  Enqueues via :meth:`run_task`.

        A cron firing while a manual run of the same task is still active
        is skipped (the duplicate-run guard raises) instead of stacking a
        concurrent duplicate.
        """
        logger.info("Cron triggered: {}", name)
        try:
            self.run_task(name, func, params)
        except TaskAlreadyRunningError:
            logger.warning(
                "Cron for '{}' skipped — a run is already pending/running.",
                name,
            )

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    def run_task(
        self,
        name: str,
        func: Callable,
        params: dict | None = None,
    ) -> None:
        """Enqueue a task for immediate background execution.

        This is the entry point for both cron-triggered and manually-triggered
        runs.  The task is scheduled as a one-shot APScheduler job so that
        history recording and error handling happen in the wrapper.

        Raises:
            TaskAlreadyRunningError: If the same task name already has a
                pending/running history row (or is in-flight in-process).
        """
        params = params or {}

        # Duplicate-run guard: in-process set (atomic on the event loop —
        # run_task is sync) + DB check (covers rows from previous process
        # lifetimes).  Without this, "manual run + cron" or double clicks
        # execute the same task concurrently.
        if name in self._running_names:
            raise TaskAlreadyRunningError(
                f"Task '{name}' is already running in this process."
            )
        with self._session_factory() as session:
            from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository
            repo = SQLAlchemyTaskRepository(session)
            busy = repo.has_pending_or_running(name)
            if busy:
                raise TaskAlreadyRunningError(
                    f"Task '{name}' is already pending or running."
                )
            # Short INSERT outside db_write() — task enqueue happens in
            # sync API paths; 60s busy_timeout covers the rare collision.
            task_id = repo.add_task_sync(name, params)
            session.commit()
            logger.info("Task '{}' (id={}) enqueued.", name, task_id)

        self._running_names.add(name)
        self.scheduler.add_job(
            self._run_task_wrapper,
            args=(task_id, name, func, params),
            id=f"manual_{task_id}",
            max_instances=1,
        )

    def run_task_now(self, task_id: int) -> None:
        """Look up a scheduled task by DB id and run it immediately.

        Used by the ``POST /api/tasks/scheduled/{id}/run`` endpoint.

        Raises:
            NotFoundError: unknown scheduled-task id.
            ValidationError: the scheduled task references an unknown
                task function.
        """
        from copixiv.domain.exceptions import NotFoundError, ValidationError
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository

        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            tasks = repo.get_scheduled_tasks_sync()
            task = next((t for t in tasks if t.id == task_id), None)
            if task is None:
                raise NotFoundError(f"Scheduled task id={task_id} not found.")

            func = get_task(task.task)
            if func is None:
                raise ValidationError(f"Unknown task function: {task.task}")

            params = self._parse_json(task.params)

        self.run_task(task.name, func, params)

    # ------------------------------------------------------------------
    # Internal: wrapper that records history + injects deps
    # ------------------------------------------------------------------

    async def _run_task_wrapper(
        self,
        task_id: int,
        name: str,
        func: Callable,
        params: dict,
    ) -> None:
        """Execute a task, record history, and send notifications."""
        logger.info("Starting task '{}' (id={})...", name, task_id)
        start_time = time.time()
        status: str = "running"
        error_msg: str | None = None
        result_val: Any = None

        try:
            await self._run_task_inner(
                task_id, name, func, params,
            )
            return
        finally:
            # Release the in-process duplicate-run guard in every exit
            # path (success / failure / cancellation).
            self._running_names.discard(name)

    async def _run_task_inner(
        self,
        task_id: int,
        name: str,
        func: Callable,
        params: dict,
    ) -> None:
        """Body of the task wrapper — status tracking + execution."""
        start_time = time.time()
        status: str = "running"
        error_msg: str | None = None
        result_val: Any = None

        # --- Update status to "running" ---
        await self._update_history(task_id, "running")

        # Capture all loguru output during task execution
        with capture_logs(task_id=task_id) as get_logs:
            try:
                # Build a Unit of Work and inject matching dependencies
                uow = SqlUnitOfWork(self._session_factory)

                sig = inspect.signature(func)
                injected: dict[str, Any] = {}
                for dep_name, dep_value in self._deps.items():
                    if dep_name in sig.parameters:
                        injected[dep_name] = dep_value
                if "uow" in sig.parameters:
                    injected["uow"] = uow

                result_val = await self._execute_func(func, params, injected)
                status = "success"
                logger.info("Task '{}' (id={}) completed successfully.", name, task_id)

            except (asyncio.TimeoutError, TimeoutError):
                status = "failed"
                error_msg = "Task timed out after 30 minutes"
                logger.error("Task '{}' (id={}) timed out.", name, task_id)
            except asyncio.CancelledError:
                # Cancellation (e.g. scheduler shutdown) is BaseException —
                # without this branch the row stays "running" forever.
                # Record the interruption best-effort, then re-raise so
                # cancellation semantics are preserved.
                duration = time.time() - start_time
                try:
                    await self._update_history(
                        task_id, "interrupted",
                        result=json.dumps(
                            {
                                "log": get_logs(),
                                "summary": "任务被中断（服务关闭/重启）",
                                "new_novels_count": 0,
                                "new_novel_titles": [],
                            },
                            ensure_ascii=False,
                        ),
                        duration=duration,
                    )
                except Exception:
                    logger.exception(
                        "Failed to record interrupted status for task '{}' "
                        "(id={}).", name, task_id,
                    )
                raise
            except Exception as exc:
                status = "failed"
                error_msg = str(exc)
                logger.error("Task '{}' (id={}) failed: {}", name, task_id, exc)
                logger.error(traceback.format_exc())

            log_output = get_logs()

        duration = time.time() - start_time
        result = self._normalize_result(result_val)

        result_data = json.dumps(
            {
                "log": log_output,
                "summary": result.summary,
                "new_novels_count": result.new_novel_count,
                "new_novel_titles": result.new_novel_titles,
            },
            ensure_ascii=False,
        )

        await self._update_history(task_id, status, result=result_data, duration=duration)

        # --- Telegram notification ---
        if self._notifier is not None:
            await self._notifier.send_task_result(
                task_name=name,
                status=status,
                duration=duration,
                error=error_msg,
                result=result,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _execute_func(
        func: Callable, params: dict, deps: dict
    ) -> Any:
        """Execute *func* with a 30-minute timeout."""
        if inspect.iscoroutinefunction(func):
            return await asyncio.wait_for(
                func(**params, **deps), timeout=1800
            )
        else:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return await asyncio.wait_for(
                    loop.run_in_executor(pool, lambda: func(**params, **deps)),
                    timeout=1800,
                )

    @staticmethod
    def _normalize_result(result_val: Any) -> TaskResult:
        """Convert a task's return value into a :class:`TaskResult`.

        Registered tasks all return ``TaskResult``; ``None`` and any other
        value are mapped to a generic summary as a safety net.
        """
        if isinstance(result_val, TaskResult):
            return result_val
        if result_val is None:
            return TaskResult(summary="完成")
        return TaskResult(summary=str(result_val))

    async def _update_history(
        self,
        task_id: int,
        status: str,
        result: str | None = None,
        duration: float | None = None,
    ) -> None:
        """Update a TaskHistory row inside the global write lock.

        task_history is written from paths that may overlap with other
        tasks' writes — serialize it through ``db_write()`` like every
        other database write.
        """
        from copixiv.infrastructure.repositories.task import SQLAlchemyTaskRepository
        from copixiv.infrastructure.database.write_lock import db_write

        async with db_write():
            with self._session_factory() as session:
                repo = SQLAlchemyTaskRepository(session)
                repo.update_task_sync(
                    task_id, status, result=result, duration=duration,
                )
                session.commit()

    @staticmethod
    def _parse_json(value: Any) -> dict:
        """Parse a JSON string or return the dict as-is.

        Malformed JSON is a configuration error — warn loudly instead of
        silently running the task with an empty parameter set.
        """
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                logger.warning(
                    "Task params are not valid JSON — running with empty "
                    "params. Raw value: {!r:.200}",
                    value,
                )
                return {}
            return parsed if isinstance(parsed, dict) else {}
        if isinstance(value, dict):
            return value
        if value is None:
            return {}
        logger.warning(
            "Task params have unexpected type {} — running with empty params.",
            type(value).__name__,
        )
        return {}

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
            "APScheduler event: type={}, job_id={}, exception={}, scheduled_run_time={}",
            code_label,
            event.job_id,
            getattr(event, "exception", None),
            getattr(event, "scheduled_run_time", None),
        )
