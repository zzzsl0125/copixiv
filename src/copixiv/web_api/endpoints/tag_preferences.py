"""Tag preference API endpoints."""

from fastapi import APIRouter, Depends

from copixiv.domain.exceptions import NotFoundError
from copixiv.web_api.deps import get_uow, get_write_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import (
    TagPreferenceCreate, TagPreferenceUpdate, TagPreferenceResponse,
)

router = APIRouter()


# Route manifest — mounted automatically by the composition root
# (docs/MODULARITY.md §M9): (prefix, tags) travels with the module.
ROUTE = ("/api/tag-preferences", ["tag_preferences"])



@router.get("/", response_model=list[TagPreferenceResponse])
async def get_tag_preferences(uow: SqlUnitOfWork = Depends(get_uow)):
    return await uow.tags.get_preferences()


@router.post("/", response_model=TagPreferenceResponse)
async def create_tag_preference(data: TagPreferenceCreate, uow: SqlUnitOfWork = Depends(get_write_uow)):
    return await uow.tags.create_preference(data.model_dump())


@router.put("/{pref_id}", response_model=TagPreferenceResponse)
async def update_tag_preference(
    pref_id: int, data: TagPreferenceUpdate, uow: SqlUnitOfWork = Depends(get_write_uow)
):
    result = await uow.tags.update_preference(pref_id, data.model_dump(exclude_none=True))
    if result is None:
        raise NotFoundError(f"Tag preference {pref_id} not found")
    return result


@router.delete("/{pref_id}")
async def delete_tag_preference(pref_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await uow.tags.delete_preference(pref_id):
        raise NotFoundError(f"Tag preference {pref_id} not found")
    return {"ok": True}


@router.post("/reorder")
async def reorder_tag_preferences(ids: list[int], uow: SqlUnitOfWork = Depends(get_write_uow)):
    await uow.tags.reorder_preferences(ids)
    return {"ok": True}
