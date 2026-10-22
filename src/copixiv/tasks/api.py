"""Task API endpoints."""

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from pydantic import ValidationError as PydanticValidationError

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.deps import get_task_manager, get_uow, get_write_uow
from copixiv.db.uow import SqlUnitOfWork
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
from copixiv.tasks.schemas import (
    ScheduledTaskCreate, ScheduledTaskUpdate, ScheduledTaskResponse,
    TaskBusyResponse, TaskHistoryListResponse, TaskHistoryLogResponse,
    TaskHistoryResponse, TaskMethod,
)
from copixiv.tasks.kernel import describe_tasks, discover_tasks, get_spec, get_task

router = APIRouter()


def _validate_task_function(task_name: str) -> None:
    """Reject schedules that reference an unregistered task function."""
    discover_tasks()  # idempotent; also covers direct endpoint usage in tests
    if get_task(task_name) is None:
        raise ValidationError(f"Unknown task function: {task_name}")


def _validate_task_params(task_name: str, params: dict | None) -> None:
    """Validate *params* against the task's Pydantic args model.

    Without this the schedule row stores whatever it is given and the first
    failure surfaces only when the cron fires — as a ``failed`` history row
    whose error is a raw Pydantic message.  The task editor coerces form
    values, so this is about direct API calls, scripts, migrations and legacy
    rows (production has one: ``novel_fetch`` with ``{"id": null}``).

    Raises:
        ValidationError: the args do not satisfy the task's model.
    """
    spec = get_spec(task_name)
    if spec is None or spec.args_model is None:
        return
    try:
        spec.args_model.model_validate(params or {})
    except PydanticValidationError as exc:
        # Re-wrap so the message names the task instead of leaking a raw
        # Pydantic error dump into the UI.
        raise ValidationError(
            f"任务 {task_name} 的参数无效：{exc.errors()[0]['msg'] if exc.errors() else exc}"
        ) from exc


@router.get("/methods", response_model=list[TaskMethod])
def get_task_methods():
    return [
        TaskMethod(
            name=m["name"],
            description=m["description"],
            arguments=m["arguments"],
        )
        for m in describe_tasks()
    ]


@router.get("/scheduled", response_model=list[ScheduledTaskResponse])
async def get_scheduled_tasks(uow: SqlUnitOfWork = Depends(get_uow)):
    return await SQLAlchemyTaskRepository(uow.session).get_scheduled_tasks()


@router.post("/scheduled", response_model=ScheduledTaskResponse)
async def create_scheduled_task(
    background_tasks: BackgroundTasks,
    task_in: ScheduledTaskCreate,
    task_manager=Depends(get_task_manager),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    _validate_task_function(task_in.task)
    _validate_task_params(task_in.task, task_in.params)
    task = await SQLAlchemyTaskRepository(uow.session).create_scheduled(task_in.model_dump())
    # Commit FIRST, then reload: a fresh connection cannot see the
    # uncommitted INSERT.  (BackgroundTasks alone is not enough — this
    # FastAPI/Starlette version runs them BEFORE the yield-dependency
    # teardown commit, so the reload would still read old data.)
    await uow.commit()
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return task


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    background_tasks: BackgroundTasks,
    task_id: int, task_in: ScheduledTaskUpdate,
    task_manager=Depends(get_task_manager),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if task_in.task is not None:
        _validate_task_function(task_in.task)
        _validate_task_params(task_in.task, task_in.params)
    elif task_in.params is not None:
        # Params can change without the function changing: validate against
        # the *existing* row's function.
        existing = await SQLAlchemyTaskRepository(uow.session).get_scheduled_tasks()
        current = next((t for t in existing if t.id == task_id), None)
        if current is not None:
            _validate_task_params(current.task, task_in.params)
    task = await SQLAlchemyTaskRepository(uow.session).update_scheduled(task_id, task_in.model_dump(exclude_none=True))
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    await uow.commit()
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(
    background_tasks: BackgroundTasks,
    task_id: int,
    task_manager=Depends(get_task_manager),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if not await SQLAlchemyTaskRepository(uow.session).delete_scheduled(task_id):
        raise NotFoundError(f"Task {task_id} not found")
    await uow.commit()
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return {"ok": True}


@router.post("/scheduled/reorder")
async def reorder_scheduled_tasks(
    background_tasks: BackgroundTasks,
    task_ids: list[int],
    task_manager=Depends(get_task_manager),
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    if not await SQLAlchemyTaskRepository(uow.session).reorder_scheduled(task_ids):
        raise NotFoundError("Failed to reorder tasks")
    await uow.commit()
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return {"ok": True}


@router.post("/scheduled/{task_id}/run")
async def run_scheduled_task(
    task_id: int,
    task_manager=Depends(get_task_manager),
):
    # DomainError 子类（NotFound/Validation → 404/400）由全局
    # DomainError handler 映射，无需在此捕获；重复触发照常入队。
    task_manager.run_task_now(task_id)
    return {"ok": True}


@router.get("/busy", response_model=TaskBusyResponse)
async def get_busy_tasks(uow: SqlUnitOfWork = Depends(get_uow)):
    """Task functions with a pending/running row — the front-end busy set.

    Tiny on purpose: the task-status poller hits this every 5 s while a task
    runs, and it used to read a full history page instead.
    """
    return {"funcs": await SQLAlchemyTaskRepository(uow.session).get_busy_funcs()}


@router.get("/history", response_model=TaskHistoryListResponse)
async def get_task_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """List task-history rows **without** their log payload.

    The log is fetched on demand via ``GET /history/{id}/log``.  Returning it
    inline made one page ~7.4 MB (96 % log text) while the front-end poller
    re-read it every 5 s whenever a task was running.
    """
    repo = SQLAlchemyTaskRepository(uow.session)
    rows = await repo.get_history_rows(limit=limit, offset=offset)
    total = await repo.count_history()
    return {
        "items": [TaskHistoryResponse.model_validate(r) for r in rows],
        "total": total,
    }


@router.get("/history/{task_id}/log", response_model=TaskHistoryLogResponse)
async def get_task_log(
    task_id: int,
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """Return the stored execution log for one task-history row."""
    log = await SQLAlchemyTaskRepository(uow.session).get_task_log(task_id)
    if log is None:
        raise NotFoundError(f"Task {task_id} 没有日志")
    return {"id": task_id, "log": log}
