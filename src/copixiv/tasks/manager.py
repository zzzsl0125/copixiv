"""Task manager facade and dual-lane queue worker."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.log import logger
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

from .cron import CronTicker
from .executor import TaskExecutor, TaskHistoryRecorder
from .registry import FETCH_TASK_NAMES, TaskSpec, discover_tasks, get_spec


@dataclass
class _PendingTask:
    """Snapshot of one pending history row (read with the session open)."""

    task_id: int
    name: str
    task_func: str | None
    arguments: str | None


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
                "backup_dir": (
                    config.backup.dir if config is not None else "backups"
                ),
            },
        )
        self._ticker = CronTicker(session_factory)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._worker_tasks: list[asyncio.Task] = []
        self._stopping = False
        self._started = False

    async def start(self) -> None:
        """Recover stale rows, start the queue worker, then the ticker."""
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stopping = False
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

        self._recorder._live_pending.clear()
        self._started = False
        logger.info("TaskManagerSystem stopped.")

    async def _worker_loop(self, fetch_lane: bool) -> None:
        """Drain pending rows for one queue lane."""
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                while not self._stopping:
                    pending = await asyncio.to_thread(
                        self._next_pending, fetch_lane,
                    )
                    if pending is None:
                        break
                    await self._run_pending(pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Task worker iteration failed (%s lane) — will retry.",
                    "fetch" if fetch_lane else "general",
                )

    def _next_pending(self, fetch_lane: bool) -> _PendingTask | None:
        """Return the oldest pending row for one lane as a detached snapshot."""
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            if fetch_lane:
                row = repo.get_next_pending(
                    task_funcs=list(FETCH_TASK_NAMES),
                )
            else:
                row = repo.get_next_pending(
                    exclude_funcs=list(FETCH_TASK_NAMES),
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
        """Resolve, parse and execute one pending history row."""
        spec = get_spec(pending.task_func) if pending.task_func else None
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
        """Wake the worker from sync code (loop thread or threadpool)."""
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
            with suppress(RuntimeError):
                self._wake.set()

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
        """Insert the history row and wake the worker."""
        task_id = self._recorder.enqueue(name, params or {}, spec.name)
        self._wake_worker()
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
            params = task.params or {}

        self.run_scheduled(task.name, task.task, params)
