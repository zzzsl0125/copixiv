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
- :class:`CronTicker` / :func:`next_occurrence` — in-memory cron tick loop
- :class:`TaskManagerSystem` — the facade + its single queue worker

The ``task_history`` row *is* the queue: enqueue inserts a ``pending`` row
and wakes the worker, which drains rows in id order one at a time.  There
is no separate in-memory run list and no execution lock — serialization is
structural (one consumer).

Business tasks are discovered via :func:`discover_tasks` (the built-in
task module list), receive a validated Pydantic args object plus
a :class:`TaskContext`, and return a :class:`TaskResult` so the notifier
knows whether a task discovered novels or performed maintenance — no more
guessing based on ``isinstance(result, list)``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import time
import traceback

from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from croniter import croniter
from pydantic import BaseModel
from sqlalchemy import select, update

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import TaskResult
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import db_write
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
from copixiv.log import capture_logs, logger


# ==== 分区: cron constants ====

# Cron expressions are evaluated in the process-local timezone, matching the
# previous scheduler behaviour (5-field crontabs used the server's local tz,
# not UTC).  ``datetime.now().astimezone().tzinfo`` is a fixed offset captured
# at import time: an accepted approximation for DST-transition zones (the risk
# noted in docs/SIMPLIFY_PLAN-v2.md §2.3).
CRON_TIMEZONE = datetime.now().astimezone().tzinfo

# How often the in-memory ticker wakes up and checks for due cron entries.
CRON_TICK_SECONDS = 30

# A due occurrence more than this late is considered a missed window: it is
# skipped (never back-filled) and the entry advances to the next occurrence.
CRON_MISFIRE_GRACE_SECONDS = 60


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


# Queue partitions.  ``task_history`` stays the single queue, but the worker
# is split in two so a long *fetching* task cannot block *local* work
# (exports, batch edits, maintenance).  Fetching tasks are identified by
# function name; every other function — including ad-hoc and legacy ``NULL``
# rows — goes to the general lane.
#
# Only the fetching lane talks to Pixiv, and its request rate is governed by
# ``PixivClient``'s semaphore + per-account ``min_interval`` + cooldown, not by
# worker serialization.  Keeping it serial nevertheless matters for a different
# reason: two concurrent fetches for the same author would both download and
# then race each other's upserts for no benefit.
FETCH_TASK_PREFIXES: tuple[str, ...] = (
    "novel_fetch", "novel_follow", "novel_ranking", "novel_search",
    "author_fetch", "author_delete", "author_special_follow", "failed_retry",
)


def is_fetch_task(func_name: str | None) -> bool:
    """True when *func_name* is a Pixiv-fetching task (fetching lane)."""
    if not func_name:
        return False
    return any(
        func_name == p or func_name.startswith(p)
        for p in FETCH_TASK_PREFIXES
    )


_registry: dict[str, TaskSpec] = {}


def register(
    name: str,
    description: str = "",
    args: type[BaseModel] | None = None,
) -> Callable:
    """Register a task function under *name* with a declarative manifest.

    The function **must be a coroutine function**.  That is enforced here, at
    registration time, because the alternative cannot honour the contract the
    executor advertises:

    * Python threads cannot be cancelled, so a synchronous task that reaches
      ``TIMEOUT_SECONDS`` keeps running while the executor has already recorded
      a failure — a "timeout" that neither stops the work nor frees the worker;
    * the workaround (``run_in_executor`` + ``wait_for`` around a per-call
      ``ThreadPoolExecutor``) joined that thread **on the event loop** at
      shutdown, stalling every HTTP request for the rest of the task.

    Rejecting sync functions up front removes that class of surprise instead
    of documenting it.  Wrap blocking work in ``asyncio.to_thread`` inside an
    async task when a thread really is needed.

    Args:
        name: Task name (stored in ``scheduled_tasks.task`` / history).
        description: Human-readable description; defaults to the
            function's docstring.
        args: Pydantic model for the task's JSON arguments — validated and
            converted by the executor before the function runs.  ``None``
            for parameter-less tasks.

    Raises:
        TypeError: *func* is not a coroutine function.
    """

    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"Task {name!r} must be an async def function: synchronous "
                "tasks cannot be cancelled on timeout and would block the "
                "event loop. Wrap blocking work in asyncio.to_thread."
            )
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
    ``arguments`` is the full JSON Schema produced by the task's Pydantic
    args model (``model_json_schema()``), or ``None`` for a parameter-less
    task.  Pydantic owns the schema — no signature reflection here.
    """
    discover_tasks()
    methods = []
    for spec in _registry.values():
        methods.append({
            "name": spec.name,
            "description": spec.description,
            "arguments": (
                spec.args_model.model_json_schema()
                if spec.args_model is not None
                else None
            ),
        })
    return methods


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
    # Only fields a task body actually reads live here.  ``config`` /
    # ``epub_builder`` / ``write_lock`` / ``notifier`` were carried and never
    # referenced by any task (verified by grepping ``ctx.<name>`` across the
    # tree): the executor uses the notifier itself, ``db_write()`` is a no-op
    # boundary under PG, and nothing needed the builder or the config.  Keeping
    # them advertised a dependency surface that did not exist.
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
# result/duration updates — all inside short write transactions.  There is
# no global ``db_write()`` lock any more: PostgreSQL MVCC lets independent
# writes proceed concurrently, and data write paths retry the rare
# LockNotAvailable conflict on a fresh transaction.

class TaskHistoryRecorder:
    """Records task lifecycle rows (enqueue + status/result updates).

    The ``task_history`` table is also the execution queue: :meth:`enqueue`
    inserts the ``pending`` row the manager's single worker later selects.
    ``_live_pending`` tracks rows this process enqueued but has not started,
    so :meth:`recover_stale` can tell them apart from a previous process's
    crash leftovers (an enqueue may legitimately precede ``start()``).
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._live_pending: set[int] = set()

    # -- enqueue ------------------------------------------------------------

    def enqueue(self, name: str, params: dict, task_func: str) -> int:
        """Insert the pending row; returns the new task id.

        Inserts under *name* (display name) and *task_func* (the registered
        function name, kept for history/audit).  There is no dedup: a second
        enqueue for the same ``task_func`` simply inserts another pending row,
        so duplicates queue up and each runs once.

        Short INSERT outside any write transaction — task enqueue happens in sync
        API paths; the 60s lock_timeout covers the rare collision.
        """
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            task_id = repo.add_task_sync(name, params, task_func)
            session.commit()
        self._live_pending.add(task_id)
        logger.info("Task '{}' (id={}) enqueued.", name, task_id)
        return task_id

    # -- status / result updates -------------------------------------------

    async def update(
        self,
        task_id: int,
        status: str | None,
        result: str | None = None,
        duration: float | None = None,
        progress: str | None = None,
    ) -> None:
        """Update a TaskHistory row in a short write transaction.

        ``status=None`` is a progress-only write: the row's state is left
        alone, so a late progress update can never resurrect a finished task.
        """
        async with db_write():
            with self._session_factory() as session:
                repo = SQLAlchemyTaskRepository(session)
                repo.update_task_sync(
                    task_id, status, result=result, duration=duration,
                    progress=progress,
                )
                session.commit()
        if status is not None and status != "pending":
            # Once started (running) or terminal this row is no longer a
            # live queue entry.
            self._live_pending.discard(task_id)

    # -- stale recovery ----------------------------------------------------

    def _prune_live_pending(self) -> None:
        """Drop ids this process no longer owns from ``_live_pending``.

        The set exists so :meth:`recover_stale` can tell "enqueued by this
        process, not started yet" from a previous process's crash leftover.
        Nothing used to remove an id except a status write, so a row that was
        enqueued and then aborted (recovered as interrupted before the worker
        picked it up, or failed to start at all) stayed in the set forever.

        That is not just a leak: ``recover_stale`` excludes ``_live_pending``
        from recovery, so a long-lived process would permanently shield its
        own abandoned rows from ever being recovered — they would sit as
        ``pending`` forever while each recovery pass skipped them.

        Pruning by "does this id still exist as a pending row?" is exact: a
        row we own is pending by definition (the worker flips it to running
        and discards the id itself).
        """
        if not self._live_pending:
            return
        try:
            with self._session_factory() as session:
                alive = set(session.execute(
                    select(models.TaskHistory.id).where(
                        models.TaskHistory.id.in_(self._live_pending),
                        models.TaskHistory.status == "pending",
                    )
                ).scalars().all())
        except Exception:
            logger.exception("Failed to prune live pending task ids.")
            return
        dropped = self._live_pending - alive
        if dropped:
            self._live_pending.difference_update(dropped)
            logger.info(
                "Pruned {} abandoned live-pending task id(s).", len(dropped),
            )

    def recover_stale(self) -> None:
        """Mark tasks stuck in pending/running as interrupted.

        A process crash leaves ``task_history`` rows in pending/running
        forever — without this, the UI shows them as eternally running.
        Rows this process enqueued but has not started yet are *not* crash
        leftovers: they are this process's live queue (enqueue can precede
        ``start()``) and are left for the worker.  Nothing is auto-resumed.
        """
        try:
            # Prune first: an id whose row is no longer pending can never be
            # "live" for this process, and leaving it in the set would shield
            # it from recovery (and grow the set without bound).
            self._prune_live_pending()
            with self._session_factory() as session:
                stmt = select(models.TaskHistory.id).where(
                    models.TaskHistory.status.in_(("pending", "running"))
                )
                if self._live_pending:
                    stmt = stmt.where(
                        models.TaskHistory.id.not_in(self._live_pending)
                    )
                stale_ids = session.execute(stmt).scalars().all()
                if not stale_ids:
                    return
                session.execute(
                    update(models.TaskHistory)
                    .where(models.TaskHistory.id.in_(stale_ids))
                    .values(
                        status="interrupted",
                        end_time=datetime.now(timezone.utc),
                        # ``result`` is JSONB: pass the dict, never a
                        # ``json.dumps`` string.  A raw string lands as a JSON
                        # *string* scalar (``jsonb_typeof = 'string'``), so
                        # ``result->>'summary'`` returns NULL and the UI shows
                        # an empty summary.  This bypasses
                        # ``update_task_sync``, which is what normally parses
                        # the string back into an object.
                        result={"summary": "服务重启导致中断"},
                    )
                )
                session.commit()
                logger.warning(
                    "Recovered {} stale task history rows → interrupted.",
                    len(stale_ids),
                )
        except Exception:
            logger.exception("Failed to recover stale task history rows.")


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

        This method owns everything from the first "running" row to the
        final notification; there is no enqueue-time dedup.
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
                # Cancellation (e.g. manager shutdown) is BaseException —
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

        # A failed run must never be summarised as 「完成」.  ``_normalize_result``
        # maps a missing return value to that generic summary, which is right
        # for a task that returned nothing on the success path — but on the
        # failure path it leaves rows saying "failed / 完成", hiding the actual
        # error from the UI (32 such rows in production).
        if status == "failed" and not result.failed_ids and (
            not result.summary or result.summary == "完成"
        ):
            result = result.model_copy(
                update={"summary": error_msg or "任务失败（未记录原因）"},
            )

        result_data = json.dumps(
            {
                "log": log_output,
                "summary": result.summary,
                "new_novels_count": result.new_novel_count,
                "new_novel_titles": result.new_novel_titles,
                # Machine-readable per-item failures.  Without this the only
                # trace of a novel that failed mid-batch was the ledger and the
                # log text; the task row still read "success, N new".
                "failed_ids": result.failed_ids,
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
        return TaskContext(
            uow=SqlUnitOfWork(self._session_factory),
            session_factory=self._session_factory,
            client=self._deps.get("client"),
            file_storage=self._deps.get("file_storage"),
            image_downloader=self._deps.get("image_downloader"),
            task_id=task_id,
        )

    async def _execute(
        self, spec: TaskSpec, params: dict, ctx: TaskContext
    ) -> Any:
        """Validate params against the args model and run the function.

        Task functions are ``(args, ctx)`` when the manifest declares an
        args model, or ``(ctx,)`` when it does not (parameter-less tasks
        and ad-hoc functions such as test doubles).

        Only coroutine functions are supported — :func:`register` rejects
        anything else, so there is no thread-pool branch here.  That branch
        could not honour ``TIMEOUT_SECONDS`` (a Python thread cannot be
        cancelled) and its executor shutdown joined the thread on the event
        loop, stalling every request for the remainder of the task.
        """
        args_obj = (
            spec.args_model.model_validate(params)
            if spec.args_model is not None
            else None
        )

        # ctx=... (keyword) keeps **kwargs-style test doubles working
        # alongside ctx-only task functions.
        if args_obj is None:
            coro = spec.func(ctx=ctx)
        else:
            coro = spec.func(args_obj, ctx=ctx)
        return await asyncio.wait_for(coro, timeout=self.TIMEOUT_SECONDS)

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


# ==== 分区: cron ticker ====

# Cron schedules are evaluated in-process: ``croniter`` computes the next
# occurrence and a 30s asyncio tick loop fires due entries through the same
# *enqueue* callback used by manual runs.  The ``scheduled_tasks`` table is
# the source of truth; ``{scheduled_id: next_run}`` is only a cache, so a
# restart simply recomputes it (missed windows are skipped, never replayed).
#
# Nothing here knows about history rows or contexts — execution is the
# manager's job.


def next_occurrence(cron: str, after: datetime) -> datetime:
    """Return the first cron occurrence strictly after *after*.

    The expression must have exactly five whitespace-separated fields
    (minute hour day-of-month month day-of-week), preserving the contract
    the previous 5-field crontab validator enforced.  A naive
    *after* is interpreted in :data:`CRON_TIMEZONE` (the process-local
    timezone, same as the previous scheduler behaviour); an aware *after*
    keeps its own timezone.

    Raises:
        ValueError: the expression has the wrong field count or is
            otherwise invalid (``croniter`` raises ``ValueError``
            subclasses; they propagate unchanged).
    """
    if not isinstance(cron, str) or len(cron.split()) != 5:
        raise ValueError(
            "Cron expression must have exactly 5 whitespace-separated "
            f"fields: {cron!r}"
        )
    if after.tzinfo is None:
        after = after.replace(tzinfo=CRON_TIMEZONE)
    return croniter(cron, after).get_next(datetime)


@dataclass
class _CronEntry:
    """One loaded cron row and its cached next occurrence."""

    scheduled_id: int
    display_name: str
    func_name: str
    params: dict
    cron: str
    next_run: datetime


class CronTicker:
    """In-memory cron ticker driven by ``scheduled_tasks`` rows.

    Loaded entries live in ``self._entries`` keyed by scheduled-task id.
    ``next_run`` is preserved across reloads while the row's cron
    expression is unchanged, so an API edit that does not touch the
    schedule never resets the timer.
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._entries: dict[int, _CronEntry] = {}
        self._enqueue = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, enqueue) -> None:
        """Load the current cron rows and start the tick loop."""
        self._stopped = False
        self.load_cron_jobs(enqueue)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the tick loop (cancelling the sleep) and clear the task."""
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info("CronTicker stopped.")

    async def _run(self) -> None:
        """Sleep for one tick, fire due entries, repeat until stopped."""
        while not self._stopped:
            await asyncio.sleep(CRON_TICK_SECONDS)
            try:
                self._fire_due(datetime.now(CRON_TIMEZONE))
            except Exception:
                # One bad tick (e.g. a transient DB error while enqueuing)
                # must never kill the loop.
                logger.exception("Cron ticker iteration failed.")

    # ------------------------------------------------------------------
    # Cron loading
    # ------------------------------------------------------------------

    def reload_cron_jobs(self, enqueue, *, now: datetime | None = None) -> None:
        """Reload all cron entries from the database."""
        self.load_cron_jobs(enqueue, now=now)

    def load_cron_jobs(
        self, enqueue, *, now: datetime | None = None
    ) -> None:
        """Read ``scheduled_tasks`` and rebuild the in-memory entry map.

        *enqueue* is ``manager.run_scheduled`` — the single execution entry
        point shared with manual runs.  *now* is injectable for tests and
        defaults to the current local time.

        Disabled rows and rows referencing an unregistered task function
        are skipped.  A row with an invalid cron expression is warned about
        and skipped too; it never aborts the whole load.  The new entry map
        is assigned atomically (a new dict) because the tick loop may be
        iterating the previous one.
        """
        if now is None:
            now = datetime.now(CRON_TIMEZONE)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=CRON_TIMEZONE)

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

        entries: dict[int, _CronEntry] = {}
        for task in tasks:
            if not task.is_enabled:
                continue

            if get_spec(task.task) is None:
                logger.warning(
                    "Scheduled task '{}' references unknown function "
                    "'{}' — skipped.",
                    task.name,
                    task.task,
                )
                continue

            existing = self._entries.get(task.id)
            if existing is not None and existing.cron == task.cron:
                # Same row, same schedule: keep the timer so an unrelated
                # API edit (name/params/enabled) does not re-anchor it.
                next_run = existing.next_run
            else:
                try:
                    next_run = next_occurrence(task.cron, now)
                except ValueError:
                    logger.warning(
                        "Scheduled task '{}' (id={}) has invalid cron "
                        "'{}' — skipped.",
                        task.name,
                        task.id,
                        task.cron,
                    )
                    continue

            entries[task.id] = _CronEntry(
                scheduled_id=task.id,
                display_name=task.name,
                func_name=task.task,
                # JSONB column → plain dict (never a JSON string here).
                params=task.params or {},
                cron=task.cron,
                next_run=next_run,
            )
            logger.info(
                "Registered cron job: {} (id={}, cron='{}', next_run={})",
                task.name,
                task.id,
                task.cron,
                next_run,
            )

        self._entries = entries
        self._enqueue = enqueue

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    def _fire_due(self, now: datetime) -> None:
        """Enqueue every entry due at *now* and advance its next run.

        Missed windows (more than :data:`CRON_MISFIRE_GRACE_SECONDS` late)
        are skipped with a warning and advanced past *now* — never
        back-filled.  On-time occurrences enqueue through the manager's
        callback; an enqueue failure is logged but does not abort the tick.
        Advancing ``next_run`` before returning makes a repeated
        ``_fire_due`` with the same *now* a no-op (no double-fire).
        """
        for entry in list(self._entries.values()):
            while entry.next_run <= now:
                late = (now - entry.next_run).total_seconds()
                if late > CRON_MISFIRE_GRACE_SECONDS:
                    logger.warning(
                        "Cron task '{}' (id={}, cron='{}') missed its "
                        "scheduled run at {} ({}s late) — skipped, next run "
                        "advanced past now.",
                        entry.display_name,
                        entry.scheduled_id,
                        entry.cron,
                        entry.next_run,
                        late,
                    )
                    entry.next_run = next_occurrence(entry.cron, now)
                    break

                logger.info("Cron triggered: {}", entry.display_name)
                try:
                    self._enqueue(
                        entry.display_name,
                        entry.func_name,
                        params=entry.params,
                    )
                except Exception:
                    logger.exception(
                        "Failed to enqueue cron task '{}' (id={}).",
                        entry.display_name,
                        entry.scheduled_id,
                    )
                entry.next_run = next_occurrence(entry.cron, entry.next_run)


# ==== 分区: manager ====

# Thin composition of the task kernel (docs/MODULARITY.md §M8):

# - :class:`TaskHistoryRecorder` — task_history row ownership
# - :class:`TaskExecutor` — context building + execution + lifecycle
# - :class:`CronTicker` / :func:`next_occurrence` — in-memory cron tick loop
# - :class:`TaskSpec` / :func:`discover_tasks` — task manifests + discovery

# The manager owns exactly one worker coroutine.  ``task_history`` is the
# queue: every entry point (manual API runs, cron fires, scheduled "run
# now") inserts a ``pending`` row and wakes the worker, which repeatedly
# selects the oldest pending row and runs it to completion.  Serialization
# is therefore structural — there is no lock, no per-enqueue asyncio task
# and no in-memory run list.  ``enqueue`` → ``Event.set()`` gives immediate
# dispatch; the worker's 1s poll is the fallback for anything missed.

# Business tasks are discovered via :func:`discover_tasks` (the built-in
# task module list), receive a validated Pydantic args object plus
# a :class:`TaskContext`, and return a :class:`TaskResult` so the notifier
# knows whether a task discovered novels or performed maintenance — no more
# guessing based on ``isinstance(result, list)``.


@dataclass
class _PendingTask:
    """Snapshot of one pending history row (read with the session open)."""

    task_id: int
    name: str
    task_func: str | None
    arguments: str | None


class TaskManagerSystem:
    """Manages background tasks and scheduled cron jobs.

    On ``start()``, recovers stale history rows (a previous process's
    leftover ``pending``/``running`` rows → ``interrupted``, never resumed),
    starts the queue worker, then loads the cron entries and starts the
    in-process tick loop.  Manual runs (via the API) and cron fires share
    the same enqueue path — a ``pending`` history row — so history is
    recorded consistently and rows execute in enqueue (id) order, one at a
    time.  A process-local ``_adhoc_specs`` map carries explicit-function
    runs, whose ``TaskSpec`` cannot be recovered from the DB row alone.

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
            },
        )
        self._ticker = CronTicker(session_factory)

        # Worker dispatch.  ``_loop`` is captured in ``start()``; sync
        # callers (threadpool endpoints) hand the wake-up back to that loop
        # via ``call_soon_threadsafe``.  ``_adhoc_specs`` holds the
        # TaskSpec of explicit-function runs keyed by task id, because the
        # DB row only stores the name (populated before the wake, popped
        # when the worker picks the row up).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        # One worker per queue lane (see ``_worker_loop``).  Kept as a list so
        # start/stop stay symmetric.
        self._worker_tasks: list[asyncio.Task] = []
        self._stopping = False
        self._adhoc_specs: dict[int, TaskSpec] = {}
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Recover stale rows, start the queue worker, then the ticker."""
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        # asyncio primitives bind to the loop that first waits on them;
        # lifespan restarts (e.g. TestClient) may run on a fresh loop, so
        # rebind the wake event while no worker exists yet.
        self._wake = asyncio.Event()
        self._stopping = False
        # Recovery must happen before the worker starts: leftover
        # pending/running rows are crash artifacts, never resumed.
        self._recorder.recover_stale()
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(fetch_lane))
            for fetch_lane in (True, False)
        ]
        self._ticker.start(self.run_scheduled)
        self._started = True
        logger.info(
            "TaskManagerSystem started — 2 queue lanes (fetch/general) "
            "running, cron jobs loaded."
        )

    async def stop(self) -> None:
        """Stop the cron ticker and cancel the queue worker."""
        self._stopping = True
        self._loop = None
        await self._ticker.stop()

        workers, self._worker_tasks = self._worker_tasks, []
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with suppress(asyncio.CancelledError):
                await worker

        # Explicit-function specs live only for the enqueue → run handoff.  On
        # shutdown the worker will never pick them up, so drop them: the rows
        # stay in the DB (recovered as interrupted on next start) and holding
        # the closures would leak one TaskSpec per ad-hoc enqueue forever.
        self._adhoc_specs.clear()
        self._recorder._live_pending.clear()

        self._started = False
        logger.info("TaskManagerSystem stopped.")

    # ------------------------------------------------------------------
    # Internal: the queue worker
    # ------------------------------------------------------------------

    async def _worker_loop(self, fetch_lane: bool) -> None:
        """Drain one queue lane, one row at a time, oldest first.

        There are two lanes over the same ``task_history`` queue:

        * ``fetch_lane=True``  — Pixiv-fetching tasks (:func:`is_fetch_task`);
        * ``fetch_lane=False`` — everything else, including ad-hoc runs and
          legacy ``NULL`` rows.

        The split exists because a single worker made a 30-minute fetch block
        a 5-second export.  Serialization is still per lane (one row in, one
        row out), so the queue semantics the UI promises are unchanged, and
        the two lanes never touch the same Pixiv account concurrently because
        only the fetch lane talks to Pixiv at all.

        Sleeps on the wake event with a 1s polling fallback (covers rows
        inserted by a path that did not wake us).  Unexpected exceptions are
        logged and end the current drain pass — the worker itself must never
        die — while cancellation propagates (the executor records the
        in-flight row as ``interrupted`` on its way out).
        """
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                while not self._stopping:
                    pending = self._next_pending(fetch_lane)
                    if pending is None:
                        break
                    await self._run_pending(pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let the worker die: log and retry on the next pass
                # (the row, if any, stays pending for that retry).
                logger.exception(
                    "Task worker iteration failed (%s lane) — will retry.",
                    "fetch" if fetch_lane else "general",
                )

    def _next_pending(self, fetch_lane: bool) -> _PendingTask | None:
        """Return the oldest pending row for one lane as a detached snapshot."""
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            if fetch_lane:
                row = repo.get_next_pending_sync(
                    task_funcs=list(FETCH_TASK_PREFIXES),
                )
            else:
                # Exclusion happens in SQL: a lane must not stall because the
                # oldest pending row belongs to the other lane.
                row = repo.get_next_pending_sync(
                    exclude_funcs=list(FETCH_TASK_PREFIXES),
                )
            if row is None:
                return None
            return _PendingTask(
                task_id=row.id,
                name=row.name,
                task_func=row.task_func,
                arguments=row.arguments,
            )

    async def _run_pending(self, pending: _PendingTask) -> None:
        """Resolve, parse and execute one pending history row.

        The ``arguments`` column is TEXT: an unparseable (or non-object)
        value fails the row with a clear summary instead of running the
        task with silently empty arguments.
        """
        spec = self._adhoc_specs.pop(pending.task_id, None)
        if spec is None and pending.task_func:
            spec = get_spec(pending.task_func)
        if spec is None:
            summary = f"未知任务函数：{pending.task_func or '(空)'}"
            logger.error(
                "Task '{}' (id={}) cannot run: {}", pending.name,
                pending.task_id, summary,
            )
            await self._recorder.update(
                pending.task_id, "failed",
                result=json.dumps({"summary": summary}, ensure_ascii=False),
            )
            return

        params: Any = {}
        if pending.arguments:
            try:
                params = json.loads(pending.arguments)
            except json.JSONDecodeError:
                params = None
        if not isinstance(params, dict):
            summary = "任务参数解析失败：arguments 不是合法的 JSON 对象"
            logger.error(
                "Task '{}' (id={}) cannot run: {}", pending.name,
                pending.task_id, summary,
            )
            await self._recorder.update(
                pending.task_id, "failed",
                result=json.dumps({"summary": summary}, ensure_ascii=False),
            )
            return

        await self._executor.run_and_record(
            spec, params, pending.task_id,
            recorder=self._recorder,
            notifier=self._notifier,
        )

    def _wake_worker(self) -> None:
        """Wake the worker from sync code (loop thread or threadpool).

        The loop may be absent (enqueue before ``start()``/after
        ``stop()``): the worker's 1s poll will catch the row then.  Sync
        API endpoints run in a threadpool, hence ``call_soon_threadsafe``;
        a shutting-down loop must not raise into the caller.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            self._wake.set()
            return

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            self._wake.set()
            return

        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            # Loop closed between the check and the call — the poll covers it.
            with suppress(RuntimeError):
                self._wake.set()

    def reload_cron_jobs(self) -> None:
        """Reload cron entries from the database (no ticker restart)."""
        self._ticker.reload_cron_jobs(self.run_scheduled)

    # ------------------------------------------------------------------
    # Task execution (enqueue)
    # ------------------------------------------------------------------

    def run_task(
        self,
        name: str,
        func: Callable | None = None,
        params: dict | None = None,
    ) -> int:
        """Enqueue a task for background execution (single worker queue).

        Entry point for manually-triggered runs — batch endpoints, tests,
        and ad-hoc callers that enqueue an explicit function.  Cron-
        triggered runs and the scheduled "run now" endpoint go through
        :meth:`run_scheduled` (which records history under the scheduled
        row's display name and resolves the function by its task column).
        The row is inserted as ``pending`` and the worker executes it in
        enqueue order; there is no dedup.

        Args:
            name: Task function name (``TaskSpec.name``), recorded as
                ``task_history.task_func``.  When *func* is None the registry
                manifest is resolved by this name (unknown names raise
                ``ValidationError``).
            func: Explicit task function — used by tests and legacy
                callers that enqueue ad-hoc functions.  Explicit functions
                run without an args model (params are ignored by the
                executor); their ``TaskSpec`` is kept in memory because the
                history row only stores the name.
            params: JSON-serializable argument dict, validated against the
                task's Pydantic args model at execution time.

        Returns:
            The new task-history row id (the task id).
        """
        if func is not None:
            # Same contract as @register: only coroutine functions can honour
            # TIMEOUT_SECONDS (threads cannot be cancelled).
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"Ad-hoc task {name!r} must be an async def function."
                )
            spec = TaskSpec(name=name, description="", func=func)
        else:
            spec = get_spec(name)
            if spec is None:
                raise ValidationError(f"Unknown task function: {name}")
        return self._enqueue(name, spec, params, adhoc=func is not None)

    def _enqueue(
        self,
        name: str,
        spec: TaskSpec,
        params: dict | None,
        *,
        adhoc: bool = False,
    ) -> int:
        """Shared enqueue path: insert the history row, then wake the worker.

        ``name`` is the display name recorded on the history row; ``spec.name``
        (the registered function name) is recorded as ``task_func``.  The
        manual path uses the same value for both (spec.name == name); the
        scheduled path passes display_name as *name* and resolves spec by the
        function name.  No dedup: a second enqueue inserts another pending row.

        Stays synchronous (the API signature is unchanged).  An *adhoc*
        run's ``TaskSpec`` is registered in ``_adhoc_specs`` before the
        wake, so the worker has it when it follows the wake-up.
        """
        task_id = self._recorder.enqueue(name, params or {}, spec.name)
        if adhoc:
            self._adhoc_specs[task_id] = spec
        self._wake_worker()
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
        function name.

        Raises:
            ValidationError: *func_name* is not a registered task function.
        """
        spec = get_spec(func_name)
        if spec is None:
            raise ValidationError(f"Unknown task function: {func_name}")
        return self._enqueue(display_name, spec, params)

    def run_task_now(self, task_id: int) -> None:
        """Look up a scheduled task by DB id and run it immediately.

        Used by the ``POST /api/tasks/scheduled/{id}/run`` endpoint.

        Raises:
            NotFoundError: unknown scheduled-task id.
            ValidationError: the scheduled task references an unknown
                task function.
        """
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            tasks = repo.get_scheduled_tasks_sync()
            task = next((t for t in tasks if t.id == task_id), None)
            if task is None:
                raise NotFoundError(f"Scheduled task id={task_id} not found.")
            # ``scheduled_task.params`` is JSONB → already a dict.
            params = task.params or {}

        # The scheduled row's *name* is a display label; the function to run
        # comes from its ``task`` column.  Delegate to run_scheduled so the
        # cron path and this endpoint share one enqueue path (history records
        # the display name, the function and args model are resolved by the
        # task column; no dedup — repeated runs queue).
        self.run_scheduled(task.name, task.task, params)

