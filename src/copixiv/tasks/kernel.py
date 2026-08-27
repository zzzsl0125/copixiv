"""Task kernel — the merged generic task subsystem.

This module collapses the old ``manager / executor / history /
scheduler / registry / context`` split into one file:
:mod:`copixiv.tasks.kernel` (docs/MODULARITY.md §M8).

Sections:
- :class:`TaskSpec` / :func:`register` / :func:`discover_tasks` — declarative
  task manifests + built-in discovery
- :class:`TaskContext` — the injected dependency channel
- :class:`TaskHistoryRecorder` — task_history row ownership
- :class:`TaskExecutor` — context building + execution + lifecycle
- :class:`CronScheduler` — APScheduler ownership + cron jobs
- :class:`TaskManagerSystem` — the facade

Business tasks are discovered via :func:`discover_tasks` (the built-in
task module list), receive a validated Pydantic args object plus
a :class:`TaskContext`, and return a :class:`TaskResult` so the notifier
knows whether a task discovered novels or performed maintenance — no more
guessing based on ``isinstance(result, list)``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import inspect
import json
import time
import traceback

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from copixiv.core.exceptions import (
    NotFoundError,
    TaskAlreadyRunningError,
    ValidationError,
)
from copixiv.core.models import TaskResult
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import DbWriteLock
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.notify.composite import CompositeNotifier
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.log import capture_logs, logger


# ==== 分区: registry ====

# A task is a module-level function decorated with :func:`register`,
# carrying its own manifest: name, description, and a Pydantic argument
# schema.  This is the copixiv equivalent of a DSH plugin manifest — the
# registry stores :class:`TaskSpec` records, and argument descriptions come
# from the Pydantic ``args`` model instead of ``inspect.signature``
# reflection (docs/MODULARITY.md §M8).

# Discovery (:func:`discover_tasks`) imports the built-in task modules
# (:data:`DEFAULT_TASK_MODULES`).  There is no third-party plugin
# ecosystem — docs/MODULARITY.md §6.

# Built-in task modules — the single source of truth for the task set.
DEFAULT_TASK_MODULES: tuple[str, ...] = (
    "copixiv.tasks.novels",
    "copixiv.tasks.batch",
    "copixiv.tasks.maintenance",
)


@dataclass(frozen=True)
class TaskSpec:
    """A registered task's manifest: metadata + function + argument schema."""

    name: str
    description: str
    func: Callable[..., Awaitable[object]]
    args_model: type[BaseModel] | None = None


_registry: dict[str, TaskSpec] = {}


def register(
    name: str,
    description: str = "",
    args: type[BaseModel] | None = None,
) -> Callable:
    """Register a task function under *name* with a declarative manifest.

    Args:
        name: Task name (stored in ``scheduled_tasks.task`` / history).
        description: Human-readable description; defaults to the
            function's docstring.
        args: Pydantic model for the task's JSON arguments — validated and
            converted by the executor before the function runs.  ``None``
            for parameter-less tasks.
    """

    def decorator(func):
        _registry[name] = TaskSpec(
            name=name,
            description=(description or (func.__doc__ or "").strip()),
            func=func,
            args_model=args,
        )
        return func

    return decorator


def unregister(name: str) -> bool:
    """Remove a task from the registry (plugin unload / tests).

    Returns True when a task was actually removed.
    """
    return _registry.pop(name, None) is not None


def get_spec(name: str) -> TaskSpec | None:
    """Look up a task manifest by name."""
    return _registry.get(name)


def get_task(name: str) -> Callable[..., Awaitable[object]] | None:
    """Look up a task function by name (convenience for callers)."""
    spec = _registry.get(name)
    return spec.func if spec else None


def list_tasks() -> dict[str, Callable[..., Awaitable[object]]]:
    """Return a copy of ``{name: function}``."""
    return {name: spec.func for name, spec in _registry.items()}


def describe_tasks() -> list[dict]:
    """Return task descriptors for the ``/api/tasks/methods`` contract.

    Each descriptor is ``{"name", "description", "arguments"}`` where
    ``arguments`` lists ``{"name", "type", "default", "required"}`` —
    derived purely from the task's Pydantic args model (no signature
    reflection).
    """
    discover_tasks()
    methods = []
    for spec in _registry.values():
        methods.append({
            "name": spec.name,
            "description": spec.description,
            "arguments": _describe_arguments(spec),
        })
    return methods


def _describe_arguments(spec: TaskSpec) -> list[dict]:
    if spec.args_model is None:
        return []
    arguments = []
    for field_name, info in spec.args_model.model_fields.items():
        arguments.append({
            "name": field_name,
            "type": _annotation_type_name(info.annotation),
            "default": None if info.is_required() else info.default,
            "required": info.is_required(),
        })
    return arguments


def _annotation_type_name(annotation: Any) -> str:
    """Map a Pydantic field annotation to a display type name.

    Handles ``int``/``bool``/``float``/``str``/``list[...]``, ``X | None``
    unions (the non-None member wins), and falls back to ``"str"`` for
    anything else (the API contract documents task arguments as JSON
    scalars; lists render as text inputs in the task editor).
    """
    if annotation is None:
        return "str"
    origin = getattr(annotation, "__origin__", None)
    if origin is None:
        return {
            int: "int", bool: "bool", float: "float", str: "str",
        }.get(annotation, "str")
    if origin is list:
        return "list"
    # Union (e.g. ``int | None``) — pick the first non-NoneType member.
    for member in getattr(annotation, "__args__", ()) or ():
        if member is not type(None):
            return _annotation_type_name(member)
    return "str"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_tasks() -> None:
    """Import every built-in task module (:data:`DEFAULT_TASK_MODULES`).

    Idempotent — imports are cheap no-ops on repeat calls.  A module that
    fails to import is logged loudly (never silent) so the failure is
    visible instead of silently dropping its tasks.
    """
    for module_name in DEFAULT_TASK_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:
            logger.exception(
                "Failed to import task module '%s' — task(s) not registered.",
                module_name,
            )


# ==== 分区: context ====

# Dependencies travel exclusively through *ctx* (never through parameter
# names), so business argument names can never collide with dependency
# names — the two channels are fully separated (docs/MODULARITY.md §M8).
# The executor builds the context; business tasks read only what they need.

@dataclass
class TaskContext:
    """All services a task may need, provided by the task kernel."""

    uow: SqlUnitOfWork | None = None
    session_factory: Any = None  # SQLAlchemy sessionmaker（tasks 层不 import app 层）
    client: PixivClient | None = None
    file_storage: FileStorage | None = None
    image_downloader: ImageDownloader | None = None
    epub_builder: EpubBuilder | None = None
    config: Any = None  # AppConfig——由组合根装配，类型不跨层引用（§2.1）
    write_lock: DbWriteLock | None = None
    notifier: CompositeNotifier | None = None
    task_id: int | None = None

    def child_uow(self) -> SqlUnitOfWork:
        """Return a fresh UnitOfWork sharing the process session factory.

        Used by fan-out helpers to give every concurrent branch its own
        session — sessions are never shared across coroutines.
        """
        if self.session_factory is None:
            raise RuntimeError(
                "TaskContext.session_factory is not set — the context was "
                "not built by the task kernel."
            )
        return SqlUnitOfWork(self.session_factory)

    def with_uow(self, uow: SqlUnitOfWork) -> "TaskContext":
        """Return a copy of this context with *uow* replaced."""
        return replace(self, uow=uow)


# ==== 分区: history ====

# Every task_history write goes through this class: enqueue (pending row),
# status transitions (running/success/failed/interrupted), and
# result/duration updates — all inside the global ``db_write()`` lock so
# they serialize with every other write path.

class TaskHistoryRecorder:
    """Records task lifecycle rows (enqueue + status/result updates)."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    # -- enqueue ------------------------------------------------------------

    def enqueue(self, name: str, params: dict, task_func: str) -> int:
        """Insert the pending row; returns the new task id.

        Inserts under *name* (display name) and *task_func* (the registered
        function name — the dedup key).  The DB partial unique index
        ``ux_task_history_running`` rejects a second pending/running row for
        the same ``task_func``, surfacing as ``sqlalchemy.exc.IntegrityError``
        (caught by the manager and mapped to ``TaskAlreadyRunningError``).

        Short INSERT outside ``db_write()`` — task enqueue happens in sync
        API paths; the 60s busy_timeout covers the rare collision.
        """
        from copixiv.tasks.history_repo import (
            SQLAlchemyTaskRepository,
        )

        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            task_id = repo.add_task_sync(name, params, task_func)
            session.commit()
        logger.info("Task '{}' (id={}) enqueued.", name, task_id)
        return task_id

    # -- status / result updates -------------------------------------------

    async def update(
        self,
        task_id: int,
        status: str,
        result: str | None = None,
        duration: float | None = None,
        progress: str | None = None,
    ) -> None:
        """Update a TaskHistory row inside the global write lock."""
        from copixiv.db.write_lock import db_write
        from copixiv.tasks.history_repo import (
            SQLAlchemyTaskRepository,
        )

        async with db_write():
            with self._session_factory() as session:
                repo = SQLAlchemyTaskRepository(session)
                repo.update_task_sync(
                    task_id, status, result=result, duration=duration,
                    progress=progress,
                )
                session.commit()

    # -- stale recovery ----------------------------------------------------

    def recover_stale(self) -> None:
        """Mark tasks stuck in pending/running as interrupted.

        A process crash (or ``stop()`` with ``wait=False``) leaves
        ``task_history`` rows in pending/running forever — without this,
        the UI shows them as eternally running and the duplicate-run
        guard keeps treating the task name as busy.
        """
        from sqlalchemy import select as _select, update as _update
        from copixiv.db import models

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

    # -- params parsing ----------------------------------------------------

    @staticmethod
    def parse_params(value: Any) -> dict:
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


# ==== 分区: executor ====

# Builds the :class:`TaskContext`, validates JSON params against the task's
# Pydantic args model, executes with a 30-minute timeout, and drives the
# full lifecycle around it (history rows, log capture, notification).

class TaskExecutor:
    """Executes task functions with context injection and a timeout."""

    TIMEOUT_SECONDS = 1800  # 30 minutes

    def __init__(self, session_factory, deps: dict):
        self._session_factory = session_factory
        self._deps = deps

    # ------------------------------------------------------------------
    # Public — the whole lifecycle
    # ------------------------------------------------------------------

    async def run_and_record(
        self,
        spec: TaskSpec,
        params: dict,
        task_id: int,
        *,
        recorder,
        notifier,
    ) -> None:
        """Execute a task, record history, and send notifications.

        The duplicate-run guard lives at the DB layer (the partial unique
        index on ``task_func``); this method owns everything from the first
        "running" row to the final notification.
        """
        name = spec.name
        logger.info("Starting task '{}' (id={})...", name, task_id)
        await self._run_inner(
            spec, params, task_id,
            recorder=recorder, notifier=notifier,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_inner(
        self,
        spec: TaskSpec,
        params: dict,
        task_id: int,
        *,
        recorder,
        notifier,
    ) -> None:
        """Body of the task lifecycle — status tracking + execution."""
        name = spec.name
        start_time = time.time()
        status: str = "running"
        error_msg: str | None = None
        result_val: Any = None

        await recorder.update(task_id, "running")

        # Capture all loguru output during task execution
        with capture_logs(task_id=task_id) as get_logs:
            try:
                ctx = self._build_context(task_id)
                result_val = await self._execute(spec, params, ctx)
                status = "success"
                logger.info(
                    "Task '{}' (id={}) completed successfully.", name, task_id,
                )

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
                    await recorder.update(
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

        await recorder.update(
            task_id, status, result=result_data, duration=duration,
        )

        # --- Notification ---
        if notifier is not None:
            await notifier.send_task_result(
                task_name=name,
                status=status,
                duration=duration,
                error=error_msg,
                result=result,
            )

    def _build_context(self, task_id: int) -> TaskContext:
        """Assemble the TaskContext for one run."""
        from copixiv.db.uow import SqlUnitOfWork

        return TaskContext(
            uow=SqlUnitOfWork(self._session_factory),
            session_factory=self._session_factory,
            client=self._deps.get("client"),
            file_storage=self._deps.get("file_storage"),
            image_downloader=self._deps.get("image_downloader"),
            epub_builder=self._deps.get("epub_builder"),
            config=self._deps.get("config"),
            write_lock=self._deps.get("write_lock"),
            notifier=self._deps.get("notifier"),
            task_id=task_id,
        )

    async def _execute(
        self, spec: TaskSpec, params: dict, ctx: TaskContext
    ) -> Any:
        """Validate params against the args model and run the function.

        Task functions are ``(args, ctx)`` when the manifest declares an
        args model, or ``(ctx,)`` when it does not (parameter-less tasks
        and ad-hoc functions such as test doubles).
        """
        args_obj = (
            spec.args_model.model_validate(params)
            if spec.args_model is not None
            else None
        )

        def _call() -> Any:
            if args_obj is None:
                # ctx=... (keyword) keeps **kwargs-style test doubles
                # working alongside ctx-only task functions.
                return spec.func(ctx=ctx)
            return spec.func(args_obj, ctx=ctx)

        if inspect.iscoroutinefunction(spec.func):
            return await asyncio.wait_for(_call(), timeout=self.TIMEOUT_SECONDS)

        # Sync functions (third-party tasks) run in a throwaway thread.
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return await asyncio.wait_for(
                loop.run_in_executor(pool, _call),
                timeout=self.TIMEOUT_SECONDS,
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


# ==== 分区: scheduler ====

# Lifecycle (start/stop), the scheduler-wide error listener, and cron jobs
# derived from the ``scheduled_tasks`` table.  Actual execution is delegated
# back to the manager through the *enqueue* callback, so this class knows
# nothing about history rows or contexts.


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

        *enqueue* is ``manager.run_scheduled`` — the single execution
        entry point shared with manual runs.
        """
        for job in self.scheduler.get_jobs():
            if job.id.startswith("cron_"):
                job.remove()
        self.load_cron_jobs(enqueue)

    def load_cron_jobs(self, enqueue) -> None:
        """Read ``scheduled_tasks`` and register a cron job per enabled row."""
        from copixiv.tasks.history_repo import (
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
                        # Args are (function_name, display_name, enqueue,
                        # params).  At fire time the manager records the
                        # history row under *display_name* (the user's UI
                        # label) and resolves the task function by
                        # *function_name* (the ``task`` column) — so a custom
                        # display name can never break the lookup, yet the
                        # task-history list still shows the user's label.
                        args=(task.task, task.name, enqueue, params),
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
        self, task_name: str, display_name: str, enqueue, params: dict
    ) -> None:
        """Fires when a cron trigger is hit.  Enqueues via the manager.

        *task_name* is the registered task *function* name (``task.task``
        column); *display_name* is the ``task.name`` free-form UI label.
        *enqueue* is :meth:`TaskManagerSystem.run_scheduled`, which records
        the history row under *display_name* and resolves the task function
        (and its args model) by *task_name* — matching
        :meth:`TaskManagerSystem.run_task_now` exactly.

        A cron firing while a manual run of the same task is still active
        is skipped (the duplicate-run guard raises) instead of stacking a
        concurrent duplicate.
        """
        logger.info("Cron triggered: {}", display_name)
        try:
            enqueue(display_name, task_name, params=params)
        except TaskAlreadyRunningError:
            logger.warning(
                "Cron for '{}' skipped — a run is already pending/running.",
                display_name,
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


# ==== 分区: manager ====

# Thin composition of the task kernel (docs/MODULARITY.md §M8):

# - :class:`TaskHistoryRecorder` — task_history row ownership
# - :class:`TaskExecutor` — context building + execution + lifecycle
# - :class:`CronScheduler` — APScheduler ownership + cron jobs
# - :class:`TaskSpec` / :func:`discover_tasks` — task manifests + discovery

# Business tasks are discovered via :func:`discover_tasks` (the built-in
# task module list), receive a validated Pydantic args object plus
# a :class:`TaskContext`, and return a :class:`TaskResult` so the notifier
# knows whether a task discovered novels or performed maintenance — no more
# guessing based on ``isinstance(result, list)``.


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
        from copixiv.db.write_lock import DbWriteLock

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

        # Process-wide execution serialization: only one task body runs at
        # a time.  Jobs that fire while another task is executing wait on
        # this lock, keeping their history row in "pending" until the
        # previous task finishes — which is what makes the UI's queued
        # (yellow clock) vs running (blue spinner) distinction actually
        # visible, and makes tasks complete in enqueue order.  Intra-task
        # concurrency (fan-out downloads via asyncio.gather inside a single
        # task) is unaffected: those are direct calls, not separate
        # run_and_record invocations.  Same coroutine always acquires
        # task-lock → db_write-lock in that order, so no deadlock.
        self._task_lock = asyncio.Lock()

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
        self._scheduler.load_cron_jobs(self.run_scheduled)
        logger.info("TaskManagerSystem started — cron jobs loaded.")

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        self._scheduler.stop()

    def reload_cron_jobs(self) -> None:
        """Remove all cron jobs and reload from the database."""
        self._scheduler.reload_cron_jobs(self.run_scheduled)

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

        Entry point for manually-triggered runs — batch endpoints, tests,
        and ad-hoc callers that enqueue an explicit function.  Cron-
        triggered runs and the scheduled "run now" endpoint go through
        :meth:`run_scheduled` (which records history under the scheduled
        row's display name and resolves the function by its task column).
        The task is scheduled as a one-shot APScheduler job so that
        history recording and error handling happen in the executor's
        lifecycle wrapper.

        Args:
            name: Task function name (``TaskSpec.name``) — also the dedup
                key stored in ``task_history.task_func``.  When *func* is
                None the registry manifest is resolved by this name
                (unknown names raise ``ValidationError``).
            func: Explicit task function — used by tests and legacy
                callers that enqueue ad-hoc functions.  Explicit functions
                run without an args model (params are ignored by the
                executor).
            params: JSON-serializable argument dict, validated against the
                task's Pydantic args model at execution time.

        Returns:
            The new task-history row id (the task id).

        Raises:
            TaskAlreadyRunningError: If the same task *function* already has
                a pending/running history row (the DB partial unique index
                ``ux_task_history_running``).
        """
        if func is not None:
            spec = TaskSpec(name=name, description="", func=func)
        else:
            spec = get_spec(name)
            if spec is None:
                raise ValidationError(f"Unknown task function: {name}")
        return self._enqueue(name, spec, params)

    def _enqueue(self, name: str, spec: TaskSpec, params: dict | None) -> int:
        """Shared enqueue path: history row (DB dedup) + one-shot job.

        ``name`` is the display name recorded on the history row; ``spec.name``
        (the registered function name) is recorded as ``task_func`` and is the
        dedup key.  The manual path uses the same value for both (spec.name ==
        name); the scheduled path passes display_name as *name* and resolves
        spec by the function name.
        """
        params = self._recorder.parse_params(params or {})
        task_func = spec.name

        # Duplicate-run guard is a single DB constraint: the partial unique
        # index ``ux_task_history_running`` rejects a second pending/running
        # row for the same ``task_func``.  This covers both in-process
        # re-entry and rows left over from a previous process lifetime.
        try:
            task_id = self._recorder.enqueue(name, params, task_func)
        except IntegrityError as exc:
            raise TaskAlreadyRunningError(
                f"任务 '{task_func}' 已存在 pending/running 记录"
            ) from exc

        self._scheduler.scheduler.add_job(
            self._run_task_wrapper,
            args=(task_id, spec, params),
            id=f"manual_{task_id}",
            max_instances=1,
        )
        return task_id

    def run_scheduled(
        self,
        display_name: str,
        func_name: str,
        params: dict | None = None,
    ) -> int:
        """Enqueue a scheduled-task run by display name + function name.

        The single execution path shared by the cron trigger and
        :meth:`run_task_now`.  The history row is recorded under
        *display_name* (the ``scheduled_tasks.name`` free-form UI label),
        while the task function — and its Pydantic args model — is resolved
        by *func_name* (the ``scheduled_tasks.task`` column).  This keeps a
        custom display name from ever breaking the registry lookup *and*
        makes the task-history list show the user's label instead of the
        function name.  The duplicate-run guard keys on *func_name* (the
        ``task_history.task_func`` column), so two scheduled rows sharing a
        function but differing in display name are deduplicated.

        Raises:
            ValidationError: *func_name* is not a registered task function.
            TaskAlreadyRunningError: *func_name* already pending/running.
        """
        spec = get_spec(func_name)
        if spec is None:
            raise ValidationError(f"Unknown task function: {func_name}")
        return self._enqueue(display_name, spec, params)

    def run_task_now(self, task_id: int) -> None:
        """Look up a scheduled task by DB id and run it immediately.

        Used by the ``POST /api/tasks/scheduled/{id}/run`` endpoint.  The
        dedup key is the scheduled row's ``task`` (the registered function
        name), so two scheduled rows sharing a function but differing in
        display name are deduplicated.

        Raises:
            NotFoundError: unknown scheduled-task id.
            ValidationError: the scheduled task references an unknown
                task function.
        """
        from copixiv.tasks.history_repo import (
            SQLAlchemyTaskRepository,
        )

        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            tasks = repo.get_scheduled_tasks_sync()
            task = next((t for t in tasks if t.id == task_id), None)
            if task is None:
                raise NotFoundError(f"Scheduled task id={task_id} not found.")
            params = self._recorder.parse_params(task.params)

        # The scheduled row's *name* is a display label; the function to run
        # comes from its ``task`` column.  Delegate to run_scheduled so the
        # cron path and this endpoint share one enqueue path (history records
        # the display name, the function — the dedup key — is resolved by the
        # task column, the args model + duplicate-guard semantics stay
        # identical).
        self.run_scheduled(task.name, task.task, params)

    # ------------------------------------------------------------------
    # Internal: the one-shot job wrapper
    # ------------------------------------------------------------------

    async def _run_task_wrapper(
        self, task_id: int, spec: TaskSpec, params: dict
    ) -> None:
        """One-shot APScheduler job body → executor lifecycle.

        Serialized by ``self._task_lock``: while one task runs, every other
        enqueued job waits here with its history row still ``pending``.
        See ``__init__`` for the full rationale.
        """
        async with self._task_lock:
            await self._executor.run_and_record(
                spec, params, task_id,
                recorder=self._recorder,
                notifier=self._notifier,
            )

