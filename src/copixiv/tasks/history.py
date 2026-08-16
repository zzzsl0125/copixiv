"""Task-history recorder — the kernel's ownership of the ``task_history`` table.

Every task_history write goes through this class: enqueue (pending row),
status transitions (running/success/failed/interrupted), and
result/duration updates — all inside the global ``db_write()`` lock so
they serialize with every other write path.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from copixiv.log import logger


class TaskHistoryRecorder:
    """Records task lifecycle rows; owns the pending/running guard queries."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    # -- enqueue + duplicate-run guard -------------------------------------

    def has_pending_or_running(self, name: str) -> bool:
        """True when a history row for *name* is still pending/running."""
        from copixiv.infrastructure.repositories.task import (
            SQLAlchemyTaskRepository,
        )

        with self._session_factory() as session:
            return SQLAlchemyTaskRepository(session).has_pending_or_running(name)

    def enqueue(self, name: str, params: dict) -> int:
        """Insert the pending row; returns the new task id.

        Short INSERT outside ``db_write()`` — task enqueue happens in sync
        API paths; the 60s busy_timeout covers the rare collision.
        """
        from copixiv.infrastructure.repositories.task import (
            SQLAlchemyTaskRepository,
        )

        with self._session_factory() as session:
            repo = SQLAlchemyTaskRepository(session)
            task_id = repo.add_task_sync(name, params)
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
    ) -> None:
        """Update a TaskHistory row inside the global write lock."""
        from copixiv.infrastructure.database.write_lock import db_write
        from copixiv.infrastructure.repositories.task import (
            SQLAlchemyTaskRepository,
        )

        async with db_write():
            with self._session_factory() as session:
                repo = SQLAlchemyTaskRepository(session)
                repo.update_task_sync(
                    task_id, status, result=result, duration=duration,
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
        from copixiv.infrastructure.database import models

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
