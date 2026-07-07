"""Tag alias API endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.tag import TagRepository
from copixiv.web_api.schemas import TagAliasCreate, TagAliasSuggestListResponse
from copixiv.application.tag import (
    ListAliasesUseCase, SuggestAliasesUseCase,
    CreateAliasUseCase, DeleteAliasUseCase,
)

router = APIRouter()


@router.get("/")
async def get_tag_aliases(db: Session = Depends(get_db)):
    use_case = ListAliasesUseCase(TagRepository(db))
    return await use_case.execute()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
async def suggest_tag_aliases(
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
    target_tag: str | None = Query(None),
    db: Session = Depends(get_db),
):
    use_case = SuggestAliasesUseCase(TagRepository(db))
    return await use_case.execute(limit=limit, offset=offset, target_tag=target_tag)


@router.post("/")
async def create_tag_alias(data: TagAliasCreate, db: Session = Depends(get_db)):
    use_case = CreateAliasUseCase(TagRepository(db))
    alias = await use_case.execute(data.model_dump())
    db.commit()
    return alias


@router.delete("/{alias_id}")
async def delete_tag_alias(alias_id: int, db: Session = Depends(get_db)):
    use_case = DeleteAliasUseCase(TagRepository(db))
    await use_case.execute(alias_id)
    db.commit()
    return {"ok": True}
