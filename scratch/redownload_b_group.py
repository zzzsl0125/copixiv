"""一次性脚本:B 组小说重新抓取 —— 正文有图但从未生成 EPUB 的篇目。

默认自动挑选 B 组:has_epub=1 且无 failed_novel 记录且正文含图片占位符。
逐篇走真实 novel_fetch 任务(redownload=True):
    - 成功 → 重新下载正文/图片/EPUB,has_epub 随新正文刷新
    - 失败 → 原因写入 failed_novel(不再被吞)
全部跑完后自动执行一次 check_epub 巡检收尾(降级/完成统计)。

用法:
    .venv/bin/python scratch/redownload_b_group.py            # 自动挑选 B 组并执行
    .venv/bin/python scratch/redownload_b_group.py --ids 1,2  # 指定 id
    .venv/bin/python scratch/redownload_b_group.py --dry-run  # 只打印将处理哪些
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from sqlalchemy import select

from copixiv.app.config import get_config
from copixiv.infrastructure.database.engine import (
    create_database_engine, create_session_factory,
)
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.epub.builder import EpubBuilder
from copixiv.infrastructure.pixiv.account import PixivAccount, TokenInfo
from copixiv.infrastructure.pixiv.accounts import AccountPool
from copixiv.infrastructure.pixiv.client import PixivClient
from copixiv.infrastructure.pixiv.patch import apply as apply_pixiv_patches
from copixiv.infrastructure.storage.file_storage import FileStorage
from copixiv.infrastructure.storage.image_downloader import ImageDownloader
from copixiv.tasks.maintenance import check_epub
from copixiv.tasks.novel_tasks import novel_fetch

DB_PATH = "database/database.db"
_PLACEHOLDER = re.compile(r"\[(uploadedimage|pixivimage):([\d\-]+)\]")


def pick_b_group(sf) -> list[int]:
    """自动挑选 B 组:has_epub=1、无失败记录、正文含图片占位符。"""
    from copixiv.infrastructure.database.models import FailedNovel, Novel

    with sf() as session:
        rows = session.execute(
            select(Novel.id, Novel.path)
            .outerjoin(FailedNovel, FailedNovel.novel_id == Novel.id)
            .where(Novel.has_epub == 1, FailedNovel.novel_id.is_(None))
        ).all()
    ids: list[int] = []
    for nid, path_str in rows:
        txt = Path(path_str) if path_str else None
        if txt and txt.exists():
            text = txt.read_text(encoding="utf-8", errors="ignore")
            if _PLACEHOLDER.search(text):
                ids.append(nid)
    return sorted(ids)


def build_client(sf) -> tuple[PixivClient, AccountPool]:
    """照抄 container.py 的构造:账号从 token 表加载。"""
    from copixiv.infrastructure.database.models import Token

    config = get_config()
    pool = AccountPool()
    with sf() as session:
        tokens = session.execute(
            select(Token).where(Token.valid == True)  # noqa: E712
        ).scalars().all()
        for t in tokens:
            pool.add_account(PixivAccount(
                token_info=TokenInfo(
                    token=t.token, username=t.name,
                    premium=t.premium, valid=t.valid,
                ),
                proxy_http=config.proxy.http,
                proxy_https=config.proxy.https,
                min_interval=config.pixiv_client.min_interval,
                cooling_duration=config.pixiv_client.cooling_duration,
            ))
    print(f"账号池: {len(tokens)} 个有效账号")
    client = PixivClient(
        account_pool=pool,
        max_concurrency=config.pixiv_client.max_concurrency,
        min_interval=config.pixiv_client.min_interval,
    )
    return client, pool


async def main() -> None:
    parser = argparse.ArgumentParser(description="B 组小说重新抓取")
    parser.add_argument("--ids", type=str, default=None, help="逗号分隔的 id 列表(默认自动挑选 B 组)")
    parser.add_argument("--dry-run", action="store_true", help="只打印将处理哪些,不执行")
    args = parser.parse_args()

    engine = create_database_engine(DB_PATH)
    sf = create_session_factory(engine)

    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
    else:
        ids = pick_b_group(sf)
    print(f"将处理 {len(ids)} 篇: {ids}")

    if args.dry_run:
        return

    config = get_config()
    # 照抄 container.build():必须先应用 pixivpy3 宽容补丁(否则
    # WebviewNovel 模型会把 illusts/images 当 list 校验,dict 直接炸)
    apply_pixiv_patches()
    client, pool = build_client(sf)
    storage = FileStorage(config.path.download)
    image_downloader = ImageDownloader(max_workers=4, epub_builder=EpubBuilder())

    try:
        for i, nid in enumerate(ids, 1):
            print(f"\n[{i}/{len(ids)}] #{nid} 重抓中 ……")
            result = await novel_fetch(
                id=nid,
                redownload=True,
                client=client,
                uow=SqlUnitOfWork(sf),
                file_storage=storage,
                image_downloader=image_downloader,
            )
            print(f"[{i}/{len(ids)}] #{nid} → {result.summary}")

        print("\n重抓完成,执行 check_epub 巡检收尾 ……")
        result = await check_epub(uow=SqlUnitOfWork(sf))
        print(f"巡检 → {result.summary}")

        # 最终统计
        from collections import Counter
        from copixiv.infrastructure.database.models import Novel
        with sf() as session:
            c = Counter(session.execute(
                select(Novel.has_epub).where(Novel.has_epub > 0)
            ).scalars().all())
        print(f"\n最终 has_epub 分布: PENDING={c.get(1, 0)}, DONE={c.get(2, 0)}")
    finally:
        image_downloader.shutdown()
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
