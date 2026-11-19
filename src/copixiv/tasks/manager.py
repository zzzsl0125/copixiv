"""Task manager facade and dual-lane queue worker."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.log import logger
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

from .cron import CronTicker
from .executor import TaskExecutor, TaskHistoryRecorder
from .registry import TaskLane, TaskSpec, discover_tasks, get_spec, task_names_for_lane


@dataclass
class _PendingTask:
    """Snapshot of one pending history row (read with the session open)."""

    task_id: int
    name: str
    task_func: str | None
    arguments: str | None


@dataclass(frozen=True)
class ExportTaskInfo:
    """Export task metadata looked up via :meth:`TaskManagerSystem.get_export_info`."""

    task_id: int
    status: str
    task_func: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def zip_name(self) -> str | None:
        raw = self.arguments.get("zip_name")
        if isinstance(raw, str):
            stripped = raw.strip()
            if stripped:
                return stripped
        return None


class TaskManagerSystem:
    """Manages background tasks and scheduled cron jobs."""

    def __init__(
        self,
        session_factory,
        client=None,
        file_storage=None,
        image_downloader=None,
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
                "backup_dir": (config.backup.dir if config is not None else "backups"),
            },
        )
        self._ticker = CronTicker(session_factory)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: dict[TaskLane, asyncio.Event] = {}
        self._worker_tasks: list[asyncio.Task] = []
        self._active_tasks: dict[int, asyncio.Task[Any]] = {}
        self._cancelled_ids: set[int] = set()
        self._stopping = False
        self._started = False

    async def start(self) -> None:
        """Recover stale rows, start both dual-lane queue workers, then the ticker."""
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = {
            "fetch": asyncio.Event(),
            "general": asyncio.Event(),
        }
        self._active_tasks.clear()
        self._cancelled_ids.clear()
        self._stopping = False
        self._recorder.recover_stale()
        self._worker_tasks = [asyncio.create_task(self._worker_loop(lane)) for lane in ("fetch", "general")]
        self._ticker.start(self.run_scheduled)
        self._started = True
        logger.info("TaskManagerSystem started — 2 queue lanes (fetch/general) running, cron jobs loaded.")

    async def stop(self) -> None:
        """Stop the cron ticker, cancel active tasks and workers."""
        self._stopping = True
        self._loop = None
        await self._ticker.stop()

        active = list(self._active_tasks.values())
        workers, self._worker_tasks = self._worker_tasks, []
        for task in active:
            task.cancel()
        for worker in workers:
            worker.cancel()
        for task in active:
            with suppress(asyncio.CancelledError):
                await task
        for worker in workers:
            with suppress(asyncio.CancelledError):
                await worker

        self._active_tasks.clear()
        self._cancelled_ids.clear()
        self._recorder._live_pending.clear()
        self._started = False
        logger.info("TaskManagerSystem stopped.")

    async def _worker_loop(self, lane: TaskLane) -> None:
        """Drain pending rows for one queue lane."""
        event = self._wake[lane]
        while not self._stopping:
            if not event.is_set():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=1.0)
            event.clear()
            try:
                while not self._stopping:
                    pending = await asyncio.to_thread(
                        self._next_pending,
                        lane,
                    )
                    if pending is None:
                        break
                    await self._run_pending(pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Task worker iteration failed (%s lane) — will retry.",
                    lane,
                )

    def _next_pending(self, lane: TaskLane) -> _PendingTask | None:
        """Return the oldest pending row for *lane* as a detached snapshot."""
        fetch_names = list(task_names_for_lane("fetch"))
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            if lane == "fetch":
                if not fetch_names:
                    return None
                row = repo.get_next_pending(task_funcs=fetch_names)
            else:
                row = repo.get_next_pending(exclude_funcs=fetch_names)
            if row is None:
                return None
            return _PendingTask(
                task_id=row.id,
                name=row.name,
                task_func=row.task_func,
                arguments=row.arguments,
            )

    async def _run_pending(self, pending: _PendingTask) -> None:
        """Resolve, parse and execute one pending history row."""
        spec = get_spec(pending.task_func) if pending.task_func else None
        if spec is None:
            summary = f"未知任务函数：{pending.task_func or '(空)'}"
            logger.error(
                "Task '{}' (id={}) cannot run: {}",
                pending.name,
                pending.task_id,
                summary,
            )
            await self._recorder.update(
                pending.task_id,
                "failed",
                result={"summary": summary},
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
                "Task '{}' (id={}) cannot run: {}",
                pending.name,
                pending.task_id,
                summary,
            )
            await self._recorder.update(
                pending.task_id,
                "failed",
                result={"summary": summary},
            )
            return

        task = asyncio.create_task(
            self._executor.run_and_record(
                spec,
                params,
                pending.task_id,
                recorder=self._recorder,
                notifier=self._notifier,
                is_user_cancelled=lambda tid: tid in self._cancelled_ids,
            )
        )
        self._active_tasks[pending.task_id] = task
        try:
            await task
        except asyncio.CancelledError:
            if self._stopping:
                raise
            logger.info(
                "Task '{}' (id={}) cancelled by user.",
                pending.name,
                pending.task_id,
            )
        finally:
            self._active_tasks.pop(pending.task_id, None)
            self._cancelled_ids.discard(pending.task_id)

    def _wake_worker(self, lane: TaskLane) -> None:
        """Wake the worker for *lane* from sync code (loop thread or threadpool)."""
        event = self._wake.get(lane)
        if event is None:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            event.set()
            return

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            event.set()
            return

        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            with suppress(RuntimeError):
                event.set()

    def reload_cron_jobs(self) -> None:
        """Reload cron entries from the database (no ticker restart)."""
        self._ticker.reload_cron_jobs(self.run_scheduled)

    def run_task(
        self,
        name: str,
        params: dict | None = None,
    ) -> int:
        """Enqueue a registered task for background execution."""
        spec = get_spec(name)
        if spec is None:
            raise ValidationError(f"Unknown task function: {name}")
        return self._enqueue(name, spec, params)

    def _enqueue(
        self,
        name: str,
        spec: TaskSpec,
        params: dict | None,
    ) -> int:
        """Insert the history row and wake the target lane worker."""
        task_id = self._recorder.enqueue(name, params or {}, spec.name)
        self._wake_worker(spec.lane)
        return task_id

    def run_scheduled(
        self,
        display_name: str,
        func_name: str,
        params: dict | None = None,
    ) -> int:
        """Enqueue a scheduled-task run by display name + function name."""
        spec = get_spec(func_name)
        if spec is None:
            raise ValidationError(f"Unknown task function: {func_name}")
        return self._enqueue(display_name, spec, params)

    def run_task_now(self, task_id: int) -> None:
        """Look up a scheduled task by DB id and run it immediately."""
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            tasks = repo.get_scheduled_tasks()
            task = next((t for t in tasks if t.id == task_id), None)
            if task is None:
                raise NotFoundError(f"Scheduled task id={task_id} not found.")
            display_name = task.name
            func_name = task.task
            params = task.params or {}

        self.run_scheduled(display_name, func_name, params)

    def cancel_task(self, task_id: int) -> str:
        """Cancel a pending or running background task by *task_id*."""
        active = self._active_tasks.get(task_id)
        if active is not None and not active.done():
            self._cancelled_ids.add(task_id)
            loop = self._loop
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if loop is not None and not loop.is_closed() and running_loop is not loop:
                loop.call_soon_threadsafe(active.cancel)
            else:
                active.cancel()
            return "cancelling"

        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            if repo.cancel_pending(task_id):
                session.commit()
                self._recorder._live_pending.discard(task_id)
                return "cancelled"
            status_row = repo.get_task_status(task_id)
        if status_row is None:
            raise NotFoundError(f"Task {task_id} not found")
        return status_row[0]

    def get_export_info(self, task_id: int) -> ExportTaskInfo | None:
        """Return metadata for an export task row (used by routes/batch)."""
        with self._session_factory() as session:
            status_row = SQLAlchemyTaskRepository(session).get_task_status(task_id)
        if status_row is None:
            return None
        status, task_func, raw_args = status_row
        parsed_args: dict[str, Any] = {}
        if isinstance(raw_args, dict):
            parsed_args = raw_args
        elif isinstance(raw_args, str) and raw_args:
            try:
                loaded = json.loads(raw_args)
                if isinstance(loaded, dict):
                    parsed_args = loaded
            except (json.JSONDecodeError, TypeError):
                pass
        return ExportTaskInfo(
            task_id=task_id,
            status=status,
            task_func=task_func,
            arguments=parsed_args,
        )
