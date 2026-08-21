"""Failed-novel ledger API — the 「失败记录」 management view.

Exposes the ``failed_novel`` table (the download-failure ledger) so the
frontend can show what failed, when, and why — and act on it:

- ``GET /api/failed-novels``        — paginated list; actionable failures
  first (newest first), "Page not found" family (deleted/unfetchable
  works) last
- ``GET /api/failed-novels/count``  — sidebar badge number
- ``POST /api/failed-novels/{novel_id}/reset-count`` — reset one record's
  failure count to 0 (record stays; unblocks automatic retry)
- ``POST /api/failed-novels/reset-count`` — reset every count
- ``DELETE /api/failed-novels/{novel_id}`` — clear one record entirely
- ``DELETE /api/failed-novels``     — clear the whole ledger
- ``POST /api/failed-novels/retry`` — enqueue a ``failed_retry`` task for
  the given ids (runs through the task system, so the global execution
  lock and history recording apply)
- ``POST /api/failed-novels/retry-all`` — enqueue one ``failed_retry``
  task for the WHOLE ledger (server-side "全部重试" — independent of the
  frontend's pagination state)
"""

from fastapi import APIRouter, Body, Depends, Query

from copixiv.domain.exceptions import ValidationError
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.web_api.deps import get_task_manager, get_uow, get_write_uow
from copixiv.web_api.schemas import (
    BatchTaskResponse,
    FailedNovelCountResponse,
    FailedNovelItem,
    FailedNovelListResponse,
    FailedNovelRetryRequest,
)

router = APIRouter()


# Route manifest — mounted automatically by the composition root
# (docs/MODULARITY.md §M9): (prefix, tags) travels with the module.
ROUTE = ("/api/failed-novels", ["failed-novels"])


# A single retry task fans out with one client per id; cap the payload so
# an accidental "select all" cannot enqueue an unbounded task.
MAX_RETRY_IDS = 500


@router.get("", response_model=FailedNovelListResponse)
async def list_failed_novels(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    uow: SqlUnitOfWork = Depends(get_uow),
):
    items = uow.failed_novels.list(offset=offset, limit=limit)
    total = uow.failed_novels.count()
    return FailedNovelListResponse(
        items=[FailedNovelItem.model_validate(i) for i in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/count", response_model=FailedNovelCountResponse)
async def count_failed_novels(
    uow: SqlUnitOfWork = Depends(get_uow),
):
    return FailedNovelCountResponse(count=uow.failed_novels.count())


@router.delete("/{novel_id}", status_code=204)
async def delete_failed_novel(
    novel_id: int,
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    """Clear one failure record (unblocks automatic retry)."""
    uow.failed_novels.forget(novel_id)


@router.post("/{novel_id}/reset-count", status_code=204)
async def reset_failed_novel_count(
    novel_id: int,
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    """Reset one record's failure count to 0 — the record stays.

    Preferred over deletion: the history row (title / error / first-seen
    info) remains visible in the 「失败记录」 view while the novel is
    unblocked for automatic retry.
    """
    uow.failed_novels.reset_count(novel_id)


@router.post("/reset-count", status_code=204)
async def reset_all_failed_novel_counts(
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    """Reset every failure count to 0 — records stay, all unblocked."""
    uow.failed_novels.reset_all()


@router.delete("", status_code=204)
async def clear_all_failed_novels(
    uow: SqlUnitOfWork = Depends(get_write_uow),
):
    """Clear the whole ledger."""
    uow.failed_novels.clear_all()


@router.post("/retry", response_model=BatchTaskResponse)
async def retry_failed_novels(
    body: FailedNovelRetryRequest = Body(...),
    task_manager=Depends(get_task_manager),
):
    """Enqueue a background ``failed_retry`` task for the given ids."""
    ids = list(dict.fromkeys(body.novel_ids))
    if not ids:
        raise ValidationError("请至少选择一本小说")
    if len(ids) > MAX_RETRY_IDS:
        raise ValidationError(
            f"一次最多重试 {MAX_RETRY_IDS} 本（当前 {len(ids)} 本）"
        )
    task_id = task_manager.run_task("failed_retry", params={"novel_ids": ids})
    return BatchTaskResponse(task_id=task_id, matched=len(ids))


@router.post("/retry-all", response_model=BatchTaskResponse)
async def retry_all_failed_novels(
    uow: SqlUnitOfWork = Depends(get_uow),
    task_manager=Depends(get_task_manager),
):
    """Enqueue a background ``failed_retry`` task for EVERY failure record.

    This is the server-side 「全部重试」: the ledger itself is the payload,
    so the whole ledger gets retried regardless of which page the client
    currently has loaded. Unlike ``POST /retry`` there is no id-payload
    cap — the confirmation dialog on the client is the consent guard, and
    the task fans out with the same one-client-per-id pattern.
    """
    ids = uow.failed_novels.all_ids()
    if not ids:
        raise ValidationError("当前没有失败记录")
    task_id = task_manager.run_task("failed_retry", params={"novel_ids": ids})
    return BatchTaskResponse(task_id=task_id, matched=len(ids))
