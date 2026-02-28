# -*- coding: utf-8 -*-
import json
from datetime import datetime
from sqlalchemy import select, text
from .base_repository import BaseRepository
from .. import models
from .. import constants as C

class TaskHistory(BaseRepository):
    def add_task(self, name: str, args: dict) -> int:
        """Adds a new task to the history and returns its ID."""
        start_time = datetime.now().isoformat()
        
        task = models.TaskHistory(
            name=name,
            arguments=json.dumps(args),
            status="pending",
            start_time=start_time
        )
        self.session.add(task)
        self.session.flush() # Populate ID
        return task.id

    def update_task(self, task_id: int, status: str, result: str = None):
        """Updates the status and result of a task."""
        end_time = datetime.now()
        
        task = self.session.get(models.TaskHistory, task_id)
        if task:
            duration = None
            if task.start_time:
                try:
                    start_dt = datetime.fromisoformat(task.start_time)
                    duration = (end_time - start_dt).total_seconds()
                except ValueError:
                    pass
            
            task.status = status
            task.end_time = end_time.isoformat()
            task.duration = duration
            task.result = result
            
            self.session.add(task)

    def get_history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Retrieves a paginated list of task history."""
        stmt = select(models.TaskHistory).order_by(models.TaskHistory.start_time.desc()).limit(limit).offset(offset)
        result = self.session.execute(stmt).scalars().all()
        return [{c.name: getattr(n, c.name) for c in n.__table__.columns} for n in result]

class ScheduledTask(BaseRepository):
    # Depending on schemas which might not be available, we accept dict or object with attributes
    
    def get(self, task_id: int) -> models.ScheduledTask | None:
        return self.session.get(models.ScheduledTask, task_id)

    def get_all(self) -> list[models.ScheduledTask]:
        stmt = select(models.ScheduledTask).order_by(models.ScheduledTask.sort_index.asc(), models.ScheduledTask.id.asc())
        return list(self.session.execute(stmt).scalars().all())

    def create(self, task_data) -> models.ScheduledTask:
        # task_data can be Pydantic model or dict
        if isinstance(task_data, dict):
            name = task_data.get('name')
            task = task_data.get('task')
            cron = task_data.get('cron')
            params = task_data.get('params')
            config = task_data.get('config')
            is_enabled = task_data.get('is_enabled')
        else:
            name = task_data.name
            task = task_data.task
            cron = task_data.cron
            params = task_data.params
            config = task_data.config
            is_enabled = task_data.is_enabled

        db_task = models.ScheduledTask(
            name=name,
            task=task,
            cron=cron,
            params=json.dumps(params) if isinstance(params, (dict, list)) else params,
            config=json.dumps(config) if isinstance(config, (dict, list)) else config,
            is_enabled=is_enabled
        )
        self.session.add(db_task)
        self.session.flush()
        self.session.refresh(db_task)
        return db_task

    def update(self, task_id: int, task_data) -> models.ScheduledTask | None:
        db_task = self.get(task_id)
        if db_task:
            if isinstance(task_data, dict):
                update_data = task_data
            else:
                # Assuming Pydantic model
                update_data = task_data.model_dump(exclude_unset=True)

            for key, value in update_data.items():
                if key in ('params', 'config') and isinstance(value, (dict, list)):
                    setattr(db_task, key, json.dumps(value))
                elif hasattr(db_task, key):
                    setattr(db_task, key, value)
            
            self.session.flush()
            self.session.refresh(db_task)
        return db_task

    def delete(self, task_id: int) -> bool:
        db_task = self.get(task_id)
        if db_task:
            self.session.delete(db_task)
            self.session.flush()
            return True
        return False

    def update_order(self, task_ids: list[int]) -> bool:
        """Updates the sort_index for a list of task IDs."""
        try:
            for index, task_id in enumerate(task_ids):
                db_task = self.get(task_id)
                if db_task:
                    db_task.sort_index = index
            self.session.flush()
            return True
        except Exception as e:
            # Handle exception if needed, session rollback should be done by the caller (Database context manager)
            return False