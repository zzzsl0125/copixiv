"""演示:批量下载编排 —— pipeline._batch_handle 的三阶段

上一课我们看了单篇旅程(DownloadNovelUseCase.execute)。
批量场景里 pipeline 没有直接复用那个 UseCase,而是把它的流程拆开重排:

  单篇 UseCase:  取数 → 解析 → 存正文 → 图+EPUB → 【落库】
  批量 pipeline: 取数 → 解析 → 存正文 → 图+EPUB → 攒着 → 【统一落库】

为什么?因为批量落库要放进全局写锁(db_write)+ 一个事务里,
下载阶段绝不能碰数据库 —— 网络 IO 期间持锁会把所有任务卡死。

本脚本真实调用 src/copixiv/tasks/pipeline.py 的 _batch_handle,
四篇小说(其中一篇下载失败),跑两次,观察三阶段。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Novel
from copixiv.infrastructure.storage.file_storage import FileStorage
from copixiv.tasks.pipeline import _batch_handle

DOWNLOAD_DIR = "scratch/demo_batch_download"


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


# ---------------------------------------------------------------------------
# 替身们
# ---------------------------------------------------------------------------

class FakePixivClient:
    """每篇 webview 耗时 0.15s(模拟网络),2003 返回 None(模拟下载失败)。"""

    async def webview_novel(self, novel_id: int):
        await asyncio.sleep(0.15)
        if novel_id == 2003:
            return None
        return SimpleNamespace(
            id=novel_id,
            title=f"批量小说{novel_id}",
            user_id=3001,
            rating=SimpleNamespace(bookmark=10 + novel_id, view=1000),
            text=f"这是 {novel_id} 的正文,雨夜的猫……",
            caption="",
            series_id=None,
            series_title=None,
            series_navigation=None,
            cdate="2024-01-01 00:00:00",
            tags=["猫", "批量"],
            images={},          # 不带图,简化
            illusts={},
            cover_url=None,
        )


class FakeImageDownloader:
    """只记录被调用的小说 ID,不真下载图片。"""

    def __init__(self):
        self.calls: list[int] = []

    async def process_novel_assets(self, data: dict, force: bool = False):
        self.calls.append(data["id"])

    async def await_all(self) -> list[tuple[int, str]]:
        return []

    def shutdown(self) -> None:
        pass


def make_novels() -> list[SimpleNamespace]:
    """四篇小说,伪装成 pixivpy3 的 Novel 列表。

    plan 阶段只用到 .id;对「已在库」的小说还要调 build_from_novel_info(),
    所以 novelInfo 风格的字段(user/series/total_bookmarks/...)也得有。
    """
    return [
        SimpleNamespace(
            id=nid,
            title=f"批量小说{nid}",
            caption="",
            tags=["猫"],
            user=SimpleNamespace(id=3001, name="作者甲"),
            series=SimpleNamespace(id=None, title=None, index=None),
            total_bookmarks=10, total_view=1000, text_length=20,
            create_date="2024-01-01",
        )
        for nid in (2001, 2002, 2003, 2004)
    ]


def make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return create_session_factory(engine)


# ---------------------------------------------------------------------------
# 跑一轮 _batch_handle,并汇报战果
# ---------------------------------------------------------------------------

async def run_round(sf, label: str, client, storage, images) -> None:
    print(f"\n--- {label} ---")
    t0 = time.perf_counter()
    titles, new_author_ids = await _batch_handle(
        make_novels(),
        sf,
        client=client,
        file_storage=storage,
        image_downloader=images,
    )
    elapsed = time.perf_counter() - t0

    with sf() as s:
        rows = s.query(Novel).order_by(Novel.id).all()
        in_db = {r.id for r in rows}
        print(f"耗时 {elapsed:.2f}s(4 篇 × 0.15s 网络延迟,并发下载)")
        print(f"返回: 新标题={titles}")
        print(f"       new_author_ids={new_author_ids}")
        print(f"库里: {sorted(in_db)}")
        print(f"本次实际下载(调了 process_novel_assets): {sorted(images.calls)}")
        print(f"磁盘 txt: {sorted(p.name for p in Path(DOWNLOAD_DIR).rglob('*.txt'))}")


async def main() -> None:
    sf = make_session_factory()
    client = FakePixivClient()
    storage = FileStorage(DOWNLOAD_DIR)
    images = FakeImageDownloader()

    sep("第 1 幕:第一次运行 —— 四篇全要下载")
    await run_round(sf, "第一次 _batch_handle", client, storage, images)

    sep("第 2 幕:第二次运行 —— 增量更新")
    images.calls.clear()
    await run_round(sf, "第二次 _batch_handle", client, storage, images)

    sep("为什么第二次只下 1 篇?_plan_batch 的减法")
    print("""
  ids              = {2001, 2002, 2003, 2004}        # 任务给的候选
  existing         = {2001, 2002, 2004}              # 库里已存在的
  need_download    = ids - existing                  # 第一次:全部
                                                     # 第二次:只剩 2003
  2003 每次都失败 → 下次还会再试(有 failed_repo 时会按失败次数跳过)
""")

    sep("对比:单篇 UseCase vs 批量 pipeline(落库位置)")
    print("""
DownloadNovelUseCase.execute()          _fetch_one()(pipeline 内)
─────────────────────────────          ─────────────────────────
webview_novel → 解析                    webview_novel → 解析
save_novel_text → 图片/EPUB             save_novel_text → 图片/EPUB
novel_repo.upsert_novels(立即落库)   →   返回 data(不落库!)
                                          ↓ 攒着,等全部下完
                                          _persist_batch(锁内统一落库)

单篇:落库无所谓(就一篇,事务一闪而过)
批量:每篇都落库 = 下载循环里反复抢全局写锁 → 改成集中落库,
     锁只包住 persist 阶段,网络 IO 完全不占锁。
""")


if __name__ == "__main__":
    asyncio.run(main())
