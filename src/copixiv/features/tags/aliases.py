"""Tag alias API endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, field_validator

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import UowDep
from copixiv.features.tags.repo import (
    SQLAlchemyTagRepository,
    create_and_apply_aliases_batch,
    replace_alias_group,
)
from copixiv.features.tags.repo import suggest_tag_aliases as _suggest_tag_aliases


def _strip_tag_name(v: str) -> str:
    stripped = v.strip()
    if not stripped:
        raise ValueError("标签名称不能为空")
    return stripped


def _strip_tag_list(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for v in values:
        stripped = v.strip()
        if not stripped:
            raise ValueError("标签名称不能为空")
        if stripped not in cleaned:
            cleaned.append(stripped)
    if not cleaned:
        raise ValueError("原标签列表不能为空")
    return cleaned


class TagAliasBatchCreate(BaseModel):
    sources: list[str]
    target: str

    @field_validator("target")
    @classmethod
    def _strip_target(cls, v: str) -> str:
        return _strip_tag_name(v)

    @field_validator("sources")
    @classmethod
    def _strip_sources(cls, v: list[str]) -> list[str]:
        return _strip_tag_list(v)


class TagAliasGroupReplace(TagAliasBatchCreate):
    old_target: str

    @field_validator("old_target")
    @classmethod
    def _strip_old_target(cls, v: str) -> str:
        return _strip_tag_name(v)


class TagAliasResponse(BaseModel):
    id: int
    source: str
    target: str
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
    next_cursor: int | None = None


router = APIRouter()


@router.get("", response_model=list[TagAliasResponse])
def get_tag_aliases(uow: UowDep):
    with uow.begin(read_only=True):
        return SQLAlchemyTagRepository(uow.session).get_aliases()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
def suggest_tag_aliases(
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
    offset: Annotated[int, Query(ge=0)] = 0,
    target_tag: Annotated[str | None, Query()] = None,
):
    """相似标签建议。

    ``offset`` 是游标（不是行偏移）：把上一次响应里的 ``next_cursor`` 原样
    传回来续扫即可（``None`` 表示已扫描结束）。``target_tag=`` 模式只指名一个
    target，没有「页」可续，因此忽略 ``offset`` 且 ``next_cursor`` 恒为 ``None``。
    """
    with uow.begin(read_only=True):
        return _suggest_tag_aliases(uow.session, limit=limit, offset=offset, target_tag=target_tag)


@router.post("/batch", response_model=list[TagAliasResponse])
def create_tag_aliases_batch(data: TagAliasBatchCreate, uow: UowDep):
    if data.target in data.sources:
        raise ValidationError("原标签不能和目标标签相同")

    def _write(tx: SqlUnitOfWork):
        aliases = create_and_apply_aliases_batch(tx.session, data.sources, data.target)
        return [TagAliasResponse.model_validate(alias) for alias in aliases]

    return run_write_transaction_sync(uow, _write)


@router.put("/groups", response_model=list[TagAliasResponse])
def replace_tag_alias_group(data: TagAliasGroupReplace, uow: UowDep):
    if data.target in data.sources:
        raise ValidationError("原标签不能和目标标签相同")

    def _write(tx: SqlUnitOfWork):
        aliases = replace_alias_group(tx.session, data.old_target, data.target, data.sources)
        return [TagAliasResponse.model_validate(alias) for alias in aliases]

    return run_write_transaction_sync(uow, _write)


@router.delete("/groups", status_code=204)
def delete_tag_alias_group(
    target: Annotated[str, Query(min_length=1)],
    uow: UowDep,
):
    cleaned_target = _strip_tag_name(target)

    def _write(tx: SqlUnitOfWork):
        if SQLAlchemyTagRepository(tx.session).delete_aliases_by_target(cleaned_target) == 0:
            raise NotFoundError(f"Tag alias group '{cleaned_target}' not found")

    run_write_transaction_sync(uow, _write)
