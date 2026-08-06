"""演示:check_epub 日志的 completed / pending 到底在数什么

用真实 check_epub 代码 + 内存库 + 假文件,模拟两天运转:

  第 1 天:入库 10 篇带图小说(全部 has_epub=PENDING)
          → 后台生成 EPUB,8 篇成功,2 篇失败
          → 巡检:completed=8, pending=2
  第 2 天:入库 10 篇,9 篇成功 1 篇失败
          昨天失败的 2 篇里有 1 篇重试成功(文件补齐)
          → 巡检:completed=? pending=?

关键问题:pending 是「存量」还是「今日增量」?
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Author, Novel
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.tasks.maintenance import check_epub

CHECK_DIR = Path("scratch/demo_epub_check")


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


def make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return create_session_factory(engine)


async def seed_novels(sf, novel_ids: list[int]) -> None:
    """入库一批带图小说(has_epub=1/PENDING),path 指向假目录。"""
    uow = SqlUnitOfWork(sf)
    async with uow.begin():
        if uow.session.get(Author, 3001) is None:
            uow.session.add(Author(author_id=3001, author_name="作者甲"))
        for nid in novel_ids:
            uow.session.add(Novel(
                id=nid, title=f"小说{nid}", author_id=3001,
                path=str(CHECK_DIR / f"{nid}" / f"novel{nid}.txt"),
                has_epub=1,   # 带图 → PENDING
            ))


def simulate_background_epub(success_ids: list[int]) -> None:
    """模拟后台线程池:给成功的篇目生成 EPUB 文件。"""
    for nid in success_ids:
        d = CHECK_DIR / str(nid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"novel{nid}.epub").write_text("fake epub")


async def run_check(sf, label: str) -> None:
    result = await check_epub(uow=SqlUnitOfWork(sf))
    with sf() as s:
        rows = s.query(Novel).order_by(Novel.id).all()
        done = [r.id for r in rows if r.has_epub == 2]
        pending = [r.id for r in rows if r.has_epub == 1]
    print(f"{label} → {result.summary}")
    print(f"   巡检后 DB 状态: DONE={done}")
    print(f"                     PENDING={pending}")


async def main() -> None:
    sf = make_session_factory()

    sep("第 1 天:入库 10 篇,后台生成成功 8 篇,失败 2 篇")
    await seed_novels(sf, list(range(1, 11)))
    simulate_background_epub(success_ids=list(range(1, 9)))   # 1-8 成功,9/10 失败
    await run_check(sf, "第 1 天巡检")

    sep("第 2 天:新入库 10 篇(9 成功 1 失败);昨天失败的 9 号重试成功")
    await seed_novels(sf, list(range(11, 21)))
    simulate_background_epub(success_ids=list(range(11, 20)))  # 11-19 成功,20 失败
    simulate_background_epub(success_ids=[9])                  # 昨天的 9 号补上了
    await run_check(sf, "第 2 天巡检")

    sep("结论")
    print("""
  第 2 天日志会是: completed=10(9 篇今日 + 1 篇昨日补上), pending=2
  而「今日新入库」其实是 10 篇 —— 两者对不上!

  completed  ≠ 今日新增  (含昨天失败今天补上的)
  pending    ≠ 今日失败  (是「全库存量」:今天 1 篇 + 昨天 1 篇的累积)

  pending 是存量不是增量:只要某篇小说一直没生成成功,
  它每天都出现在 pending 里,直到文件就绪或被人处理。
""")


if __name__ == "__main__":
    asyncio.run(main())
