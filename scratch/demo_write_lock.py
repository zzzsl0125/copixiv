"""演示:db_write() 全局写锁 —— 为什么要锁,锁怎么工作

背景事实:SQLite 同一时刻只允许一个写者(即使 WAL 模式)。
两个写事务撞在一起,后到者就会得到 "database is locked"。

本脚本四幕:
  第 1 幕:复现 SQLite 的「单写者」铁律 —— 不加锁的并发写
  第 2 幕:asyncio.Lock —— 协程世界的排队
  第 3 幕:db_write() + uow.begin() 真实组合,验证串行化
  第 4 幕:一行两个上下文管理器 —— async with a, b: 的秘密
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

from contextlib import asynccontextmanager

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Author
from copixiv.infrastructure.database.uow import SqlUnitOfWork
from copixiv.infrastructure.database.write_lock import db_write


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


# ---------------------------------------------------------------------------
# 第 1 幕:复现 SQLite 的「单写者」铁律
# ---------------------------------------------------------------------------

def act1_locked() -> None:
    print("场景:两个连接同时写同一个库(没有锁)")

    for wal, label in ((False, "默认 journal 模式"), (True, "WAL 模式")):
        conn1 = sqlite3.connect("scratch/demo_lock.db", timeout=0.05,
                                 check_same_thread=False)
        conn2 = sqlite3.connect("scratch/demo_lock.db", timeout=0.05,
                                 check_same_thread=False)
        if wal:
            conn1.execute("PRAGMA journal_mode=WAL")
            conn2.execute("PRAGMA journal_mode=WAL")
        conn1.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        conn1.commit()

        def writer1() -> None:
            conn1.execute("BEGIN IMMEDIATE")      # 拿到写锁
            conn1.execute("INSERT INTO t VALUES (1)")
            time.sleep(0.3)                        # 慢慢写(模拟大事务)
            conn1.commit()                         # 写完才释放

        def writer2() -> None:
            time.sleep(0.05)                       # 晚一点开始
            try:
                conn2.execute("BEGIN IMMEDIATE")   # 写锁被占,等 50ms 后放弃
                conn2.execute("INSERT INTO t VALUES (2)")
                conn2.commit()
                print(f"   {label}: 第二个写者成功")
            except sqlite3.OperationalError as e:
                print(f"   {label}: 第二个写者 → {e}")

        t1 = threading.Thread(target=writer1)
        t2 = threading.Thread(target=writer2)
        t1.start(); t2.start(); t1.join(); t2.join()
        conn1.close(); conn2.close()

    print("→ 结论:无论哪种模式,写者永远只有一个,后到者只能等或报错。")


# ---------------------------------------------------------------------------
# 第 2 幕:asyncio.Lock —— 协程世界的排队
# ---------------------------------------------------------------------------

async def act2_async_lock() -> None:
    lock = asyncio.Lock()

    async def writer(name: str, work: float) -> None:
        async with lock:
            print(f"   {name}: 拿到锁,开始写 ……")
            await asyncio.sleep(work)      # 模拟写事务耗时
            print(f"   {name}: 写完,释放锁")
        print(f"   {name}: 锁外干别的(不影响别人)")

    await asyncio.gather(
        writer("写者A", 0.2),
        writer("写者B", 0.2),
        writer("写者C", 0.2),
    )
    print("→ asyncio.Lock:同一时刻只有一个协程能进临界区,其他协程排队等待。")
    print("  注意:它管的是『协程』之间,不是系统线程之间。")


# ---------------------------------------------------------------------------
# 第 3 幕:db_write() + uow.begin() 真实组合
# ---------------------------------------------------------------------------

def make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return create_session_factory(engine)


async def act3_real_combination() -> None:
    sf = make_session_factory()

    # 无锁版:两个任务各写各的,事务时间重叠
    async def persist_no_lock(n: int, work: float) -> None:
        uow = SqlUnitOfWork(sf)
        async with uow.begin():
            uow.session.add(Author(author_id=n, author_name=f"a{n}"))
            await asyncio.sleep(work)

    t0 = time.perf_counter()
    await asyncio.gather(
        persist_no_lock(1, 0.2), persist_no_lock(2, 0.2),
    )
    print(f"无锁版:两个任务总耗时 {time.perf_counter() - t0:.2f}s "
          f"(事务重叠,SQLite 层互相踩)")

    # 加锁版:pipeline.py 的真实写法 —— 锁包住整个写事务
    async def persist_with_lock(n: int, work: float) -> None:
        uow = SqlUnitOfWork(sf)
        async with db_write():          # 全局写锁
            async with uow.begin():     # 事务
                uow.session.add(Author(author_id=n, author_name=f"b{n}"))
                await asyncio.sleep(work)
        # 锁覆盖:写入 + commit 都在锁内(commit 在 begin() 退出时发生)

    t0 = time.perf_counter()
    await asyncio.gather(
        persist_with_lock(3, 0.2), persist_with_lock(4, 0.2),
    )
    print(f"加锁版:两个任务总耗时 {time.perf_counter() - t0:.2f}s "
          f"(严格排队 → 2 × 0.2s)")

    with sf() as s:
        names = [r[0] for r in s.query(Author.author_name).all()]
    print(f"最终库里: {sorted(names)}")

    print("→ 加锁版总耗时≈两倍,代价是串行;换来的是绝不撞出 locked。")


# ---------------------------------------------------------------------------
# 第 4 幕:一行两个上下文管理器 —— async with a, b: 的秘密
# ---------------------------------------------------------------------------

async def act4_multi_with() -> None:
    @asynccontextmanager
    async def ctx(name: str):
        print(f"   {name}: 进入")
        yield
        print(f"   {name}: 退出")

    print("嵌套写法(真实代码是两行):")
    async with ctx("锁"):
        async with ctx("事务"):
            print("   (业务代码)")

    print("\n一行写法:async with 锁, 事务: —— 完全等价")
    async with ctx("锁"), ctx("事务"):
        print("   (业务代码)")

    print("→ 规则:从左到右进入,从右到左退出(像洋葱,先剥最外层的皮)。")
    print("  所以 db_write() 包在最外面 → 事务先提交,锁才释放 —— 这正是")
    print("  write_lock.py 注释里的不变量:锁必须覆盖提交。")


async def main() -> None:
    sep("第 1 幕:SQLite 的「单写者」铁律")
    act1_locked()

    sep("第 2 幕:asyncio.Lock —— 协程排队")
    await act2_async_lock()

    sep("第 3 幕:db_write() + uow.begin() 真实组合")
    await act3_real_combination()

    sep("第 4 幕:一行两个上下文管理器")
    await act4_multi_with()

    sep("最后:pipeline 为什么把下载放在锁外面(src/copixiv/tasks/pipeline.py:282)")
    print("""
  阶段 1  Plan      只读,短事务,无锁      → 读不需要锁(WAL 支持并发读)
  阶段 2  Download  纯网络/文件 IO,不碰库  → 锁里放网络 = 其他任务全卡死
  阶段 3  Persist   一个写事务,包在锁里    → 写必须串行
""")


if __name__ == "__main__":
    asyncio.run(main())
