"""``backup_database`` — the weekly ``pg_dump``, as a scheduled task.

**Why this is a task and not startup work.**  The backup used to run inside
``create_app()``: the first startup of each ISO week shelled out to
``pg_dump`` before the app was even listening.  On this host that is a 590 MB
read plus a ~71 MB write — a real I/O spike, and one that lands at an
arbitrary time (whenever the unit happens to restart) instead of a quiet
window.  As a task it gets the same schedule machinery as every other
maintenance job: a cron row, history, progress, and a Telegram notification.

The startup fallback in :mod:`copixiv.app` stays for installs that have not
scheduled this task yet, and switches itself off as soon as an enabled
``backup_database`` row exists (``_has_enabled_backup_schedule``).

**Format and naming are owned by** :mod:`copixiv.db.backup` — custom-format
``pg_dump``, one file per ISO week (``2026-W45.dump``), retention by
``keep_count``.  This module only adapts it to the task contract: resolve the
live database URL from the session's engine, run the blocking dump off the
event loop, then prune.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from copixiv.core.models import TaskResult
from copixiv.db.backup import (
    backup_database as _dump_database,
)
from copixiv.db.backup import cleanup_old_backups

from ..kernel import TaskContext, register


class BackupDatabaseArgs(BaseModel):
    """Parameters for ``backup_database``."""

    keep_count: int = Field(
        default=4, ge=1, le=52,
        description="保留最近多少份周备份（默认 4，约一个月）",
    )


def _database_url(ctx: TaskContext) -> str:
    """Resolve the live database URL from the task's session factory.

    Read from the engine the app already uses rather than re-reading
    ``config.yaml``: the two cannot drift, and the task keeps working when it
    is run from a script against an alternate config.
    """
    if ctx.session_factory is None:
        raise RuntimeError("backup_database: session factory is not configured")
    with ctx.session_factory() as session:
        bind = session.get_bind()
        if bind is None or bind.url is None:
            raise RuntimeError("backup_database: session has no bound engine")
        return str(bind.url)


@register(
    "backup_database",
    description="备份数据库（pg_dump 自定义格式，按 ISO 周命名）",
    args=BackupDatabaseArgs,
)
async def backup_database(
    args: BackupDatabaseArgs, ctx: TaskContext,
) -> TaskResult:
    """Run one ``pg_dump`` and prune older weekly backups."""
    url = _database_url(ctx)
    # ``backup.dir`` from config (default ``backups``) — configurable so a
    # test app can never write a dump into the production directory.
    backup_dir = ctx.backup_dir

    # Both halves block on a subprocess / filesystem; keep them off the loop.
    dest = await asyncio.to_thread(_dump_database, url, backup_dir)
    removed = await asyncio.to_thread(
        cleanup_old_backups, url, args.keep_count, backup_dir,
    )

    size = Path(dest).stat().st_size
    size_mb = size / (1024 * 1024)
    summary = f"数据库备份完成: {dest}（{size_mb:.1f} MiB）"
    if removed:
        summary += f"，清理旧备份 {len(removed)} 份"
    return TaskResult(summary=summary)
