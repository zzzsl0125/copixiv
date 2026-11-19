"""Tag preference API endpoints."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import NotFoundError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.deps import UowDep
from copixiv.features.tags.preference_repo import TagPreferenceRepository


class TagPreferenceResponse(BaseModel):
    id: int
    tag: str
    preference: Literal["favourite", "blocked"]
    sort_index: int
    model_config = ConfigDict(from_attributes=True)


class TagPreferenceCreate(BaseModel):
    tag: str
    preference: Literal["favourite", "blocked"]
    sort_index: int = 0


class TagPreferenceUpdate(BaseModel):
    tag: str | None = None
    preference: Literal["favourite", "blocked"] | None = None
    sort_index: int | None = None


router = APIRouter()


@router.get("", response_model=list[TagPreferenceResponse])
def get_tag_preferences(uow: UowDep):
    with uow.begin():
        return [
            TagPreferenceResponse.model_validate(pref)
            for pref in TagPreferenceRepository(uow.session).get_preferences()
        ]


@router.post("", response_model=TagPreferenceResponse)
def create_tag_preference(data: TagPreferenceCreate, uow: UowDep):
    def _write(tx: SqlUnitOfWork):
        pref = TagPreferenceRepository(tx.session).create_preference(data.model_dump())
        return TagPreferenceResponse.model_validate(pref)

    return run_write_transaction_sync(uow, _write)


@router.put("/{pref_id}", response_model=TagPreferenceResponse)
def update_tag_preference(
    pref_id: int,
    data: TagPreferenceUpdate,
    uow: UowDep,
):
    def _write(tx: SqlUnitOfWork):
        result = TagPreferenceRepository(tx.session).update_preference(
            pref_id, data.model_dump(exclude_none=True)
        )
        if result is None:
            raise NotFoundError(f"Tag preference {pref_id} not found")
        return TagPreferenceResponse.model_validate(result)

    return run_write_transaction_sync(uow, _write)


@router.delete("/{pref_id}", status_code=204)
def delete_tag_preference(pref_id: int, uow: UowDep):
    def _write(tx: SqlUnitOfWork):
        if not TagPreferenceRepository(tx.session).delete_preference(pref_id):
            raise NotFoundError(f"Tag preference {pref_id} not found")

    run_write_transaction_sync(uow, _write)


@router.post("/reorder")
def reorder_tag_preferences(ids: list[int], uow: UowDep):
    run_write_transaction_sync(
        uow,
        lambda tx: TagPreferenceRepository(tx.session).reorder_preferences(ids),
    )
    return {"ok": True}
