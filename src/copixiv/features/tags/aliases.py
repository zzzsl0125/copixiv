"""Tag alias API endpoints."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.deps import get_uow, get_write_uow
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.tags.repo import SQLAlchemyTagRepository


# ---------------------------------------------------------------------------
# Tag alias schemas — carried with the feature (S1).
# ---------------------------------------------------------------------------

class TagAliasBase(BaseModel):
    source: str
    target: str


class TagAliasResponse(TagAliasBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class TagCandidate(BaseModel):
    id: int
    name: str
    reference_count: int


class TagAliasSuggestResponse(BaseModel):
    target: TagCandidate
    candidates: list[TagCandidate]


class TagAliasSuggestListResponse(BaseModel):
    items: list[TagAliasSuggestResponse]
    next_offset: int

router = APIRouter()


@router.get("/", response_model=list[TagAliasResponse])
async def get_tag_aliases(uow: SqlUnitOfWork = Depends(get_uow)):
    return await SQLAlchemyTagRepository(uow.session).get_aliases()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
async def suggest_tag_aliases(
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
    target_tag: str | None = Query(None),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    """相似标签建议。

    ``offset`` 是**游标**（不是行偏移）：把上一次响应里的 ``next_offset`` 原样
    传回来续扫即可（字段名保留 ``offset`` / ``next_offset`` 是为了不动前端契约，
    前端只把它当不透明整数回传）。``target_tag=`` 模式只指名一个 target，
    没有「页」可续，因此忽略 ``offset`` 且 ``next_offset`` 恒为 0。
    """
    return await SQLAlchemyTagRepository(uow.session).suggest_aliases(limit=limit, offset=offset, target_tag=target_tag)


@router.post("/", response_model=TagAliasResponse)
async def create_tag_alias(data: TagAliasBase, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if data.source == data.target:
        raise ValidationError("原标签不能和目标标签相同")
    alias = await SQLAlchemyTagRepository(uow.session).create_alias(data.model_dump())
    await SQLAlchemyTagRepository(uow.session).apply_alias_retroactively(data.source, data.target)
    return alias


@router.delete("/{alias_id}")
async def delete_tag_alias(alias_id: int, uow: SqlUnitOfWork = Depends(get_write_uow)):
    if not await SQLAlchemyTagRepository(uow.session).delete_alias(alias_id):
        raise NotFoundError(f"Tag alias {alias_id} not found")
    return {"ok": True}
