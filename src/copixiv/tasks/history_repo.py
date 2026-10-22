"""Task repository — history and scheduled tasks."""

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import case, func, select, text as sa_text
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemyTaskRepository(BaseRepository):
    """Repository for task history and scheduled tasks."""

    def __init__(self, session: Session):
        super().__init__(session)

    # -- task history ------------------------------------------------------

    async def add_task(self, name: str, arguments: dict, task_func: str) -> int:
        return self.add_task_sync(name, arguments, task_func)

    def add_task_sync(self, name: str, arguments: dict, task_func: str) -> int:
        """Insert a pending history row under *name* (display) + *task_func*.

        *task_func* is the registered function name, kept for history/audit.
        There is no dedup index: repeated enqueues insert separate pending
        rows (the queue ordering is the worker's job).
        """
        task = models.TaskHistory(
            name=name,
            task_func=task_func,
            arguments=json.dumps(arguments, ensure_ascii=False),
            status="pending",
            # timestamptz — store an aware UTC datetime (not an ISO string).
            start_time=datetime.now(timezone.utc),
        )
        self.session.add(task)
        self.session.flush()
        return task.id

    async def update_task(
        self, task_id: int, status: str | None, result: str | None = None,
        duration: float | None = None, progress: str | None = None,
    ) -> None:
        self.update_task_sync(
            task_id, status, result=result, duration=duration, progress=progress,
        )

    def update_task_sync(
        self, task_id: int, status: str | None, result: str | None = None,
        duration: float | None = None, progress: str | None = None,
    ) -> None:
        """Update one history row; ``status=None`` means "progress only".

        A progress-only write must not touch ``status`` / ``end_time``.  The
        batch tasks report progress through a fire-and-forget
        ``run_coroutine_threadsafe`` (see ``tasks/batch.py``), so a late
        progress write can land *after* the executor recorded the terminal
        status.  Without this guard it would rewrite a finished row back to
        ``running`` and leave the old ``end_time`` behind — the UI then shows
        an eternally running task whose button stays disabled.
        """
        task = self.session.get(models.TaskHistory, task_id)
        if task is not None:
            if status is not None:
                task.status = status
            # end_time only on terminal states — "running" rows must not
            # carry an end timestamp, and a progress-only update never
            # touches either column.
            if status in ("success", "failed", "interrupted"):
                task.end_time = datetime.now(timezone.utc)
            if result is not None:
                # result/progress are JSONB columns.  Store a parsed dict so
                # reads return structured data (the API response model already
                # parses a JSON string defensively, but the ORM should not
                # leave a raw JSON string behind either).
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        pass
                task.result = result
            # Live progress lives in its own column.  We only ever write
            # (never clear) progress here — callers that want to stop showing
            # it pass Nothing, and the front-end gates on status.  This keeps
            # old "running" rows and terminal rows with a stale progress
            # harmless instead of destructively wiping data.
            if progress is not None:
                task.progress = progress
            if duration is not None:
                task.duration = duration

    def get_next_pending_sync(
        self,
        task_funcs: Sequence[str] | None = None,
        exclude_funcs: Sequence[str] | None = None,
    ) -> models.TaskHistory | None:
        """Oldest pending queue row (FIFO) for one queue lane.

        ``task_funcs`` / ``exclude_funcs`` partition the queue between the two
        workers.  The exclusion is part of the query, not a post-filter: a
        lane must never stall just because the oldest pending row belongs to
        the other lane.  ``NULL task_func`` rows (legacy) are picked up by the
        unrestricted lane — the same rule the front-end busy flags use.
        """
        stmt = (
            select(models.TaskHistory)
            .where(models.TaskHistory.status == "pending")
            .order_by(models.TaskHistory.id)
            .limit(1)
        )
        if task_funcs is not None:
            stmt = stmt.where(models.TaskHistory.task_func.in_(task_funcs))
        if exclude_funcs:
            stmt = stmt.where(
                models.TaskHistory.task_func.not_in(exclude_funcs)
            )
        return self.session.execute(stmt).scalar_one_or_none()

    async def get_busy_funcs(self) -> list[str]:
        """Distinct ``task_func`` values with a pending/running row.

        One tiny query backing the front-end busy flags.  It exists so the
        poller no longer derives that set from a full history page (which it
        did with ``limit=100`` — a 7.4 MB response while anything was running,
        and blind to anything beyond the first 100 rows).
        """
        rows = self.session.execute(
            select(models.TaskHistory.task_func)
            .where(
                models.TaskHistory.status.in_(("pending", "running")),
                models.TaskHistory.task_func.isnot(None),
            )
            .distinct()
        ).scalars().all()
        return sorted(rows)

    async def get_history(
        self, limit: int = 50, offset: int = 0
    ) -> Sequence[models.TaskHistory]:
        stmt = (
            select(models.TaskHistory)
            .order_by(models.TaskHistory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    async def get_history_rows(
        self, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """History rows **without** the log payload, plus a ``has_log`` flag.

        The list endpoint used to return ``result`` verbatim, and the log
        inside it was 96 % of a 7.4 MB response for ``limit=100`` — pulled
        every 5 s by the front-end task-status poller while anything was
        running.  The log is only ever *displayed* after the user clicks
        「日志」, so ship the summary and a flag instead; the text itself comes
        from :meth:`get_task_log`.

        Server-side subtraction (``result - 'log'``) keeps the JSONB work in
        the database: no need to load up to 1.3 MB per row into Python just
        to drop it.
        """
        # ``result - 'log'`` raises "cannot delete from scalar" on the legacy
        # rows whose JSONB is a *string* (``recover_stale`` used to write a
        # ``json.dumps`` payload there).  Guard by type: those rows ship
        # unchanged and are simply not log-bearing.
        result_obj = models.TaskHistory.result
        is_object = sa_text("jsonb_typeof(task_history.result) = 'object'")
        stmt = (
            select(
                models.TaskHistory.id,
                models.TaskHistory.name,
                models.TaskHistory.task_func,
                models.TaskHistory.arguments,
                models.TaskHistory.status,
                models.TaskHistory.start_time,
                models.TaskHistory.end_time,
                models.TaskHistory.duration,
                # Drop the log server-side; the rest of the object is small.
                case(
                    (is_object, result_obj.op("-")("log")),
                    else_=result_obj,
                ).label("result"),
                case(
                    (is_object, result_obj.has_key("log")),
                    else_=sa_text("false"),
                ).label("has_log"),
                models.TaskHistory.progress,
            )
            .order_by(models.TaskHistory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = self.session.execute(stmt).all()
        return [dict(row._mapping) for row in rows]

    async def get_task_log(self, task_id: int) -> str | None:
        """Return just the stored log text for one row (``None`` if none)."""
        log = self.session.execute(
            select(models.TaskHistory.result["log"]).where(
                models.TaskHistory.id == task_id
            )
        ).scalar()
        return log if isinstance(log, str) else None

    async def count_history(self) -> int:
        return self.count(models.TaskHistory)

    async def get_task_arguments(self, task_id: int) -> str | None:
        """Return the raw JSON ``arguments`` of a history row (or None).

        Lets endpoints read task arguments without touching the ORM
        model or ``uow.session`` directly.
        """
        row = self.session.get(models.TaskHistory, task_id)
        return row.arguments if row is not None else None

    # -- scheduled tasks ----------------------------------------------------

    async def get_scheduled_tasks(self) -> Sequence[models.ScheduledTask]:
        return self.get_scheduled_tasks_sync()

    def get_scheduled_tasks_sync(self) -> Sequence[models.ScheduledTask]:
        stmt = select(models.ScheduledTask).order_by(models.ScheduledTask.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    async def create_scheduled(
        self, task_data: dict
    ) -> models.ScheduledTask:
        task = models.ScheduledTask(**task_data)
        self.session.add(task)
        self.session.flush()
        return task

    async def update_scheduled(
        self, task_id: int, task_data: dict
    ) -> models.ScheduledTask | None:
        return self._update_attrs(models.ScheduledTask, task_id, task_data)

    async def delete_scheduled(self, task_id: int) -> bool:
        return self._delete_by_id(models.ScheduledTask, task_id)

    async def reorder_scheduled(self, ids: list[int]) -> bool:
        return self._reorder(models.ScheduledTask, "sort_index", ids)
