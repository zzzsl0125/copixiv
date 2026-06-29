"""Tag alias API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.tag import TagRepository

router = APIRouter()


@router.get("/")
async def get_tag_aliases(db: Session = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.get_aliases()


@router.post("/")
async def create_tag_alias(data: dict, db: Session = Depends(get_db)):
    repo = TagRepository(db)
    alias = await repo.create_alias(data)
    # Apply retroactively
    await repo.apply_alias_retroactively(data["source"], data["target"])
    return alias


@router.delete("/{alias_id}")
async def delete_tag_alias(alias_id: int, db: Session = Depends(get_db)):
    repo = TagRepository(db)
    if not await repo.delete_alias(alias_id):
        raise HTTPException(status_code=404)
    return {"ok": True}
