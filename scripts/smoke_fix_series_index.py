#!/usr/bin/env python3
"""生产小范围实跑 + 对账：`fix_series_index`（章节号 / 系列 id 修复）。

为什么需要这个脚本：任务本身要打 pixiv（网络 + 账号池 + 限流），跑全量前必须
先确认"新代码在真实数据上真的只改该改的行"。跑一批、印一批对账，就是这里的用法：

    # 只跑 1 个缺号系列 + 1 本孤儿，然后打印受影响系列的前几行
    .venv/bin/python scripts/smoke_fix_series_index.py --max-series 1 --max-orphans 1

    # 指定要看对账的系列（可重复），跑完打印该系列每一行的 号 / 名册位置
    .venv/bin/python scripts/smoke_fix_series_index.py --max-series 1 --series 16242552

    # 只对账、不跑任务（纯只读，看某一个系列现在长什么样）
    .venv/bin/python scripts/smoke_fix_series_index.py --dry-run --series 16242552

注意：
* 这个脚本直接调任务函数（绕过任务内核），因此**不会**写 task_history、也不会走
  定时任务的去重；它用与线上完全相同的 client / 账号池 / 读写事务。
* 它复用任务的三个游标（``setting`` 表里的 ``fix_series_index.*``），所以跑过的
  进度会被线上任务继承——这正是"分批续跑"的语义，不是污染。
* 生产后端进程用的是启动那一刻的代码：改了 ``tasks/maintenance.py`` 后，网页上点
  「修复章节」跑的还是旧代码，需要重启后端才生效。这个脚本不受影响。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from copixiv.app import _build  # noqa: E402
from copixiv.tasks.kernel import TaskContext  # noqa: E402
from copixiv.tasks.maintenance import (  # noqa: E402
    FixSeriesIndexArgs,
    fix_series_index,
)
from copixiv.db.uow import SqlUnitOfWork  # noqa: E402


def _engine():
    """只读对账用的引擎：直接读 config.yaml 的 database_url。"""
    from copixiv.config import load_config

    cfg = load_config()
    url = getattr(cfg, "database_url", None) or cfg.path.database_url
    return create_engine(url)


def report(engine, series_ids: list[int], limit: int = 6) -> None:
    """打印受影响系列的现状：每行的 库内号 / 该系列 id 升序位置。"""
    with engine.connect() as conn:
        for sid in series_ids:
            rows = conn.execute(
                text(
                    "select id, series_index, series_name,"
                    " row_number() over (order by id) as id_pos"
                    " from novel where series_id = :sid order by id"
                ),
                {"sid": sid},
            ).all()
            missing = sum(1 for r in rows if r[1] is None)
            print(
                f"  系列 {sid}: 库内 {len(rows)} 本，缺号 {missing} 本",
                flush=True,
            )
            for r in rows[:limit]:
                print(f"     {r[0]} 号={r[1]} (id序={r[3]}) {str(r[2])[:20]}", flush=True)
            if len(rows) > limit:
                print(f"     … 其余 {len(rows) - limit} 本省略", flush=True)
        cursors = conn.execute(
            text(
                "select key, value from setting"
                " where key like 'fix_series_index%' order by key"
            )
        ).all()
    print(f"  游标: {cursors or '（空）'}", flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-series", type=int, default=1, help="本轮最多处理几个缺号系列")
    ap.add_argument("--max-orphans", type=int, default=1, help="本轮最多处理几本孤儿号")
    ap.add_argument(
        "--sweep-all", action="store_true",
        help="打开全量复查（号齐全的系列也拉名册核对；请求量大）",
    )
    ap.add_argument(
        "--series", type=int, action="append", default=[],
        help="额外对账的 series_id（可重复；只读展示）",
    )
    ap.add_argument("--dry-run", action="store_true", help="不跑任务，只对账")
    args = ap.parse_args()

    print("装配容器（读 config.yaml / 连库 / 载入账号池）…", flush=True)
    singletons = _build()
    ctx = TaskContext(
        uow=SqlUnitOfWork(singletons.session_factory),
        session_factory=singletons.session_factory,
        client=singletons.client,
        file_storage=singletons.file_storage,
        image_downloader=singletons.image_downloader,
        epub_builder=singletons.epub_builder,
        config=singletons.config,
    )

    engine = _engine()
    if args.series:
        print("对账（跑之前）:", flush=True)
        report(engine, args.series)

    if args.dry_run:
        print("--dry-run：未执行任务。", flush=True)
        return 0

    result = await fix_series_index(
        FixSeriesIndexArgs(
            max_series=args.max_series,
            max_orphans=args.max_orphans,
            sweep_all=args.sweep_all,
        ),
        ctx,
    )
    print(f"\n任务摘要: {result.summary}", flush=True)

    if args.series:
        print("对账（跑之后）:", flush=True)
        report(engine, args.series)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
