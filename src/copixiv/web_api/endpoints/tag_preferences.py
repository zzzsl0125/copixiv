"""Tag preference API endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.web_api.schemas import (
    TagPreferenceCreate, TagPreferenceUpdate, TagPreferenceResponse,
)
from copixiv.infrastructure.repositories.tag import TagRepository
from copixiv.application.tag import (
    ListPreferencesUseCase, CreatePreferenceUseCase, UpdatePreferenceUseCase,
    DeletePreferenceUseCase, ReorderPreferencesUseCase,
)

router = APIRouter()


@router.get("/", response_model=list[TagPreferenceResponse])
async def get_tag_preferences(db: Session = Depends(get_db)):
    use_case = ListPreferencesUseCase(TagRepository(db))
    return await use_case.execute()


@router.post("/", response_model=TagPreferenceResponse)
async def create_tag_preference(data: TagPreferenceCreate, db: Session = Depends(get_db)):
    use_case = CreatePreferenceUseCase(TagRepository(db))
    result = await use_case.execute(data.model_dump())
    db.commit()
    return result


@router.put("/{pref_id}", response_model=TagPreferenceResponse)
async def update_tag_preference(
    pref_id: int, data: TagPreferenceUpdate, db: Session = Depends(get_db)
):
    use_case = UpdatePreferenceUseCase(TagRepository(db))
    result = await use_case.execute(pref_id, data.model_dump(exclude_none=True))
    db.commit()
    return result


@router.delete("/{pref_id}")
async def delete_tag_preference(pref_id: int, db: Session = Depends(get_db)):
    use_case = DeletePreferenceUseCase(TagRepository(db))
    await use_case.execute(pref_id)
    db.commit()
    return {"ok": True}


@router.post("/reorder")
async def reorder_tag_preferences(ids: list[int], db: Session = Depends(get_db)):
    use_case = ReorderPreferencesUseCase(TagRepository(db))
    await use_case.execute(ids)
    db.commit()
    return {"ok": True}
