from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Any, Dict

class NovelBase(BaseModel):
    id: int
    title: str
    author_name: Optional[str] = None
    like: int = 0
    view: int = 0
    text: int = 0
    caption: Optional[str] = None
    create_time: Optional[str] = None
    has_epub: int = 0
    tags: List[str] = []
    is_favourite: int = 0  # 0 or 1 from query
    
    model_config = ConfigDict(from_attributes=True)

class NovelListResponse(BaseModel):
    novels: List[NovelBase]
    cursor: Optional[NovelBase] = None 

# Task Management Schemas

class ScheduledTaskCreate(BaseModel):
    name: str
    task: str
    cron: str
    params: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    is_enabled: bool = False
    sort_index: int = 0

class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = None
    task: Optional[str] = None
    cron: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    is_enabled: Optional[bool] = None
    sort_index: Optional[int] = None

class ScheduledTaskResponse(ScheduledTaskCreate):
    id: int
    
    model_config = ConfigDict(from_attributes=True)

class TaskHistoryResponse(BaseModel):
    id: int
    name: str
    arguments: Optional[str] = None
    status: str
    start_time: str
    end_time: Optional[str] = None
    duration: Optional[float] = None
    result: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)

class TaskHistoryListResponse(BaseModel):
    items: List[TaskHistoryResponse]
    total: int

class TaskArgument(BaseModel):
    name: str
    type: str
    default: Optional[Any] = None
    required: bool

class TaskMethod(BaseModel):
    name: str
    description: Optional[str] = None
    arguments: List[TaskArgument]
