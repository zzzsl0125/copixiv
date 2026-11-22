"""Failed-novel ledger API — the 「失败记录」 management view.

Exposes the ``failed_novel`` table (the download-failure ledger) so the
frontend can show what failed, when, and why — and act on it:

- ``GET /api/failed-novels``        — paginated list; actionable failures
  first (newest first), "not found" family (deleted/unfetchable
  works) last
- ``POST /api/failed-novels/{novel_id}/reset-count`` — reset one record's
  failure count to 0 (record stays; unblocks automatic retry)
- ``POST /api/failed-novels/reset-count`` — reset every count
- ``POST /api/failed-novels/retry`` — enqueue a ``failed_retry`` task for
  the given ids (runs through the task system, so the global execution
  lock and history recording apply)
- ``POST /api/failed-novels/retry-all`` — enqueue one ``failed_retry``
  task for the WHOLE ledger (server-side "全部重试" — independent of the
  frontend's pagination state)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from copixiv.core.exceptions import ValidationError
from copixiv.db.tx import run_write_transaction_sync
from copixiv.deps import TaskManagerDep, UowDep
from copixiv.features.failures.repo import FailedNovelRepository

# ---------------------------------------------------------------------------
# Failed-novel ledger schemas — carried with the feature (S1).
# ---------------------------------------------------------------------------


class FailedNovelItem(BaseModel):
    """One download-failure record.

    ``title`` may be null for legacy rows recorded before title capture
    existed; ``last_failed_at`` may be null for pre-migration rows (they
    sort to the end of the list).
    """

    novel_id: int
    title: str | None = None
    failure_type: str | None = None
    error_message: str | None = None
    failed_times: int = 1
    # timestamptz → datetime; Pydantic serializes to an ISO string on the wire.
    last_failed_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class FailedNovelListResponse(BaseModel):
    items: list[FailedNovelItem]
    total: int
    offset: int = 0
    limit: int = 100


class FailedNovelRetryRequest(BaseModel):
    novel_ids: list[int]


class BatchTaskResponse(BaseModel):
    """A batch operation enqueued into the background task system."""

    task_id: int
    matched: int


router = APIRouter()


# A single retry task fans out with one client per id; cap the payload so
# an accidental "select all" cannot enqueue an unbounded task.
MAX_RETRY_IDS = 500


@router.get("", response_model=FailedNovelListResponse)
def list_failed_novels(
    uow: UowDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    with uow.begin(read_only=True):
        repo = FailedNovelRepository(uow.session)
        items = repo.list_failures(offset=offset, limit=limit)
        total = repo.count()
        return FailedNovelListResponse(
            items=[FailedNovelItem.model_validate(i) for i in items],
            total=total,
            offset=offset,
            limit=limit,
        )


@router.post("/{novel_id}/reset-count", status_code=204)
def reset_failed_novel_count(
    novel_id: int,
    uow: UowDep,
):
    """Reset one record's failure count to 0 — the record stays.

    Preferred over deletion: the history row (title / error / first-seen
    info) remains visible in the 「失败记录」 view while the novel is
    unblocked for automatic retry.
    """
    run_write_transaction_sync(uow, lambda tx: FailedNovelRepository(tx.session).reset_count(novel_id))


@router.post("/reset-count", status_code=204)
def reset_all_failed_novel_counts(
    uow: UowDep,
):
    """Reset every failure count to 0 — records stay, all unblocked."""
    run_write_transaction_sync(uow, lambda tx: FailedNovelRepository(tx.session).reset_all())


@router.post("/retry", response_model=BatchTaskResponse)
def retry_failed_novels(
    body: FailedNovelRetryRequest,
    task_manager: TaskManagerDep,
):
    """Enqueue a background ``failed_retry`` task for the given ids."""
    ids = list(dict.fromkeys(body.novel_ids))
    if not ids:
        raise ValidationError("请至少选择一本小说")
    if len(ids) > MAX_RETRY_IDS:
        raise ValidationError(f"一次最多重试 {MAX_RETRY_IDS} 本（当前 {len(ids)} 本）")
    task_id = task_manager.run_task("failed_retry", params={"novel_ids": ids})
    return BatchTaskResponse(task_id=task_id, matched=len(ids))


@router.post("/retry-all", response_model=BatchTaskResponse)
def retry_all_failed_novels(
    uow: UowDep,
    task_manager: TaskManagerDep,
):
    """Enqueue a background ``failed_retry`` task for EVERY failure record.

    This is the server-side 「全部重试」: the ledger itself is the payload,
    so the whole ledger gets retried regardless of which page the client
    currently has loaded. Unlike ``POST /retry`` there is no id-payload
    cap — the confirmation dialog on the client is the consent guard, and
    the task fans out with the same one-client-per-id pattern.
    """
    with uow.begin(read_only=True):
        ids = FailedNovelRepository(uow.session).all_ids()
    if not ids:
        raise ValidationError("当前没有失败记录")
    task_id = task_manager.run_task("failed_retry", params={"novel_ids": ids})
    return BatchTaskResponse(task_id=task_id, matched=len(ids))
