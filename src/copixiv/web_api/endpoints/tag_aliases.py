"""Tag alias API endpoints."""

from fastapi import APIRouter, Depends, Query

from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import TagAliasCreate, TagAliasSuggestListResponse
from copixiv.application.tag import (
    ListAliasesUseCase, SuggestAliasesUseCase,
    CreateAliasUseCase, DeleteAliasUseCase,
)

router = APIRouter()


@router.get("/")
async def get_tag_aliases(uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = ListAliasesUseCase(uow.tags)
    return await use_case.execute()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
async def suggest_tag_aliases(
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
    target_tag: str | None = Query(None),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    use_case = SuggestAliasesUseCase(uow.tags)
    return await use_case.execute(limit=limit, offset=offset, target_tag=target_tag)


@router.post("/")
async def create_tag_alias(data: TagAliasCreate, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = CreateAliasUseCase(uow.tags)
    alias = await use_case.execute(data.model_dump())
    return alias


@router.delete("/{alias_id}")
async def delete_tag_alias(alias_id: int, uow: SqlUnitOfWork = Depends(get_uow)):
    use_case = DeleteAliasUseCase(uow.tags)
    await use_case.execute(alias_id)
    return {"ok": True}
