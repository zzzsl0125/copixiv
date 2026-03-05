from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from web_api.schemas import (
    ScheduledTaskCreate,
    ScheduledTaskUpdate,
    ScheduledTaskResponse,
    TaskHistoryListResponse,
    TaskHistoryResponse
)
from web_api.deps import get_db
from core.db.database import Database
from core.db.repositories.task import ScheduledTask, TaskHistory
import json
import inspect
from core import tasks as core_tasks
from core.task_manager import TaskExecutor
from web_api.schemas import (
    ScheduledTaskCreate,
    ScheduledTaskUpdate,
    ScheduledTaskResponse,
    TaskHistoryListResponse,
    TaskHistoryResponse,
    TaskMethod,
    TaskArgument
)

router = APIRouter()

@router.get("/methods", response_model=List[TaskMethod])
def get_task_methods():
    methods = []
    for name, func in inspect.getmembers(core_tasks, inspect.isfunction):
        if name.startswith("_"):
            continue
        
        sig = inspect.signature(func)
        arguments = []
        for param_name, param in sig.parameters.items():
            param_type = "str"
            if param.annotation != inspect.Parameter.empty:
                if param.annotation == int:
                    param_type = "int"
                elif param.annotation == bool:
                    param_type = "bool"
                elif param.annotation == float:
                    param_type = "float"
            
            default_val = None
            required = True
            if param.default != inspect.Parameter.empty:
                default_val = param.default
                required = False
            
            arguments.append(TaskArgument(
                name=param_name,
                type=param_type,
                default=default_val,
                required=required
            ))
            
        methods.append(TaskMethod(
            name=name,
            description=func.__doc__,
            arguments=arguments
        ))
    return methods

@router.get("/scheduled", response_model=List[ScheduledTaskResponse])
def get_scheduled_tasks(db: Session = Depends(get_db)):
    repo = ScheduledTask(db)
    tasks = repo.get_all()
    # Parse JSON fields for response
    for task in tasks:
        if isinstance(task.params, str):
            try: task.params = json.loads(task.params)
            except: task.params = {}
        if isinstance(task.config, str):
            try: task.config = json.loads(task.config)
            except: task.config = {}
    return tasks

@router.post("/scheduled", response_model=ScheduledTaskResponse)
def create_scheduled_task(task_in: ScheduledTaskCreate, db: Session = Depends(get_db)):
    repo = ScheduledTask(db)
    task = repo.create(task_in)
    db.commit()
    db.refresh(task)
    if isinstance(task.params, str):
        try: task.params = json.loads(task.params)
        except: task.params = {}
    if isinstance(task.config, str):
        try: task.config = json.loads(task.config)
        except: task.config = {}
    return task

@router.put("/scheduled/{task_id}", response_model=ScheduledTaskResponse)
def update_scheduled_task(task_id: int, task_in: ScheduledTaskUpdate, db: Session = Depends(get_db)):
    repo = ScheduledTask(db)
    task = repo.update(task_id, task_in)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.commit()
    db.refresh(task)
    if isinstance(task.params, str):
        try: task.params = json.loads(task.params)
        except: task.params = {}
    if isinstance(task.config, str):
        try: task.config = json.loads(task.config)
        except: task.config = {}
    return task

@router.delete("/scheduled/{task_id}")
def delete_scheduled_task(task_id: int, db: Session = Depends(get_db)):
    repo = ScheduledTask(db)
    if not repo.delete(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    db.commit()
    return {"ok": True}

@router.post("/scheduled/reorder")
def reorder_scheduled_tasks(task_ids: List[int], db: Session = Depends(get_db)):
    repo = ScheduledTask(db)
    if not repo.update_order(task_ids):
        raise HTTPException(status_code=500, detail="Failed to reorder tasks")
    db.commit()
    return {"ok": True}

@router.post("/scheduled/{task_id}/run")
def run_scheduled_task(task_id: int, db: Session = Depends(get_db)):
    repo = ScheduledTask(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # 1. Get the function
    func_name = task.task
    if not hasattr(core_tasks, func_name) or func_name.startswith("_"):
         raise HTTPException(status_code=400, detail=f"Invalid task function: {func_name}")
    
    func = getattr(core_tasks, func_name)
    
    # 2. Parse params
    params = {}
    if task.params:
        if isinstance(task.params, str):
            try:
                params = json.loads(task.params)
            except:
                params = {}
        elif isinstance(task.params, dict):
            params = task.params

    # 3. Parse config
    config = {}
    if task.config:
        if isinstance(task.config, str):
            try:
                config = json.loads(task.config)
            except:
                config = {}
        elif isinstance(task.config, dict):
            config = task.config
            
    # 4. Add to queue
    TaskExecutor().add_task(task.name, func, config, **params)
    
    return {"ok": True, "message": "Task queued"}

@router.get("/history", response_model=TaskHistoryListResponse)
def get_task_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    repo = TaskHistory(db)
    history = repo.get_history(limit=limit, offset=offset)
    # We need total count if possible, but get_history doesn't return total
    # For simplicity, just return the list and a placeholder total
    # Or let's modify the response to just include items if we don't have total easily
    
    # history items are dicts from get_history
    items = []
    for h in history:
        items.append({
            "id": h["id"],
            "name": h["name"],
            "arguments": h["arguments"],
            "status": h["status"],
            "start_time": h["start_time"],
            "end_time": h["end_time"],
            "duration": h["duration"],
            "result": h["result"]
        })
        
    return {"items": items, "total": 0} # We could implement total in the repo later if needed