"""Task execution context, history recorder, and task executor."""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from copixiv.core.models import TaskResult
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import db_write
from copixiv.log import capture_logs, logger
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

from .registry import TaskSpec


@dataclass
class TaskContext:
    """All services a task may need, provided by the task kernel."""

    uow: SqlUnitOfWork | None = None
    session_factory: Any = None  # SQLAlchemy sessionmaker（tasks 层不 import app 层）
    client: PixivClient | None = None
    file_storage: FileStorage | None = None
    image_downloader: ImageDownloader | None = None
    backup_dir: str = "backups"
    task_id: int | None = None

    def child_uow(self) -> SqlUnitOfWork:
        """Return a fresh UnitOfWork sharing the process session factory."""
        if self.session_factory is None:
            raise RuntimeError(
                "TaskContext.session_factory is not set — the context was "
                "not built by the task kernel."
            )
        return SqlUnitOfWork(self.session_factory)

    def with_uow(self, uow: SqlUnitOfWork) -> TaskContext:
        """Return a copy of this context with *uow* replaced."""
        return replace(self, uow=uow)

    def require_uow(self) -> SqlUnitOfWork:
        """Return the active UnitOfWork, raising loudly if absent."""
        if self.uow is None:
            raise RuntimeError("TaskContext.uow is not set")
        return self.uow

    def require_client(self) -> PixivClient:
        """Return the PixivClient, raising loudly if absent."""
        if self.client is None:
            raise RuntimeError("TaskContext.client is not set")
        return self.client

    def require_file_storage(self) -> FileStorage:
        """Return the FileStorage, raising loudly if absent."""
        if self.file_storage is None:
            raise RuntimeError("TaskContext.file_storage is not set")
        return self.file_storage


class TaskHistoryRecorder:
    """Records task lifecycle rows (enqueue + status/result updates)."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._live_pending: set[int] = set()

    def enqueue(self, name: str, params: dict, task_func: str) -> int:
        """Insert the pending row; returns the new task id."""
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            task_id = repo.add_task(name, params, task_func)
            session.commit()
        self._live_pending.add(task_id)
        logger.info("Task '{}' (id={}) enqueued.", name, task_id)
        return task_id

    async def update(
        self,
        task_id: int,
        status: str | None,
        result: str | None = None,
        duration: float | None = None,
        progress: str | None = None,
    ) -> None:
        """Update a TaskHistory row in a short write transaction."""
        await asyncio.to_thread(
            self._update_sync,
            task_id,
            status,
            result=result,
            duration=duration,
            progress=progress,
        )
        if status is not None and status != "pending":
            self._live_pending.discard(task_id)

    def _update_sync(
        self,
        task_id: int,
        status: str | None,
        result: str | None = None,
        duration: float | None = None,
        progress: str | None = None,
    ) -> None:
        """Synchronous body of :meth:`update` (runs in a worker thread)."""
        with db_write(), self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            repo.update_task(
                task_id, status, result=result, duration=duration,
                progress=progress,
            )
            session.commit()

    def _prune_live_pending(self) -> None:
        """Drop ids this process no longer owns from ``_live_pending``."""
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
        """Mark tasks stuck in pending/running as interrupted."""
        try:
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


class TaskExecutor:
    """Executes task functions with context injection and a timeout."""

    TIMEOUT_SECONDS = 1800  # 30 minutes

    def __init__(self, session_factory, deps: dict):
        self._session_factory = session_factory
        self._deps = deps

    async def run_and_record(
        self,
        spec: TaskSpec,
        params: dict,
        task_id: int,
        *,
        recorder: TaskHistoryRecorder,
        notifier,
    ) -> None:
        """Execute a task, record history, and send notifications."""
        name = spec.name
        logger.info("Starting task '{}' (id={})...", name, task_id)
        await self._run_inner(
            spec, params, task_id,
            recorder=recorder, notifier=notifier,
        )

    async def _run_inner(
        self,
        spec: TaskSpec,
        params: dict,
        task_id: int,
        *,
        recorder: TaskHistoryRecorder,
        notifier,
    ) -> None:
        """Body of the task lifecycle — status tracking + execution."""
        name = spec.name
        start_time = time.time()
        status: str = "running"
        error_msg: str | None = None
        result_val: Any = None

        await recorder.update(task_id, "running")

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
                "failed_ids": result.failed_ids,
            },
            ensure_ascii=False,
        )

        await recorder.update(
            task_id, status, result=result_data, duration=duration,
        )

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
            backup_dir=self._deps.get("backup_dir", "backups"),
            task_id=task_id,
        )

    async def _execute(
        self, spec: TaskSpec, params: dict, ctx: TaskContext
    ) -> Any:
        """Validate params against the args model and run the function."""
        args_obj = (
            spec.args_model.model_validate(params)
            if spec.args_model is not None
            else None
        )

        if args_obj is None:
            coro = spec.func(ctx=ctx)
        else:
            coro = spec.func(args_obj, ctx=ctx)
        return await asyncio.wait_for(coro, timeout=self.TIMEOUT_SECONDS)

    @staticmethod
    def _normalize_result(result_val: Any) -> TaskResult:
        """Convert a task's return value into a :class:`TaskResult`."""
        if isinstance(result_val, TaskResult):
            return result_val
        if result_val is None:
            return TaskResult(summary="完成")
        return TaskResult(summary=str(result_val))
