"""Task repository — history and scheduled tasks."""

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from .base import BaseRepository


class TaskRepository(BaseRepository):
    """Repository for task history and scheduled tasks."""

    def __init__(self, session: Session):
        super().__init__(session)

    # -- task history ------------------------------------------------------

    async def add_task(self, name: str, arguments: dict) -> int:
        return self.add_task_sync(name, arguments)

    def add_task_sync(self, name: str, arguments: dict) -> int:
        task = models.TaskHistory(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
            status="pending",
            start_time=datetime.now().isoformat(),
        )
        self.session.add(task)
        self.session.flush()
        return task.id

    async def update_task(
        self, task_id: int, status: str, result: str | None = None
    ) -> None:
        self.update_task_sync(task_id, status, result=result)

    def update_task_sync(
        self, task_id: int, status: str, result: str | None = None,
        duration: float | None = None,
    ) -> None:
        task = self.session.get(models.TaskHistory, task_id)
        if task is not None:
            task.status = status
            task.end_time = datetime.now().isoformat()
            if result is not None:
                task.result = result
            if duration is not None:
                task.duration = duration

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

    async def count_history(self) -> int:
        return self.count(models.TaskHistory)

    # -- scheduled tasks ----------------------------------------------------

    async def get_scheduled_tasks(self) -> Sequence[models.ScheduledTask]:
        return self.get_scheduled_tasks_sync()

    def get_scheduled_tasks_sync(self) -> Sequence[models.ScheduledTask]:
        stmt = select(models.ScheduledTask).order_by(models.ScheduledTask.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    async def create_scheduled(
        self, task_data: dict
    ) -> models.ScheduledTask:
        if "config" in task_data and isinstance(task_data["config"], dict):
            task_data["config"] = json.dumps(task_data["config"], ensure_ascii=False)
        task = models.ScheduledTask(**task_data)
        self.session.add(task)
        self.session.flush()
        return task

    async def update_scheduled(
        self, task_id: int, task_data: dict
    ) -> models.ScheduledTask | None:
        if "config" in task_data and isinstance(task_data["config"], dict):
            task_data["config"] = json.dumps(task_data["config"], ensure_ascii=False)
        task = self.session.get(models.ScheduledTask, task_id)
        if task is None:
            return None
        for k, v in task_data.items():
            if v is not None and hasattr(task, k):
                setattr(task, k, v)
        return task

    async def delete_scheduled(self, task_id: int) -> bool:
        task = self.session.get(models.ScheduledTask, task_id)
        if task is None:
            return False
        self.session.delete(task)
        return True

    async def reorder_scheduled(self, ids: list[int]) -> bool:
        for idx, task_id in enumerate(ids):
            task = self.session.get(models.ScheduledTask, task_id)
            if task is not None:
                task.sort_index = idx
        return True
