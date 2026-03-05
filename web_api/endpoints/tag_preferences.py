from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List
from web_api.schemas import TagPreferenceResponse

from sqlalchemy.orm import Session

from core.db.repositories.system import System
from core.db.models import TagPreferenceType
from web_api.deps import get_db

router = APIRouter()

class TagPreferenceRequest(BaseModel):
    tag: str
    preference: TagPreferenceType

@router.get("/", response_model=List[TagPreferenceResponse])
def get_tag_preferences(db: Session = Depends(get_db)):
    system_repo = System(db)
    return system_repo.get_tag_preferences()

@router.post("/")
def set_tag_preference(request: TagPreferenceRequest, db: Session = Depends(get_db)):
    system_repo = System(db)
    system_repo.set_tag_preference(request.tag, request.preference)
    db.commit()
    return {"ok": True}

@router.delete("/{tag}")
def delete_tag_preference(tag: str, db: Session = Depends(get_db)):
    system_repo = System(db)
    system_repo.delete_tag_preference(tag)
    db.commit()
    return {"ok": True}

@router.post("/reorder")
def reorder_tag_preferences(tag_ids: List[int], db: Session = Depends(get_db)):
    system_repo = System(db)
    if not system_repo.update_tag_preference_order(tag_ids):
        raise HTTPException(status_code=500, detail="Failed to reorder tags")
    db.commit()
    return {"ok": True}



