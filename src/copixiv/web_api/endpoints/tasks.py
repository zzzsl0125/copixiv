"""Task API endpoints — identical contract to v1."""

import inspect

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from copixiv.web_api.schemas import (
    ScheduledTaskCreate,
    ScheduledTaskUpdate,
    ScheduledTaskResponse,
    TaskHistoryListResponse,
    TaskHistoryResponse,
    TaskMethod,
    TaskArgument,
)
from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.task import TaskRepository
from copixiv.tasks.registry import list_tasks
import copixiv.tasks.novel_tasks  # ensure task @register decorators fire at import time  # noqa: F401

router = APIRouter()


@router.get("/methods", response_model=list[TaskMethod])
def get_task_methods():
    methods = []
    for name, func in list_tasks().items():
        sig = inspect.signature(func)
        arguments = []
        for param_name, param in sig.parameters.items():
            # Skip injected dependencies (keyword-only) and catch-all **_ kwargs
            if param.kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                continue
            param_type = "str"
            if param.annotation != inspect.Parameter.empty:
                if param.annotation is int:
                    param_type = "int"
                elif param.annotation is bool:
                    param_type = "bool"
                elif param.annotation is float:
                    param_type = "float"

            default_val = None
            required = True
            if param.default != inspect.Parameter.empty:
                default_val = param.default
                required = False

            arguments.append(TaskArgument(
                name=param_name, type=param_type, default=default_val, required=required
            ))
        methods.append(TaskMethod(name=name, description=func.__doc__, arguments=arguments))
    return methods


@router.get("/scheduled", response_model=list[ScheduledTaskResponse])
async def get_scheduled_tasks(db: Session = Depends(get_db)):
    repo = TaskRepository(db)
    tasks = await repo.get_scheduled_tasks()
    return tasks


@router.post("/scheduled", response_model=ScheduledTaskResponse)
async def create_scheduled_task(
    task_in: ScheduledTaskCreate, db: Session = Depends(get_db), request: Request = None,
):
    repo = TaskRepository(db)
    task = await repo.create_scheduled(task_in.model_dump())
    db.commit()
    request.app.state.task_manager.reload_cron_jobs()
    return task


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    task_id: int, task_in: ScheduledTaskUpdate,
    db: Session = Depends(get_db), request: Request = None,
):
    repo = TaskRepository(db)
    task = await repo.update_scheduled(task_id, task_in.model_dump(exclude_none=True))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.commit()
    request.app.state.task_manager.reload_cron_jobs()
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(
    task_id: int, db: Session = Depends(get_db), request: Request = None,
):
    repo = TaskRepository(db)
    if not await repo.delete_scheduled(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.commit()
    request.app.state.task_manager.reload_cron_jobs()
    return {"ok": True}


@router.post("/scheduled/reorder")
async def reorder_scheduled_tasks(
    task_ids: list[int], db: Session = Depends(get_db), request: Request = None,
):
    repo = TaskRepository(db)
    if not await repo.reorder_scheduled(task_ids):
        raise HTTPException(status_code=500, detail="Failed to reorder tasks")
    db.commit()
    request.app.state.task_manager.reload_cron_jobs()
    return {"ok": True}


@router.post("/scheduled/{task_id}/run")
async def run_scheduled_task(task_id: int, request: Request):
    try:
        request.app.state.task_manager.run_task_now(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.get("/history", response_model=TaskHistoryListResponse)
async def get_task_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    repo = TaskRepository(db)
    history = await repo.get_history(limit=limit, offset=offset)
    total = await repo.count_history()
    return {"items": history, "total": total}
