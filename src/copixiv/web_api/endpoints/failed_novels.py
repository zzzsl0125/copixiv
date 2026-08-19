"""Failed-novel ledger API — the 「下载失败」 management view.

Exposes the ``failed_novel`` table (the download-failure ledger) so the
frontend can show what failed, when, and why — and act on it:

- ``GET /api/failed-novels``        — paginated list, newest failure first
- ``GET /api/failed-novels/count``  — sidebar badge number
- ``DELETE /api/failed-novels/{novel_id}`` — clear one record (unblocks
  retry for records that reached the failed-too-many-times threshold)
- ``DELETE /api/failed-novels``     — clear the whole ledger
- ``POST /api/failed-novels/retry`` — enqueue a ``failed_retry`` task for
  the given ids (runs through the task system, so the global execution
  lock and history recording apply)
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
