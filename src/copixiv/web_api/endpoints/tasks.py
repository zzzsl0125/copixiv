"""Task API endpoints — identical contract to v1."""

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from copixiv.domain.exceptions import NotFoundError, ValidationError
from copixiv.web_api.deps import get_uow, get_write_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import (
    ScheduledTaskCreate, ScheduledTaskUpdate, ScheduledTaskResponse,
    TaskHistoryListResponse, TaskMethod, TaskArgument,
)
from copixiv.tasks.registry import describe_tasks, get_task
import copixiv.tasks.novel_tasks  # noqa: F401 — ensure @register decorators fire

router = APIRouter()


def _validate_task_function(task_name: str) -> None:
    """Reject schedules that reference an unregistered task function."""
    if get_task(task_name) is None:
        raise ValidationError(f"Unknown task function: {task_name}")


@router.get("/methods", response_model=list[TaskMethod])
def get_task_methods():
    raw_methods = describe_tasks()
    methods = []
    for m in raw_methods:
        arguments = [
            TaskArgument(name=a["name"], type=a["type"],
                         default=a["default"], required=a["required"])
            for a in m["arguments"]
        ]
        methods.append(TaskMethod(
            name=m["name"], description=m["description"], arguments=arguments,
        ))
    return methods


@router.get("/scheduled", response_model=list[ScheduledTaskResponse])
async def get_scheduled_tasks(uow: SqlUnitOfWork = Depends(get_uow)):
    return await uow.tasks.get_scheduled_tasks()


@router.post("/scheduled", response_model=ScheduledTaskResponse)
async def create_scheduled_task(
    request: Request,
    background_tasks: BackgroundTasks,
    task_in: ScheduledTaskCreate,
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    _validate_task_function(task_in.task)
    task = await uow.tasks.create_scheduled(task_in.model_dump())
    # Commit FIRST, then reload: a fresh connection cannot see the
    # uncommitted INSERT.  (BackgroundTasks alone is not enough — this
    # FastAPI/Starlette version runs them BEFORE the yield-dependency
    # teardown commit, so the reload would still read old data.)
    await uow.commit()
    background_tasks.add_task(request.app.state.task_manager.reload_cron_jobs)
    return task


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    request: Request,
    background_tasks: BackgroundTasks,
    task_id: int, task_in: ScheduledTaskUpdate,
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if task_in.task is not None:
        _validate_task_function(task_in.task)
    task = await uow.tasks.update_scheduled(task_id, task_in.model_dump(exclude_none=True))
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    await uow.commit()
    background_tasks.add_task(request.app.state.task_manager.reload_cron_jobs)
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(
    request: Request,
    background_tasks: BackgroundTasks,
    task_id: int, uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if not await uow.tasks.delete_scheduled(task_id):
        raise NotFoundError(f"Task {task_id} not found")
    await uow.commit()
    background_tasks.add_task(request.app.state.task_manager.reload_cron_jobs)
    return {"ok": True}


@router.post("/scheduled/reorder")
async def reorder_scheduled_tasks(
    request: Request,
    background_tasks: BackgroundTasks,
    task_ids: list[int], uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if not await uow.tasks.reorder_scheduled(task_ids):
        raise NotFoundError("Failed to reorder tasks")
    await uow.commit()
    background_tasks.add_task(request.app.state.task_manager.reload_cron_jobs)
    return {"ok": True}


@router.post("/scheduled/{task_id}/run")
async def run_scheduled_task(task_id: int, request: Request):
    # DomainError 子类（NotFound/Validation/TaskAlreadyRunning → 404/400/409）
    # 由全局 DomainError handler 映射，无需在此捕获。
    request.app.state.task_manager.run_task_now(task_id)
    return {"ok": True}


@router.get("/history", response_model=TaskHistoryListResponse)
async def get_task_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    history = await uow.tasks.get_history(limit=limit, offset=offset)
    total = await uow.tasks.count_history()
    return {"items": history, "total": total}
