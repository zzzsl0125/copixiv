"""``refetch_missing_source`` — re-download novels whose body file vanished.

**Why this is a task and not part of the reconciler.**  ``check_epub`` walks the
disk and *labels* rows; it never touches the network.  ``MISSING_SOURCE`` (4) is
the honest label for "``novel.path`` is set but no text file is on disk" — 684
production rows, all pixiv books, none with a renamed sibling anywhere.  A label
is the right outcome for the reconciler (it must stay cheap and offline), but it
is not a *fix*: those rows are downloadable again, and nothing was downloading
them.  This task is the other half.

**Why not just extend ``failed_retry``.**  That task retries an explicit id list
from the ``failed_novel`` ledger, and is driven by the user picking rows from
that page.  These 684 rows are not in the ledger at all (they were written
before the ledger recorded this failure shape), so there is nothing to click;
the candidate set has to be derived from the status itself.  Keeping the two
apart also means the ledger's "failed N times → stop trying" rule still applies
only to genuine download failures, while this sweep is bounded by its own
``limit``/cursor.

**How it avoids becoming a nightly pile of retries.**  A row that fails to come
back is **remembered** in ``failed_novel`` (via the normal ingest path), and a
subsequent run derives its candidates from ``has_epub = 4`` *minus* the ledger's
skip set — so a permanently-deleted work costs one attempt, not one per night.
``force_recent_days`` additionally stops a row from being retried again within
the window, which keeps a flapping network from turning the sweep into a loop.

**Does a successful re-download fix the status?**  Yes, through the ordinary
path, and this is what makes the task worth running: ``novel_fetch(redownload=
True)`` rewrites the body, so the draft carries a freshly detected
``has_epub`` (``PENDING`` when the body has placeholders, ``NO_IMAGES`` when it
has none), and that value is in ``_upsert_rows``' overwrite whitelist — it
replaces the terminal ``4``.  The producer then settles it to ``DONE`` in the
same transaction when the assets land.

**Dry-run is the default.**  Like the two prune tasks: the first run should be a
report ("684 本可重抓"), because a re-fetch is 684 webview calls against a
rate-limited API and the operator may prefer to do it in slices.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select as _select

from copixiv.core.models import EpubStatus, TaskResult
from copixiv.db import models
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.log import logger

from ..kernel import TaskContext
from ..kernel import register


class RefetchMissingSourceArgs(BaseModel):
    """Parameters for ``refetch_missing_source``.

    ``apply=False`` reports the candidate set without touching the network or
    the database.  ``limit`` keeps a single run inside the task timeout — the
    measure is "novels", not requests, and each novel is one webview call plus
    its assets.
    """

    apply: bool = Field(
        default=False,
        description="真正重抓。默认 false = 只统计候选（不发请求、不写库）",
    )
    limit: int | None = Field(
        default=None, ge=1,
        description="本轮最多重抓多少本（None = 全部候选）",
    )
    force_recent_days: int = Field(
        default=0, ge=0,
        description=(
            "跳过最近 N 天内已经失败过的行（0 = 不跳过）。用于避免网络抖动时"
            "同一批行每轮重复烧请求"
        ),
    )


def _count_missing(session) -> int:
    """How many rows still carry the terminal ``MISSING_SOURCE`` status."""
    from sqlalchemy import func

    return int(session.execute(
        _select(func.count()).select_from(models.Novel).where(
            models.Novel.has_epub == EpubStatus.MISSING_SOURCE,
            models.Novel.id > 0,
        )
    ).scalar() or 0)


def _candidates(
    session, *, limit: int | None, skip_ids: set[int],
) -> list[int]:
    """Ids whose body file is gone and which are not already known-failed."""
    stmt = _select(models.Novel.id).where(
        models.Novel.has_epub == EpubStatus.MISSING_SOURCE,
        models.Novel.id > 0,          # local books have no pixiv id to fetch
    ).order_by(models.Novel.id)
    stmt = stmt.limit(limit) if limit is not None else stmt
    return [
        int(nid) for nid in session.execute(stmt).scalars().all()
        if int(nid) not in skip_ids
    ]


@register(
    "refetch_missing_source",
    description="重抓正文文件已丢失的小说（has_epub=4；默认只统计不抓）",
    args=RefetchMissingSourceArgs,
)
async def refetch_missing_source(
    args: RefetchMissingSourceArgs, ctx: TaskContext,
) -> TaskResult:
    """Re-download novels whose local text file is gone.

    The candidate set is derived from the status itself, not from a list the
    user maintains; see the module docstring for why that is a separate task
    from ``failed_retry``.
    """
    # Imported here: ``tasks/novels.py`` imports this package indirectly, so a
    # module-level import would be circular.
    from ..novels import NovelFetchArgs, novel_fetch

    if ctx.client is None or ctx.file_storage is None:
        return TaskResult(
            summary="重抓丢失正文: 缺少 pixiv 客户端或文件存储，跳过"
        )

    with ctx.session_factory() as session:
        total = _count_missing(session)

        # Two independent reasons to leave a row out, unioned because either
        # alone is enough:
        #   1. the ledger has given up on it (failed_times >= _MAX_RETRIES) —
        #      retrying a permanently-dead work every night is exactly the
        #      waste this task must not introduce, and it is the same rule
        #      ``_plan_batch`` applies to ordinary downloads;
        #   2. it failed recently and ``force_recent_days`` is set — protects
        #      against one bad network night turning the sweep into a loop.
        skip_ids: set[int] = set()
        if args.force_recent_days:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=args.force_recent_days
            )
            skip_ids |= {
                int(nid) for nid in session.execute(
                    _select(models.FailedNovel.novel_id).where(
                        models.FailedNovel.last_failed_at.isnot(None),
                        models.FailedNovel.last_failed_at >= cutoff,
                    )
                ).scalars().all()
            }
        # ``get_skip_ids`` needs the id universe up front (it filters a given
        # set), so read the candidate page first and subtract afterwards.
        candidates = _candidates(session, limit=args.limit, skip_ids=skip_ids)
        if candidates:
            skip_ids |= FailedNovelRepository(session).get_skip_ids(
                set(candidates)
            )
            candidates = [nid for nid in candidates if nid not in skip_ids]

    if not candidates:
        return TaskResult(
            summary=(
                f"重抓丢失正文: 没有可抓的候选"
                f"（has_epub=4 共 {total} 本，均已放弃或已跳过）"
            )
        )

    if not args.apply:
        logger.info(
            "refetch_missing_source: 试运行，候选 {} 本（加 apply=true 才抓）",
            len(candidates),
        )
        return TaskResult(
            summary=(
                f"重抓丢失正文（试运行）: has_epub=4 共 {total} 本，"
                f"本轮候选 {len(candidates)} 本；未发起任何请求"
                f"（设 apply=true 执行）"
            )
        )

    results = await asyncio.gather(
        *[
            novel_fetch(
                NovelFetchArgs(id=nid, redownload=True),
                ctx.with_uow(ctx.child_uow()),
            )
            for nid in candidates
        ],
        return_exceptions=True,
    )

    ok_ids: list[int] = []
    failed_ids: list[int] = []
    for nid, result in zip(candidates, results):
        if isinstance(result, TaskResult) and not result.failed_ids:
            ok_ids.append(nid)
        else:
            failed_ids.append(nid)
            logger.error(f"refetch_missing_source: #{nid} 重抓失败: {result!r}")

    # The ordinary ingest path already records failures in the ledger; the
    # count here is what the operator reads.
    with ctx.session_factory() as session:
        remaining = _count_missing(session)

    logger.info(
        "refetch_missing_source: 成功 {} / {} 本，仍为 has_epub=4 的 {} 本",
        len(ok_ids), len(candidates), remaining,
    )
    return TaskResult(
        summary=(
            f"重抓丢失正文: 成功 {len(ok_ids)}/{len(candidates)} 本，"
            f"失败 {len(failed_ids)} 本；仍有 {remaining} 本缺正文"
        ),
        failed_ids=failed_ids,
    )


__all__ = [
    "RefetchMissingSourceArgs",
    "refetch_missing_source",
]
