"""Tag preference API endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError
from copixiv.deps import get_uow, get_write_uow
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.tags.repo import SQLAlchemyTagRepository


# ---------------------------------------------------------------------------
# Tag preference schemas — carried with the feature (S1).
# ---------------------------------------------------------------------------

class TagPreferenceResponse(BaseModel):
    id: int
    tag: str
    preference: str  # "favourite" | "blocked"
    sort_index: int
    model_config = ConfigDict(from_attributes=True)


class TagPreferenceCreate(BaseModel):
    tag: str
    preference: Literal["favourite", "blocked"]
    sort_index: int = 0


class TagPreferenceUpdate(BaseModel):
    tag: str | None = None
    preference: Literal["favourite", "blocked"] | None = None
    sort_index: int | None = None

router = APIRouter()


@router.get("/", response_model=list[TagPreferenceResponse])
async def get_tag_preferences(uow: SqlUnitOfWork = Depends(get_uow)):
    return await SQLAlchemyTagRepository(uow.session).get_preferences()


@router.post("/", response_model=TagPreferenceResponse)
async def create_tag_preference(data: TagPreferenceCreate, uow: SqlUnitOfWork = Depends(get_write_uow)):
    return await SQLAlchemyTagRepository(uow.session).create_preference(data.model_dump())


@router.put("/{pref_id}", response_model=TagPreferenceResponse)
async def update_tag_preference(
    pref_id: int, data: TagPreferenceUpdate, uow: SqlUnitOfWork = Depends(get_write_uow)
):
    result = await SQLAlchemyTagRepository(uow.session).update_preference(pref_id, data.model_dump(exclude_none=True))
    if result is None:
        raise NotFoundError(f"Tag preference {pref_id} not found")
    return result


@router.delete("/{pref_id}")
async def delete_tag_preference(pref_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await SQLAlchemyTagRepository(uow.session).delete_preference(pref_id):
        raise NotFoundError(f"Tag preference {pref_id} not found")
    return {"ok": True}


@router.post("/reorder")
async def reorder_tag_preferences(ids: list[int], uow: SqlUnitOfWork = Depends(get_write_uow)):
    await SQLAlchemyTagRepository(uow.session).reorder_preferences(ids)
    return {"ok": True}
