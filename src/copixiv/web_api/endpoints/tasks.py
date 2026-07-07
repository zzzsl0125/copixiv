"""Task API endpoints — identical contract to v1."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from copixiv.web_api.schemas import (
    ScheduledTaskCreate, ScheduledTaskUpdate, ScheduledTaskResponse,
    TaskHistoryListResponse, TaskMethod, TaskArgument,
)
from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.task import TaskRepository
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
async def get_scheduled_tasks(db: Session = Depends(get_db)):
    use_case = ListScheduledUseCase(TaskRepository(db))
    return await use_case.execute()


@router.post("/scheduled", response_model=ScheduledTaskResponse)
async def create_scheduled_task(
    task_in: ScheduledTaskCreate, db: Session = Depends(get_db),
    request: Request = None,
):
    use_case = CreateScheduledUseCase(
        TaskRepository(db), task_manager=request.app.state.task_manager,
    )
    task = await use_case.execute(task_in.model_dump())
    db.commit()
    return task


@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    task_id: int, task_in: ScheduledTaskUpdate,
    db: Session = Depends(get_db), request: Request = None,
):
    use_case = UpdateScheduledUseCase(
        TaskRepository(db), task_manager=request.app.state.task_manager,
    )
    task = await use_case.execute(task_id, task_in.model_dump(exclude_none=True))
    db.commit()
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(
    task_id: int, db: Session = Depends(get_db), request: Request = None,
):
    use_case = DeleteScheduledUseCase(
        TaskRepository(db), task_manager=request.app.state.task_manager,
    )
    await use_case.execute(task_id)
    db.commit()
    return {"ok": True}


@router.post("/scheduled/reorder")
async def reorder_scheduled_tasks(
    task_ids: list[int], db: Session = Depends(get_db), request: Request = None,
):
    use_case = ReorderScheduledUseCase(
        TaskRepository(db), task_manager=request.app.state.task_manager,
    )
    await use_case.execute(task_ids)
    db.commit()
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
    db: Session = Depends(get_db),
):
    use_case = GetHistoryUseCase(TaskRepository(db))
    return await use_case.execute(limit=limit, offset=offset)
