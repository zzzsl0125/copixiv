"""Task manager — the kernel facade for background tasks.

Thin composition of the task kernel (docs/MODULARITY.md §M8):

- :mod:`copixiv.tasks.history` — task_history row ownership
- :mod:`copixiv.tasks.executor` — context building + execution + lifecycle
- :mod:`copixiv.tasks.scheduler` — APScheduler ownership + cron jobs
- :mod:`copixiv.tasks.registry` — task manifests + discovery

Business tasks are discovered via :func:`discover_tasks` (the built-in
task module list), receive a validated Pydantic args object plus
a :class:`TaskContext`, and return a :class:`TaskResult` so the notifier
knows whether a task discovered novels or performed maintenance — no more
guessing based on ``isinstance(result, list)``.
"""

from __future__ import annotations

from collections.abc import Callable

from copixiv.domain.exceptions import (
    NotFoundError,
    TaskAlreadyRunningError,
    ValidationError,
)
from copixiv.log import logger

from .executor import TaskExecutor
from .history import TaskHistoryRecorder
from .registry import TaskSpec, discover_tasks, get_spec
from .scheduler import CronScheduler


class TaskManagerSystem:
    """Manages background tasks and scheduled cron jobs.

    On ``start()``, recovers stale history rows, starts the scheduler and
    registers a cron job for each enabled ``scheduled_tasks`` row.  Manual
    runs (via the API) are dispatched through the same enqueue path so
    history is recorded consistently.

    Dependencies (PixivClient, FileStorage, ...) are captured at
    construction time and travel to task functions exclusively through
    :class:`TaskContext` — task argument names can never collide with
    dependency names (docs/MODULARITY.md §M8).
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

        discover_tasks()

        self._session_factory = session_factory
        self._notifier = notifier

        self._recorder = TaskHistoryRecorder(session_factory)
        self._executor = TaskExecutor(
            session_factory,
            deps={
                "client": client,
                "file_storage": file_storage,
                "image_downloader": image_downloader,
                "epub_builder": epub_builder,
                "config": config,
                "write_lock": DbWriteLock(),
                "notifier": notifier,
            },
        )
        self._scheduler = CronScheduler(session_factory)

        # In-process duplicate-run guard (task name → in-flight).
        # Checked-and-set inside the sync run_task(), so it is atomic on
        # the event loop; cleared by the executor's wrapper ``finally``.
        self._running_names: set[str] = set()

    @property
    def scheduler(self):
        """The underlying APScheduler (compat access for tests/ops)."""
        return self._scheduler.scheduler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler, recover stale tasks, load cron jobs."""
        self._scheduler.start()
        self._recorder.recover_stale()
        self._scheduler.load_cron_jobs(self.run_task)
        logger.info("TaskManagerSystem started — cron jobs loaded.")

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        self._scheduler.stop()

    def reload_cron_jobs(self) -> None:
        """Remove all cron jobs and reload from the database."""
        self._scheduler.reload_cron_jobs(self.run_task)

    # ------------------------------------------------------------------
    # Task execution (enqueue)
    # ------------------------------------------------------------------

    def run_task(
        self,
        name: str,
        func: Callable | None = None,
        params: dict | None = None,
    ) -> int:
        """Enqueue a task for immediate background execution.

        This is the entry point for both cron-triggered and
        manually-triggered runs.  The task is scheduled as a one-shot
        APScheduler job so that history recording and error handling
        happen in the executor's lifecycle wrapper.

        Args:
            name: Task name — the duplicate-run guard keys on it.  When
                *func* is None the registry manifest is resolved by this
                name (unknown names raise ``ValidationError``).
            func: Explicit task function — used by tests and legacy
                callers that enqueue ad-hoc functions.  Explicit functions
                run without an args model (params are ignored by the
                executor).
            params: JSON-serializable argument dict, validated against the
                task's Pydantic args model at execution time.

        Returns:
            The new task-history row id (the task id).

        Raises:
            TaskAlreadyRunningError: If the same task name already has a
                pending/running history row (or is in-flight in-process).
        """
        if func is not None:
            spec = TaskSpec(name=name, description="", func=func)
        else:
            spec = get_spec(name)
            if spec is None:
                raise ValidationError(f"Unknown task function: {name}")
        return self._enqueue(name, spec, params)

    def _enqueue(self, name: str, spec: TaskSpec, params: dict | None) -> int:
        """Shared enqueue path: duplicate guard + history row + one-shot job."""
        params = self._recorder.parse_params(params or {})

        # Duplicate-run guard: in-process set (atomic on the event loop —
        # run_task is sync) + DB check (covers rows from previous process
        # lifetimes).  Without this, "manual run + cron" or double clicks
        # execute the same task concurrently.
        if name in self._running_names:
            raise TaskAlreadyRunningError(
                f"Task '{name}' is already running in this process."
            )
        if self._recorder.has_pending_or_running(name):
            raise TaskAlreadyRunningError(
                f"Task '{name}' is already pending or running."
            )

        task_id = self._recorder.enqueue(name, params)

        self._running_names.add(name)
        self._scheduler.scheduler.add_job(
            self._run_task_wrapper,
            args=(task_id, spec, params),
            id=f"manual_{task_id}",
            max_instances=1,
        )
        return task_id

    def run_task_now(self, task_id: int) -> None:
        """Look up a scheduled task by DB id and run it immediately.

        Used by the ``POST /api/tasks/scheduled/{id}/run`` endpoint.

        Raises:
            NotFoundError: unknown scheduled-task id.
            ValidationError: the scheduled task references an unknown
                task function.
        """
        from copixiv.infrastructure.repositories.task import (
            SQLAlchemyTaskRepository,
        )

        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            tasks = repo.get_scheduled_tasks_sync()
            task = next((t for t in tasks if t.id == task_id), None)
            if task is None:
                raise NotFoundError(f"Scheduled task id={task_id} not found.")

            # The scheduled row's *name* is a display label; the function
            # to run comes from its ``task`` column.  Resolve here and pass
            # the function explicitly so the duplicate-run guard keys on
            # the row name (same semantics as before the kernel split).
            spec = get_spec(task.task)
            if spec is None:
                raise ValidationError(f"Unknown task function: {task.task}")

            params = self._recorder.parse_params(task.params)

        # The scheduled row's *name* is a display label — pass it to the
        # enqueue path directly together with the resolved spec (keeps the
        # args model + duplicate-guard semantics identical to pre-split).
        self._enqueue(task.name, spec, params)

    # ------------------------------------------------------------------
    # Internal: the one-shot job wrapper
    # ------------------------------------------------------------------

    async def _run_task_wrapper(
        self, task_id: int, spec: TaskSpec, params: dict
    ) -> None:
        """One-shot APScheduler job body → executor lifecycle."""
        await self._executor.run_and_record(
            spec, params, task_id,
            recorder=self._recorder,
            notifier=self._notifier,
            running_names=self._running_names,
        )
