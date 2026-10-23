"""``prune_task_history`` — trim the task-history table, dry-run by default.

**Why it needs a task at all.**  ``task_history`` is simultaneously the audit
trail *and* the task queue (the worker polls ``status = 'pending' ORDER BY id``).
It only grows: nothing deletes from it anywhere in the codebase.  Measured on
production (2026-10-24): 1,566 rows holding **13 MB** of ``result``, the largest
single row 525 kB, with one task's stored log at 1.26 M characters.

The queue read is cheap today only *because* the table is small.  Growth is
therefore not merely "disk"; it is the one query whose cost tracks total history
size, which is why migration 0014 added the partial indexes and why trimming
matters.

**Two rules, not one.**  Age alone would delete a quiet week's history while
keeping a busy day's; count alone would delete the only record of what happened
last month during a burst.  So a row survives if **either** holds:

* it is among the newest ``keep`` rows (default 2,000), **or**
* it is younger than ``max_age_days`` (default 180).

Rows that fail both are deleted.  Both thresholds are read from the
``setting`` row ``task_history.retention`` (JSON), so they can be tuned without
a migration; the args model overrides them for a single run.

**Dry-run is the default**, like ``prune_orphan_files``: history is audit
material, and "silently deleted the evidence" is a worse failure than "kept
a few extra rows".  Nothing here is scheduled automatically — enabling it is an
explicit operator decision, and the first enabled run should be a dry run.

**Pending/running rows are never deleted**, whatever the thresholds say: they
are live queue entries, not history.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import delete as _delete_stmt
from sqlalchemy import func, select as _select

from copixiv.core.models import TaskResult
from copixiv.db import models
from copixiv.db.write_lock import run_write_transaction
from copixiv.log import logger

from ..kernel import TaskContext
from ..kernel import register


#: ``setting`` row holding the retention policy (JSON object).
RETENTION_SETTING_KEY = "task_history.retention"

#: Defaults, also written into the setting row so the UI/ops can see them.
DEFAULT_KEEP = 2_000
DEFAULT_MAX_AGE_DAYS = 180


class PruneTaskHistoryArgs(BaseModel):
    """Parameters for ``prune_task_history``.

    All four are optional overrides for one run; ``None`` means "use the
    ``task_history.retention`` setting, else the module default".  ``apply``
    defaults to false for the same reason as ``prune_orphan_files``: this
    deletes audit rows.
    """

    apply: bool = Field(
        default=False,
        description="真正删除。默认 false = 只统计（试运行）",
    )
    keep: int | None = Field(
        default=None, ge=0,
        description="至少保留最近 N 行（不论多老）",
    )
    max_age_days: int | None = Field(
        default=None, ge=1,
        description="同时保留最近 N 天内的行（不论多少）",
    )


def _policy(args: PruneTaskHistoryArgs, stored: dict | None) -> tuple[int, int]:
    """Resolve ``(keep, max_age_days)``: args → setting row → module default."""
    stored = stored or {}
    keep = args.keep if args.keep is not None else int(
        stored.get("keep", DEFAULT_KEEP)
    )
    age = args.max_age_days if args.max_age_days is not None else int(
        stored.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    )
    return keep, age


def _read_policy(ctx: TaskContext) -> dict | None:
    """The stored policy JSON, or ``None`` when the row is absent/unparseable."""
    with ctx.session_factory() as session:
        raw = session.execute(
            _select(models.Setting.value).where(
                models.Setting.key == RETENTION_SETTING_KEY
            )
        ).scalar()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "{} 不是合法 JSON，按默认保留策略处理：{!r}",
            RETENTION_SETTING_KEY, raw,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _keep_boundary(session, keep: int) -> int | None:
    """The id below which a row falls outside "keep the newest *keep* rows".

    Ids are monotonic in insertion order — the same order the queue uses — so
    "the newest N" is "id >= the Nth newest id".

    Returns ``None`` when the table holds fewer than *keep* rows.  That case is
    **not** "no protection": "keep the newest 2 000" is a promise that a small
    history is left alone, so a short table yields no victims at all (see
    :func:`_victim_condition`).  Reading ``None`` as "delete everything old"
    would empty a quiet instance the first time retention ran.
    """
    if keep <= 0:
        return None
    return session.execute(
        _select(models.TaskHistory.id)
        .order_by(models.TaskHistory.id.desc())
        .offset(keep - 1)
        .limit(1)
    ).scalar()


def _victim_condition(session, keep: int, cutoff: datetime) -> list:
    """The ``WHERE`` shared by the count and the delete — one owner.

    A row is a victim only when it satisfies **all** of: not part of the live
    queue, older than *cutoff*, and below the keep boundary.  When the table is
    shorter than *keep*, the boundary is ``None`` and **nothing** is a victim.
    """
    boundary = _keep_boundary(session, keep)
    if boundary is None:
        # Fewer rows than the floor: the count rule alone protects the table.
        return [models.TaskHistory.id.is_(None)]   # matches nothing

    return [
        models.TaskHistory.status.not_in(("pending", "running")),
        models.TaskHistory.start_time < cutoff,
        models.TaskHistory.id < boundary,
    ]


def _count_victims(session, keep: int, cutoff: datetime) -> int:
    """How many rows the two retention rules agree on deleting."""
    stmt = _select(func.count()).select_from(models.TaskHistory).where(
        *_victim_condition(session, keep, cutoff)
    )
    return int(session.execute(stmt).scalar() or 0)


@register(
    "prune_task_history",
    description="按「保留最近 N 行」+「保留最近 N 天」两条规则裁剪任务历史（默认只统计）",
    args=PruneTaskHistoryArgs,
)
async def prune_task_history(
    args: PruneTaskHistoryArgs, ctx: TaskContext,
) -> TaskResult:
    """Trim ``task_history``: delete rows that are both old *and* beyond the top N.

    Pending/running rows are never candidates — they are the live queue.
    """
    keep, max_age_days = _policy(args, _read_policy(ctx))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    with ctx.session_factory() as session:
        total = int(session.execute(
            _select(func.count()).select_from(models.TaskHistory)
        ).scalar() or 0)
        victims = _count_victims(session, keep, cutoff)

    threshold = (
        f"保留最近 {keep} 行 + 最近 {max_age_days} 天内的记录"
    )
    if victims == 0:
        return TaskResult(
            summary=(
                f"任务历史清理: 无需裁剪（共 {total} 行；{threshold}，"
                f"两条规则都不满足的行才删）"
            )
        )

    if not args.apply:
        logger.info(
            "prune_task_history: 试运行，可删除 {} / {} 行（加 apply=true 才真正删除）",
            victims, total,
        )
        return TaskResult(
            summary=(
                f"任务历史清理（试运行）: 共 {total} 行，可删除 {victims} 行"
                f"（{threshold}）；未删除任何行（设 apply=true 执行）"
            )
        )

    async def _delete(uw) -> int:
        session = uw.session
        conditions = _victim_condition(session, keep, cutoff)
        result = session.execute(
            _delete_stmt(models.TaskHistory).where(*conditions)
        )
        return int(result.rowcount or 0)

    deleted = await run_write_transaction(ctx.uow, _delete)
    logger.info(
        "prune_task_history: 删除 {}/{} 行（{}）", deleted, total, threshold,
    )
    return TaskResult(
        summary=(
            f"任务历史清理: 删除 {deleted} 行，剩余 {total - deleted} 行"
            f"（{threshold}）"
        )
    )


__all__ = [
    "DEFAULT_KEEP",
    "DEFAULT_MAX_AGE_DAYS",
    "RETENTION_SETTING_KEY",
    "PruneTaskHistoryArgs",
    "prune_task_history",
]
