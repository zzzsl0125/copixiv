"""Task API endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import ValidationError as PydanticValidationError

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import TaskManagerDep, UowDep
from copixiv.tasks.history_repo import SQLAlchemyTaskRepository
from copixiv.tasks.registry import describe_tasks, discover_tasks, get_spec, get_task
from copixiv.tasks.schemas import (
    ScheduledTaskCreate,
    ScheduledTaskResponse,
    ScheduledTaskUpdate,
    TaskBusyResponse,
    TaskCancelResponse,
    TaskHistoryListResponse,
    TaskHistoryLogResponse,
    TaskHistoryResponse,
    TaskMethod,
)

router = APIRouter()


def _validate_task_function(task_name: str) -> None:
    """Reject schedules that reference an unregistered task function."""
    discover_tasks()  # idempotent; also covers direct endpoint usage in tests
    if get_task(task_name) is None:
        raise ValidationError(f"Unknown task function: {task_name}")


def _validate_task_params(task_name: str, params: dict | None) -> None:
    """Validate *params* against the task's Pydantic args model."""
    spec = get_spec(task_name)
    if spec is None or spec.args_model is None:
        return
    try:
        spec.args_model.model_validate(params or {})
    except PydanticValidationError as exc:
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
def get_scheduled_tasks(uow: UowDep):
    with uow.begin():
        return [
            ScheduledTaskResponse.model_validate(t)
            for t in SQLAlchemyTaskRepository(uow.session).get_scheduled_tasks()
        ]


@router.post("/scheduled", response_model=ScheduledTaskResponse)
def create_scheduled_task(
    background_tasks: BackgroundTasks,
    task_in: ScheduledTaskCreate,
    task_manager: TaskManagerDep,
    uow: UowDep,
):
    _validate_task_function(task_in.task)
    _validate_task_params(task_in.task, task_in.params)

    def _write(tx: SqlUnitOfWork):
        task = SQLAlchemyTaskRepository(tx.session).create_scheduled(task_in.model_dump())
        return ScheduledTaskResponse.model_validate(task)

    result = run_write_transaction_sync(uow, _write)
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return result


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
def update_scheduled_task(
    background_tasks: BackgroundTasks,
    task_id: int,
    task_in: ScheduledTaskUpdate,
    task_manager: TaskManagerDep,
    uow: UowDep,
):
    if task_in.task is not None:
        _validate_task_function(task_in.task)

    def _write(tx: SqlUnitOfWork):
        repo = SQLAlchemyTaskRepository(tx.session)
        current = repo.get_scheduled_task(task_id)
        if current is None:
            raise NotFoundError(f"Task {task_id} not found")

        raw_updates = task_in.model_dump(exclude_unset=True)
        updates = {k: v for k, v in raw_updates.items() if k == "params" or v is not None}
        if task_in.task is not None and task_in.task != current.task:
            if "params" not in updates or updates["params"] is None:
                updates["params"] = None
            _validate_task_params(task_in.task, updates.get("params"))
        elif "params" in updates:
            _validate_task_params(current.task, updates["params"])

        task = repo.update_scheduled(task_id, updates)
        return ScheduledTaskResponse.model_validate(task)

    result = run_write_transaction_sync(uow, _write)
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return result


@router.delete("/scheduled/{task_id}", status_code=204)
def delete_scheduled_task(
    background_tasks: BackgroundTasks,
    task_id: int,
    task_manager: TaskManagerDep,
    uow: UowDep,
):
    def _write(tx: SqlUnitOfWork):
        if not SQLAlchemyTaskRepository(tx.session).delete_scheduled(task_id):
            raise NotFoundError(f"Task {task_id} not found")

    run_write_transaction_sync(uow, _write)
    background_tasks.add_task(task_manager.reload_cron_jobs)


@router.post("/scheduled/reorder")
def reorder_scheduled_tasks(
    background_tasks: BackgroundTasks,
    task_ids: list[int],
    task_manager: TaskManagerDep,
    uow: UowDep,
):
    def _write(tx: SqlUnitOfWork):
        if not SQLAlchemyTaskRepository(tx.session).reorder_scheduled(task_ids):
            raise NotFoundError("Failed to reorder tasks")

    run_write_transaction_sync(uow, _write)
    background_tasks.add_task(task_manager.reload_cron_jobs)
    return {"ok": True}


@router.post("/scheduled/{task_id}/run")
def run_scheduled_task(
    task_id: int,
    task_manager: TaskManagerDep,
):
    task_manager.run_task_now(task_id)
    return {"ok": True}


@router.get("/busy", response_model=TaskBusyResponse)
def get_busy_tasks(uow: UowDep):
    """Task functions with a pending/running row — the front-end busy set."""
    with uow.begin():
        return {"funcs": SQLAlchemyTaskRepository(uow.session).get_busy_funcs()}


@router.get("/history", response_model=TaskHistoryListResponse)
def get_task_history(
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List task-history rows **without** their log payload."""
    with uow.begin():
        repo = SQLAlchemyTaskRepository(uow.session)
        rows = repo.get_history_rows(limit=limit, offset=offset)
        total = repo.count_history()
        return {
            "items": [TaskHistoryResponse.model_validate(r) for r in rows],
            "total": total,
        }


@router.get("/history/{task_id}/log", response_model=TaskHistoryLogResponse)
def get_task_log(
    task_id: int,
    uow: UowDep,
):
    """Return the stored execution log for one task-history row."""
    with uow.begin():
        log = SQLAlchemyTaskRepository(uow.session).get_task_log(task_id)
        if log is None:
            raise NotFoundError(f"Task {task_id} 没有日志")
        return {"id": task_id, "log": log}


@router.post("/history/{task_id}/cancel", response_model=TaskCancelResponse)
def cancel_task_history(
    task_id: int,
    task_manager: TaskManagerDep,
):
    """Request cancellation of a pending or running background task."""
    status = task_manager.cancel_task(task_id)
    return TaskCancelResponse(task_id=task_id, status=status)
