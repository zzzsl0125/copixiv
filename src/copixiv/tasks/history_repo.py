"""Task repository — history and scheduled tasks."""

import json
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, or_, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository


class SQLAlchemyTaskRepository(BaseRepository):
    """Repository for task history and scheduled tasks."""

    def __init__(self, session: Session):
        super().__init__(session)

    # -- task history ------------------------------------------------------

    def add_task(self, name: str, arguments: dict, task_func: str) -> int:
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
            start_time=datetime.now(UTC),
        )
        self.session.add(task)
        self.session.flush()
        return task.id

    def update_task(
        self,
        task_id: int,
        status: str | None,
        result: dict[str, Any] | str | None = None,
        duration: float | None = None,
        progress: Any | None = None,
    ) -> None:
        """Update one history row; ``status=None`` means "progress only"."""
        task = self.session.get(models.TaskHistory, task_id)
        if task is not None:
            if status is not None:
                task.status = status
            if status in ("success", "failed", "interrupted", "cancelled"):
                task.end_time = datetime.now(UTC)
            if result is not None:
                if isinstance(result, str):
                    with suppress(json.JSONDecodeError, TypeError):
                        result = json.loads(result)
                task.result = result
            if progress is not None:
                task.progress = progress
            if duration is not None:
                task.duration = duration

    def cancel_pending(self, task_id: int) -> bool:
        """Atomically transition a pending task row to ``cancelled``."""
        now = datetime.now(UTC)
        cancelled_id = self.session.execute(
            update(models.TaskHistory)
            .where(
                models.TaskHistory.id == task_id,
                models.TaskHistory.status == "pending",
            )
            .values(
                status="cancelled",
                end_time=now,
                result={"summary": "任务已手动取消"},
            )
            .returning(models.TaskHistory.id)
        ).scalar_one_or_none()
        return cancelled_id is not None

    def get_task_status(
        self,
        task_id: int,
    ) -> tuple[str, str | None, Any] | None:
        """Return ``(status, task_func, arguments)`` for *task_id*, or ``None``."""
        row = self.session.get(models.TaskHistory, task_id)
        if row is None:
            return None
        return row.status, row.task_func, row.arguments

    def get_next_pending(
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
                or_(
                    models.TaskHistory.task_func.is_(None),
                    models.TaskHistory.task_func.not_in(exclude_funcs),
                )
            )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_busy_funcs(self) -> list[str]:
        """Distinct ``task_func`` values with a pending/running row.

        One tiny query backing the front-end busy flags.  It exists so the
        poller no longer derives that set from a full history page (which it
        did with ``limit=100`` — a 7.4 MB response while anything was running,
        and blind to anything beyond the first 100 rows).
        """
        rows = (
            self.session.execute(
                select(models.TaskHistory.task_func)
                .where(
                    models.TaskHistory.status.in_(("pending", "running")),
                    models.TaskHistory.task_func.isnot(None),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        return sorted(r for r in rows if r is not None)

    def get_history(self, limit: int = 50, offset: int = 0) -> Sequence[models.TaskHistory]:
        stmt = select(models.TaskHistory).order_by(models.TaskHistory.id.desc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def get_history_rows(
        self,
        limit: int = 50,
        offset: int = 0,
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

    def get_task_log(self, task_id: int) -> str | None:
        """Return just the stored log text for one row (``None`` if none)."""
        log = self.session.execute(
            select(models.TaskHistory.result["log"]).where(models.TaskHistory.id == task_id)
        ).scalar()
        return log if isinstance(log, str) else None

    def count_history(self) -> int:
        return self.count(models.TaskHistory)

    def get_task_arguments(self, task_id: int) -> str | None:
        """Return the raw JSON ``arguments`` of a history row (or None).

        Lets endpoints read task arguments without touching the ORM
        model or ``uow.session`` directly.
        """
        row = self.session.get(models.TaskHistory, task_id)
        return row.arguments if row is not None else None

    # -- scheduled tasks ----------------------------------------------------

    def get_scheduled_tasks(self) -> Sequence[models.ScheduledTask]:
        stmt = select(models.ScheduledTask).order_by(models.ScheduledTask.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    def get_scheduled_task(self, task_id: int) -> models.ScheduledTask | None:
        return self.session.get(models.ScheduledTask, task_id)

    def create_scheduled(self, task_data: dict) -> models.ScheduledTask:
        task = models.ScheduledTask(**task_data)
        self.session.add(task)
        self.session.flush()
        return task

    def update_scheduled(self, task_id: int, task_data: dict) -> models.ScheduledTask | None:
        return self._update_attrs(
            models.ScheduledTask,
            task_id,
            task_data,
            allow_none_fields=("params",),
        )

    def delete_scheduled(self, task_id: int) -> bool:
        return self._delete_by_id(models.ScheduledTask, task_id)

    def reorder_scheduled(self, ids: list[int]) -> int:
        return self._reorder(models.ScheduledTask, "sort_index", ids)
