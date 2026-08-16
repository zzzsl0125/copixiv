"""Task executor — the kernel that turns a TaskSpec into a completed run.

Builds the :class:`TaskContext`, validates JSON params against the task's
Pydantic args model, executes with a 30-minute timeout, and drives the
full lifecycle around it (history rows, log capture, notification).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import time
import traceback
from typing import Any

from copixiv.domain.models.task_result import TaskResult
from copixiv.log import capture_logs, logger

from .context import TaskContext
from .registry import TaskSpec


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
        running_names: set[str],
    ) -> None:
        """Execute a task, record history, and send notifications.

        The duplicate-run guard lives in the caller (enqueue path); this
        method owns everything from the first "running" row to the final
        notification, releasing the in-process guard in every exit path.
        """
        name = spec.name
        logger.info("Starting task '{}' (id={})...", name, task_id)
        try:
            await self._run_inner(
                spec, params, task_id,
                recorder=recorder, notifier=notifier,
            )
        finally:
            # Release the in-process duplicate-run guard in every exit
            # path (success / failure / cancellation).
            running_names.discard(name)

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
        from copixiv.infrastructure.database.uow import SqlUnitOfWork

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
