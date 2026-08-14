"""Tag alias API endpoints."""

from fastapi import APIRouter, Depends, Query

from copixiv.domain.exceptions import NotFoundError, ValidationError
from copixiv.web_api.deps import get_uow
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.schemas import TagAliasBase, TagAliasSuggestListResponse

router = APIRouter()


@router.get("/")
async def get_tag_aliases(uow: SqlUnitOfWork = Depends(get_uow)):
    return await uow.tags.get_aliases()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
async def suggest_tag_aliases(
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
    target_tag: str | None = Query(None),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    return await uow.tags.suggest_aliases(limit=limit, offset=offset, target_tag=target_tag)


@router.post("/")
async def create_tag_alias(data: TagAliasBase, uow: SqlUnitOfWork = Depends(get_uow)):
    if data.source == data.target:
        raise ValidationError("原标签不能和目标标签相同")
    alias = await uow.tags.create_alias(data.model_dump())
    await uow.tags.apply_alias_retroactively(data.source, data.target)
    return alias


@router.delete("/{alias_id}")
async def delete_tag_alias(alias_id: int, uow: SqlUnitOfWork = Depends(get_uow)):
    if not await uow.tags.delete_alias(alias_id):
        raise NotFoundError(f"Tag alias {alias_id} not found")
    return {"ok": True}
