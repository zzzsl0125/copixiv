from pydantic import BaseModel
from typing import List, Optional, Any

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
    
    class Config:
        from_attributes = True

class NovelListResponse(BaseModel):
    novels: List[NovelBase]
    cursor: Optional[NovelBase] = None 
