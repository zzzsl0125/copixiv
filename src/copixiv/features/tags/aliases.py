"""Tag alias API endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import UowDep
from copixiv.features.tags.alias_repo import TagAliasRepository
from copixiv.features.tags.apply_alias import create_and_apply_alias
from copixiv.features.tags.suggest import suggest_tag_aliases as _suggest_tag_aliases


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


@router.get("", response_model=list[TagAliasResponse])
def get_tag_aliases(uow: UowDep):
    with uow.begin():
        return TagAliasRepository(uow.session).get_aliases()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
def suggest_tag_aliases(
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
    offset: Annotated[int, Query(ge=0)] = 0,
    target_tag: Annotated[str | None, Query()] = None,
):
    """相似标签建议。

    ``offset`` 是**游标**（不是行偏移）：把上一次响应里的 ``next_offset`` 原样
    传回来续扫即可（字段名保留 ``offset`` / ``next_offset`` 是为了不动前端契约，
    前端只把它当不透明整数回传）。``target_tag=`` 模式只指名一个 target，
    没有「页」可续，因此忽略 ``offset`` 且 ``next_offset`` 恒为 0。
    """
    with uow.begin():
        return _suggest_tag_aliases(uow.session, limit=limit, offset=offset, target_tag=target_tag)


@router.post("", response_model=TagAliasResponse)
def create_tag_alias(data: TagAliasBase, uow: UowDep):
    if data.source == data.target:
        raise ValidationError("原标签不能和目标标签相同")

    def _write(tx: SqlUnitOfWork):
        alias = create_and_apply_alias(tx.session, data.source, data.target)
        return TagAliasResponse.model_validate(alias)

    return run_write_transaction_sync(uow, _write)


@router.delete("/{alias_id}", status_code=204)
def delete_tag_alias(alias_id: int, uow: UowDep):
    def _write(tx: SqlUnitOfWork):
        if not TagAliasRepository(tx.session).delete_alias(alias_id):
            raise NotFoundError(f"Tag alias {alias_id} not found")

    run_write_transaction_sync(uow, _write)
