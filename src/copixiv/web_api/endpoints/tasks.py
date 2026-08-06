"""Task API endpoints — identical contract to v1."""

from fastapi import APIRouter, Depends, Query, Request

from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import (
    ScheduledTaskCreate, ScheduledTaskUpdate, ScheduledTaskResponse,
    TaskHistoryListResponse, TaskMethod, TaskArgument,
)
from copixiv.application.task import (
    GetMethodsUseCase, ListScheduledUseCase, CreateScheduledUseCase,
    UpdateScheduledUseCase, DeleteScheduledUseCase, ReorderScheduledUseCase,
    RunScheduledUseCase, GetHistoryUseCase,
)
import copixiv.tasks.novel_tasks  # noqa: F401 — ensure @register decorators fire

router = APIRouter()


@router.get("/methods", response_model=list[TaskMethod])
def get_task_methods():
    use_case = GetMethodsUseCase()
    raw_methods = use_case.execute()
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
    use_case = ListScheduledUseCase(uow.tasks)
    return await use_case.execute()


@router.post("/scheduled", response_model=ScheduledTaskResponse)
async def create_scheduled_task(
    task_in: ScheduledTaskCreate, uow: SqlUnitOfWork = Depends(get_uow),
    request: Request = None,
):
    use_case = CreateScheduledUseCase(
        uow.tasks, task_manager=request.app.state.task_manager,
    )
    task = await use_case.execute(task_in.model_dump())
    return task


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    task_id: int, task_in: ScheduledTaskUpdate,
    uow: SqlUnitOfWork = Depends(get_uow), request: Request = None,
):
    use_case = UpdateScheduledUseCase(
        uow.tasks, task_manager=request.app.state.task_manager,
    )
    task = await use_case.execute(task_id, task_in.model_dump(exclude_none=True))
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(
    task_id: int, uow: SqlUnitOfWork = Depends(get_uow), request: Request = None,
):
    use_case = DeleteScheduledUseCase(
        uow.tasks, task_manager=request.app.state.task_manager,
    )
    await use_case.execute(task_id)
    return {"ok": True}


@router.post("/scheduled/reorder")
async def reorder_scheduled_tasks(
    task_ids: list[int], uow: SqlUnitOfWork = Depends(get_uow), request: Request = None,
):
    use_case = ReorderScheduledUseCase(
        uow.tasks, task_manager=request.app.state.task_manager,
    )
    await use_case.execute(task_ids)
    return {"ok": True}


@router.post("/scheduled/{task_id}/run")
async def run_scheduled_task(task_id: int, request: Request):
    use_case = RunScheduledUseCase(request.app.state.task_manager)
    use_case.execute(task_id)
    return {"ok": True}


@router.get("/history", response_model=TaskHistoryListResponse)
async def get_task_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    use_case = GetHistoryUseCase(uow.tasks)
    return await use_case.execute(limit=limit, offset=offset)
