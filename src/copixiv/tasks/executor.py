"""Task execution context, history recorder, and task executor."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from copixiv.core.models import TaskResult
from copixiv.db import models
from copixiv.db.tx import run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest.asset_pipeline import NovelAssetPipeline
from copixiv.log import capture_logs, logger
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository

from .registry import TaskSpec


@dataclass
class TaskContext:
    """All services a task may need, provided by the task kernel."""

    uow: SqlUnitOfWork | None = None
    session_factory: Any = None  # SQLAlchemy sessionmaker（tasks 层不 import app 层）
    client: PixivClient | None = None
    file_storage: FileStorage | None = None
    image_downloader: NovelAssetPipeline | None = None
    backup_dir: str = "backups"
    task_id: int | None = None
    source_registry: Any = None
    source_service: Any = None
    http_manager: Any = None

    def child_uow(self) -> SqlUnitOfWork:
        """Return a fresh UnitOfWork sharing the process session factory."""
        if self.session_factory is None:
            raise RuntimeError(
                "TaskContext.session_factory is not set — the context was not built by the task kernel."
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

    async def report_progress(self, progress: Any) -> None:
        """Write live progress into ``task_history`` via an isolated short session.

        Uses ``self.child_uow()`` (never ``self.uow``) so concurrent callbacks
        or background progress writers cannot race on the task's primary UoW.
        Never touches ``status`` / ``end_time`` and never raises.
        """
        task_id = self.task_id
        if task_id is None:
            return
        target_uow = self.child_uow() if self.session_factory is not None else self.uow
        if target_uow is None:
            return
        try:

            def _write(uw: SqlUnitOfWork) -> None:
                SQLAlchemyTaskRepository(uw.session).update_task(
                    task_id,
                    None,
                    progress=progress,
                )

            await run_write_transaction(target_uow, _write)
        except Exception:  # noqa: BLE001 — progress must never kill the task
            logger.exception("任务进度更新失败（不影响执行）")


class TaskHistoryRecorder:
    """Records task lifecycle rows (enqueue + status/result updates)."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def enqueue(self, name: str, params: dict, task_func: str) -> int:
        """Insert the pending row; returns the new task id."""
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            task_id = repo.add_task(name, params, task_func)
            session.commit()
        logger.info("Task '{}' (id={}) enqueued.", name, task_id)
        return task_id

    async def update(
        self,
        task_id: int,
        status: str | None,
        result: dict[str, Any] | None = None,
        duration: float | None = None,
        progress: Any | None = None,
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

    def _update_sync(
        self,
        task_id: int,
        status: str | None,
        result: dict[str, Any] | None = None,
        duration: float | None = None,
        progress: Any | None = None,
    ) -> None:
        """Synchronous body of :meth:`update` (runs in a worker thread)."""
        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            repo.update_task(
                task_id,
                status,
                result=result,
                duration=duration,
                progress=progress,
            )
            session.commit()

    def recover_stale(self) -> None:
        """Mark tasks stuck in pending/running as interrupted."""
        try:
            with self._session_factory() as session:
                stale_ids = (
                    session.execute(
                        update(models.TaskHistory)
                        .where(models.TaskHistory.status.in_(("pending", "running")))
                        .values(
                            status="interrupted",
                            end_time=datetime.now(UTC),
                            result={"summary": "服务重启导致中断"},
                        )
                        .returning(models.TaskHistory.id)
                    )
                    .scalars()
                    .all()
                )
                if not stale_ids:
                    return
                session.commit()
                logger.warning(
                    "Recovered {} stale task history rows → interrupted.",
                    len(stale_ids),
                )
        except Exception:
            logger.exception("Failed to recover stale task history rows.")


def _resolve_source_timeout(
    source_service: Any,
    sources: Sequence[str],
    default_timeout: float,
) -> float | None:
    """Resolve effective task timeout from source runtime configs (``<= 0`` means unlimited)."""
    if source_service is None or not sources:
        return default_timeout

    resolved: list[float] = []
    for raw_source in dict.fromkeys(s.strip() for s in sources if isinstance(s, str) and s.strip()):
        try:
            runtime = source_service.get_runtime_config(raw_source)
        except Exception:  # noqa: BLE001
            logger.exception("读取数据源 {} 运行配置失败，回退默认超时", raw_source)
            continue
        raw_val = runtime.values.get("task_timeout_sec")
        if raw_val is None or isinstance(raw_val, bool):
            continue
        try:
            timeout_val = float(raw_val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(timeout_val):
            continue
        if timeout_val <= 0:
            return None
        resolved.append(timeout_val)

    return max(resolved) if resolved else default_timeout


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
        is_user_cancelled: Callable[[int], bool] | None = None,
    ) -> None:
        """Execute a task, record history, and send notifications."""
        name = spec.name
        logger.info("Starting task '{}' (id={})...", name, task_id)
        await self._run_inner(
            spec,
            params,
            task_id,
            recorder=recorder,
            notifier=notifier,
            is_user_cancelled=is_user_cancelled,
        )

    async def _run_inner(
        self,
        spec: TaskSpec,
        params: dict,
        task_id: int,
        *,
        recorder: TaskHistoryRecorder,
        notifier,
        is_user_cancelled: Callable[[int], bool] | None = None,
    ) -> None:
        """Body of the task lifecycle — status tracking + execution."""
        name = spec.name
        start_time = time.time()
        status: str = "running"
        error_msg: str | None = None
        result_val: Any = None
        effective_timeout: float | None = spec.timeout if spec.timeout is not None else self.TIMEOUT_SECONDS

        await recorder.update(task_id, "running")

        with capture_logs(task_id=task_id) as get_logs:
            try:
                ctx = self._build_context(task_id)
                args_obj = spec.args_model.model_validate(params) if spec.args_model is not None else None
                effective_timeout = await self._resolve_timeout(spec, args_obj, ctx)
                result_val = await self._run_with_timeout(spec, args_obj, ctx, effective_timeout)
                status = "success"
                logger.info(
                    "Task '{}' (id={}) completed successfully.",
                    name,
                    task_id,
                )

            except TimeoutError:
                status = "failed"
                error_msg = (
                    f"Task timed out after {effective_timeout:g}s"
                    if effective_timeout is not None
                    else "Task timed out"
                )
                logger.error("Task '{}' (id={}) timed out.", name, task_id)
            except asyncio.CancelledError:
                duration = time.time() - start_time
                user_cancelled = is_user_cancelled(task_id) if is_user_cancelled is not None else False
                cancel_status = "cancelled" if user_cancelled else "interrupted"
                cancel_summary = "任务已手动取消" if user_cancelled else "任务被中断（服务关闭/重启）"
                try:
                    await recorder.update(
                        task_id,
                        cancel_status,
                        result={
                            "log": get_logs(),
                            "summary": cancel_summary,
                            "new_novels_count": 0,
                            "new_novel_titles": [],
                        },
                        duration=duration,
                    )
                except Exception:
                    logger.exception(
                        "Failed to record %s status for task '{}' (id={}).",
                        cancel_status,
                        name,
                        task_id,
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

        if status == "failed" and not result.failed_ids and (not result.summary or result.summary == "完成"):
            result = result.model_copy(
                update={"summary": error_msg or "任务失败（未记录原因）"},
            )

        result_data = {
            "log": log_output,
            "summary": result.summary,
            "new_novels_count": result.new_novel_count,
            "new_novel_titles": result.new_novel_titles,
            "failed_ids": result.failed_ids,
        }

        await recorder.update(
            task_id,
            status,
            result=result_data,
            duration=duration,
        )

        if notifier is not None and spec.notify:
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
            source_registry=self._deps.get("source_registry"),
            source_service=self._deps.get("source_service"),
            http_manager=self._deps.get("http_manager"),
        )

    async def _resolve_timeout(
        self,
        spec: TaskSpec,
        args_obj: Any,
        ctx: TaskContext,
    ) -> float | None:
        if spec.timeout is not None:
            return spec.timeout if spec.timeout > 0 else None
        if spec.timeout_sources is not None:
            raw_sources = spec.timeout_sources(args_obj, ctx)
            sources = await raw_sources if inspect.isawaitable(raw_sources) else raw_sources
            return _resolve_source_timeout(ctx.source_service, sources, self.TIMEOUT_SECONDS)
        if spec.source is not None:
            return _resolve_source_timeout(ctx.source_service, (spec.source,), self.TIMEOUT_SECONDS)
        return self.TIMEOUT_SECONDS

    async def _run_with_timeout(
        self,
        spec: TaskSpec,
        args_obj: Any,
        ctx: TaskContext,
        timeout: float | None,
    ) -> Any:
        coro = spec.func(ctx=ctx) if args_obj is None else spec.func(args_obj, ctx=ctx)
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)

    async def _execute(self, spec: TaskSpec, params: dict, ctx: TaskContext) -> Any:
        """Validate params against the args model and run the function."""
        args_obj = spec.args_model.model_validate(params) if spec.args_model is not None else None
        timeout = await self._resolve_timeout(spec, args_obj, ctx)
        return await self._run_with_timeout(spec, args_obj, ctx, timeout)

    @staticmethod
    def _normalize_result(result_val: Any) -> TaskResult:
        """Convert a task's return value into a :class:`TaskResult`."""
        if isinstance(result_val, TaskResult):
            return result_val
        if result_val is None:
            return TaskResult(summary="完成")
        return TaskResult(summary=str(result_val))
