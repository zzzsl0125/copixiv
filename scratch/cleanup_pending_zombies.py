"""一次性清理:PENDING 僵尸小说批量降级。

依赖 check_epub 的两条降级规则(见 src/copixiv/tasks/maintenance.py):
  1. 正文已无图片占位符 → 降级 NO
  2. 有占位符但从未下载到图片文件,且最后尝试(txt mtime)超过 7 天 → 降级 NO

覆盖对象:作者已删除/图片已失效、EPUB 永远做不出来的僵尸 PENDING。

用法:
    .venv/bin/python scratch/cleanup_pending_zombies.py --dry-run  # 只预览名单
    .venv/bin/python scratch/cleanup_pending_zombies.py            # 执行
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path

from sqlalchemy import select

from copixiv.infrastructure.database.engine import (
    create_database_engine, create_session_factory,
)
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.tasks.maintenance import _STALE_DAYS, check_epub

DB_PATH = "database/database.db"
_PLACEHOLDER = re.compile(r"\[(uploadedimage|pixivimage):([\d\-]+)\]")


def _would_downgrade(novel_id: int, path_str: str | None) -> bool:
    """与 check_epub 降级规则一致(仅文件判断,不碰数据库)。"""
    if not path_str:
        return False
    txt = Path(path_str)
    if not txt.exists():
        return False
    if txt.with_suffix(".epub").exists():
        return False  # epub 在 → completed,不是降级对象
    text = txt.read_text(encoding="utf-8", errors="ignore")
    if not _PLACEHOLDER.search(text):
        return True  # 规则 1:正文无图
    # 规则 2:有占位符 + 无图文件 + txt 超期
    parent = txt.parent
    if any(parent.glob(f"{novel_id}_u_*")) or any(parent.glob(f"{novel_id}_p_*")):
        return False
    try:
        age_days = (time.time() - txt.stat().st_mtime) / 86400
    except OSError:
        return False
    return age_days > _STALE_DAYS


async def main() -> None:
    parser = argparse.ArgumentParser(description="PENDING 僵尸降级")
    parser.add_argument("--dry-run", action="store_true", help="只预览将降级的名单")
    args = parser.parse_args()

    engine = create_database_engine(DB_PATH)
    sf = create_session_factory(engine)

    from copixiv.infrastructure.database.models import Novel

    with sf() as session:
        rows = session.execute(
            select(Novel.id, Novel.path).where(Novel.has_epub == 1)
        ).all()
    candidates = sorted(nid for nid, p in rows if _would_downgrade(nid, p))
    print(f"当前 PENDING 共 {len(rows)} 篇,符合降级规则 {len(candidates)} 篇")

    if args.dry_run:
        print(f"将降级: {candidates}")
        return

    # 直接跑一次巡检:内部会用同样的规则降级 + 完成/回退
    result = await check_epub(uow=SqlUnitOfWork(sf))
    print(f"巡检 → {result.summary}")

    with sf() as session:
        remaining = session.execute(
            select(Novel.id).where(Novel.has_epub == 1)
        ).scalars().all()
    print(f"剩余 PENDING: {len(remaining)} 篇 {sorted(remaining)}")
    engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
