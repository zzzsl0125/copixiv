"""Tag preference API endpoints."""

from fastapi import APIRouter, Depends

from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import (
    TagPreferenceCreate, TagPreferenceUpdate, TagPreferenceResponse,
)
from copixiv.application.tag import (
    ListPreferencesUseCase, CreatePreferenceUseCase, UpdatePreferenceUseCase,
    DeletePreferenceUseCase, ReorderPreferencesUseCase,
)

router = APIRouter()


@router.get("/", response_model=list[TagPreferenceResponse])
async def get_tag_preferences(uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = ListPreferencesUseCase(uow.tags)
    return await use_case.execute()


@router.post("/", response_model=TagPreferenceResponse)
async def create_tag_preference(data: TagPreferenceCreate, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = CreatePreferenceUseCase(uow.tags)
    result = await use_case.execute(data.model_dump())
    return result


@router.put("/{pref_id}", response_model=TagPreferenceResponse)
async def update_tag_preference(
    pref_id: int, data: TagPreferenceUpdate, uow: SqlUnitOfWork = Depends(get_uow)
):
    use_case = UpdatePreferenceUseCase(uow.tags)
    result = await use_case.execute(pref_id, data.model_dump(exclude_none=True))
    return result


@router.delete("/{pref_id}")
async def delete_tag_preference(pref_id: int, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = DeletePreferenceUseCase(uow.tags)
    await use_case.execute(pref_id)
    return {"ok": True}


@router.post("/reorder")
async def reorder_tag_preferences(ids: list[int], uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = ReorderPreferencesUseCase(uow.tags)
    await use_case.execute(ids)
    return {"ok": True}
