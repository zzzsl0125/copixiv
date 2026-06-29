"""Task API endpoints — identical contract to v1."""

import inspect

from fastapi import APIRouter, Depends, HTTPException, Query
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
from copixiv.tasks.registry import get_task, list_tasks

router = APIRouter()


@router.get("/methods", response_model=list[TaskMethod])
def get_task_methods():
    methods = []
    import copixiv.tasks.novel_tasks  # ensure registration
    for name, func in list_tasks().items():
        sig = inspect.signature(func)
        arguments = []
        for param_name, param in sig.parameters.items():
            if param_name.startswith("_"):
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
async def create_scheduled_task(task_in: ScheduledTaskCreate, db: Session = Depends(get_db)):
    repo = TaskRepository(db)
    task = await repo.create_scheduled(task_in.model_dump())
    return task


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(task_id: int, task_in: ScheduledTaskUpdate, db: Session = Depends(get_db)):
    repo = TaskRepository(db)
    task = await repo.update_scheduled(task_id, task_in.model_dump(exclude_none=True))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(task_id: int, db: Session = Depends(get_db)):
    repo = TaskRepository(db)
    if not await repo.delete_scheduled(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@router.post("/scheduled/reorder")
async def reorder_scheduled_tasks(task_ids: list[int], db: Session = Depends(get_db)):
    repo = TaskRepository(db)
    if not await repo.reorder_scheduled(task_ids):
        raise HTTPException(status_code=500, detail="Failed to reorder tasks")
    return {"ok": True}


@router.post("/scheduled/{task_id}/run")
async def run_scheduled_task(task_id: int, db: Session = Depends(get_db)):
    repo = TaskRepository(db)
    tasks = await repo.get_scheduled_tasks()
    task = next((t for t in tasks if t.id == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    func = get_task(task.task)
    if not func:
        raise HTTPException(status_code=400, detail=f"Invalid task function: {task.task}")

    # Queue the task via the app's task manager
    # This will be wired through app.state.task_manager
    raise HTTPException(status_code=501, detail="Task execution not yet wired")


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
