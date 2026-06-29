"""Tag preference API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.tag import TagRepository

router = APIRouter()


@router.get("/")
async def get_tag_preferences(db: Session = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.get_preferences()


@router.post("/")
async def create_tag_preference(data: dict, db: Session = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.create_preference(data)


@router.put("/{pref_id}")
async def update_tag_preference(pref_id: int, data: dict, db: Session = Depends(get_db)):
    repo = TagRepository(db)
    result = await repo.update_preference(pref_id, data)
    if not result:
        raise HTTPException(status_code=404)
    return result


@router.delete("/{pref_id}")
async def delete_tag_preference(pref_id: int, db: Session = Depends(get_db)):
    repo = TagRepository(db)
    if not await repo.delete_preference(pref_id):
        raise HTTPException(status_code=404)
    return {"ok": True}


@router.post("/reorder")
async def reorder_tag_preferences(ids: list[int], db: Session = Depends(get_db)):
    repo = TagRepository(db)
    await repo.reorder_preferences(ids)
    return {"ok": True}
