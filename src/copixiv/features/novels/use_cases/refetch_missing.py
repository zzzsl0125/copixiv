"""Re-fetch Pixiv novels whose local body file vanished (``has_epub = MISSING_SOURCE``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from copixiv.core.models import TaskResult
from copixiv.db.tx import read_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.failures.repo import MAX_RETRIES
from copixiv.features.novels.ingest import ingest
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient

_REFETCH_BATCH_SIZE = 50


def count_missing_source(session: Session) -> int:
    """How many Pixiv novel rows currently carry ``MISSING_SOURCE``."""
    return SQLAlchemyNovelRepository(session).count_missing_source()


def find_refetch_candidates(
    session: Session,
    *,
    limit: int | None,
    skip_ids: set[int] | None = None,
    skip_cutoff: datetime | None = None,
) -> list[int]:
    """Ids whose body file is gone and which are not excluded by failure ledger."""
    return SQLAlchemyNovelRepository(session).find_refetch_candidates(
        limit=limit,
        max_retries=MAX_RETRIES,
        skip_ids=skip_ids,
        skip_cutoff=skip_cutoff,
    )


async def refetch_missing_novels(
    *,
    apply: bool = False,
    limit: int | None = None,
    force_recent_days: int = 0,
    uow: SqlUnitOfWork,
    client: PixivClient | None,
    file_storage: Any,
    image_downloader: Any,
) -> TaskResult:
    """Re-download novels whose local text file is gone."""
    if client is None or file_storage is None:
        return TaskResult(summary="重抓丢失正文: 缺少 pixiv 客户端或文件存储，跳过")

    def _plan(uw: SqlUnitOfWork) -> tuple[int, list[int]]:
        total = count_missing_source(uw.session)
        skip_cutoff = datetime.now(UTC) - timedelta(days=force_recent_days) if force_recent_days else None
        candidates = find_refetch_candidates(
            uw.session,
            limit=limit,
            skip_cutoff=skip_cutoff,
        )
        return total, candidates

    total, candidates = await read_transaction(uow, _plan)

    if not candidates:
        return TaskResult(
            summary=(f"重抓丢失正文: 没有可抓的候选（has_epub=4 共 {total} 本，均已放弃或已跳过）")
        )

    if not apply:
        logger.info(
            "refetch_missing_source: 试运行，候选 {} 本（加 apply=true 才抓）",
            len(candidates),
        )
        return TaskResult(
            summary=(
                f"重抓丢失正文（试运行）: has_epub=4 共 {total} 本，"
                f"本轮候选 {len(candidates)} 本；未发起任何请求"
                "（设 apply=true 执行）"
            )
        )

    ok_ids: list[int] = []
    failed_ids: list[int] = []
    for i in range(0, len(candidates), _REFETCH_BATCH_SIZE):
        chunk = candidates[i : i + _REFETCH_BATCH_SIZE]
        out = await ingest(
            ids=chunk,
            force=True,
            session_factory=uow.session_factory,
            client=client,
            file_storage=file_storage,
            image_downloader=image_downloader,
        )
        for nid, reason in out.failed:
            logger.error(f"refetch_missing_source: #{nid} 重抓失败: {reason}")
        chunk_failed = set(out.failed_ids)
        failed_ids.extend(out.failed_ids)
        ok_ids.extend(nid for nid in chunk if nid not in chunk_failed)

    remaining = await read_transaction(
        uow,
        lambda uw: count_missing_source(uw.session),
    )

    logger.info(
        "refetch_missing_source: 成功 {} / {} 本，仍为 has_epub=4 的 {} 本",
        len(ok_ids),
        len(candidates),
        remaining,
    )
    return TaskResult(
        summary=(
            f"重抓丢失正文: 成功 {len(ok_ids)}/{len(candidates)} 本，"
            f"失败 {len(failed_ids)} 本；仍有 {remaining} 本缺正文"
        ),
        failed_ids=failed_ids,
    )


__all__ = [
    "count_missing_source",
    "find_refetch_candidates",
    "refetch_missing_novels",
]
