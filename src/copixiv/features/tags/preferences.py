"""Tag preference API endpoints."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, field_validator

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import UowDep
from copixiv.features.tags.repo import SQLAlchemyTagRepository


class TagPreferenceResponse(BaseModel):
    id: int
    tag: str
    preference: Literal["favourite", "blocked"]
    sort_index: int
    model_config = ConfigDict(from_attributes=True)


class TagPreferenceCreate(BaseModel):
    tag: str
    preference: Literal["favourite", "blocked"]
    sort_index: int | None = None

    @field_validator("tag")
    @classmethod
    def _strip_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("标签名称不能为空")
        return stripped


router = APIRouter()


@router.get("", response_model=list[TagPreferenceResponse])
def get_tag_preferences(uow: UowDep):
    with uow.begin(read_only=True):
        return [
            TagPreferenceResponse.model_validate(pref)
            for pref in SQLAlchemyTagRepository(uow.session).get_preferences()
        ]


@router.post("", response_model=TagPreferenceResponse)
def create_tag_preference(data: TagPreferenceCreate, uow: UowDep):
    def _write(tx: SqlUnitOfWork):
        pref = SQLAlchemyTagRepository(tx.session).create_preference(data.model_dump())
        return TagPreferenceResponse.model_validate(pref)

    return run_write_transaction_sync(uow, _write)


@router.delete("/{pref_id}", status_code=204)
def delete_tag_preference(pref_id: int, uow: UowDep):
    def _write(tx: SqlUnitOfWork):
        if not SQLAlchemyTagRepository(tx.session).delete_preference(pref_id):
            raise NotFoundError(f"Tag preference {pref_id} not found")

    run_write_transaction_sync(uow, _write)


@router.post("/reorder")
def reorder_tag_preferences(ids: list[int], uow: UowDep):
    run_write_transaction_sync(
        uow,
        lambda tx: SQLAlchemyTagRepository(tx.session).reorder_preferences(ids),
    )
    return {"ok": True}
