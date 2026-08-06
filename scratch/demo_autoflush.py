"""演示:autoflush 地雷 —— 「先写后读」为什么读不到自己的行

项目 session 配置是 autoflush=False(见 engine.py:create_session_factory)。
后果:session.add() 只是把对象放进「待写队列」,还没发 SQL;
下一次查询(SELECT)不会自动帮你把队列倒进数据库 —— 于是:

    add(排队) → 查询同表 → 读不到自己刚加的行,而且不报错。

本脚本四幕,把这个地雷引爆、拆解、再埋回去:
  第 1 幕:复现地雷 —— add 后立即查同表,返回 None
  第 2 幕:uow.flush() 拆雷 —— 强制把队列倒进数据库
  第 3 幕:autoflush=True 时同样的代码能读到 —— 对比隐式行为
  第 4 幕:可见性边界 —— flush ≠ commit:别人看不见,直到 commit
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Author
from copixiv.infrastructure.database.uow import SqlUnitOfWork


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


async def act1_mine() -> None:
    """复现地雷:add 后立即查同表,读不到。"""
    sf = make_session_factory()
    uow = SqlUnitOfWork(sf)
    async with uow.begin():
        uow.session.add(Author(author_id=1, author_name="排队中的猫"))
        # 此时数据库里什么都没有 —— add 只是把对象放进了内存队列

        row = uow.session.get(Author, 1)   # ← SELECT 已发出,但 INSERT 还没发
        print(f"add 之后立刻查同表 → {row}")
        print("没有报错,只是查不到 —— 这就是『地雷』:静默返回旧数据。")


async def act2_defuse() -> None:
    """uow.flush():把队列倒进数据库(事务内)。"""
    sf = make_session_factory()
    uow = SqlUnitOfWork(sf)
    async with uow.begin():
        uow.session.add(Author(author_id=2, author_name="flush后的猫"))
        await uow.flush()                  # ← 强制发 INSERT(还没 commit)
        row = uow.session.get(Author, 2)
        print(f"flush 之后查同表 → {row}")
    print("begin 正常退出 → commit 落盘。")


async def act3_autoflush_true() -> None:
    """对比:autoflush=True(默认)时,查询前会自动 flush。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(autocommit=False, autoflush=True, bind=engine)

    s = sf()
    s.add(Author(author_id=3, author_name="隐式flush的猫"))
    row = s.get(Author, 3)                 # 查询前 SQLAlchemy 偷偷 flush 了
    print(f"autoflush=True,add 后直接查同表 → {row}")
    s.rollback()                           # 不提交,清理
    s.close()


async def act4_boundary() -> None:
    """flush ≠ commit:同事务可见,别的 session 不可见,commit 后才全局可见。

    注意:这里用文件库而不是内存库+StaticPool —— StaticPool 让所有
    session 共享同一条连接,会把隔离边界抹掉,看不出效果。
    """
    engine = create_engine("sqlite:///scratch/demo_flush_boundary.db")
    Base.metadata.create_all(bind=engine)
    sf = create_session_factory(engine)

    uow = SqlUnitOfWork(sf)
    async with uow.begin():
        uow.session.add(Author(author_id=4, author_name="可见性实验"))
        await uow.flush()

        same = uow.session.get(Author, 4) is not None
        with sf() as other:               # 独立连接 + 独立事务
            other_sees = other.get(Author, 4) is not None
        print(f"flush 后:同 session 能读到 = {same}")
        print(f"flush 后:另一个 session 能读到 = {other_sees}(没 commit,看不到)")

    with sf() as after:
        print(f"begin 退出(commit)后:新 session 能读到 = "
              f"{after.get(Author, 4) is not None}")


async def main() -> None:
    sep("第 1 幕:复现地雷 —— add 后查同表,读不到")
    await act1_mine()

    sep("第 2 幕:uow.flush() 拆雷")
    await act2_defuse()

    sep("第 3 幕:autoflush=True 时同样的代码能读到")
    await act3_autoflush_true()

    sep("第 4 幕:可见性边界 —— flush ≠ commit")
    await act4_boundary()

    sep("项目现状:地雷为什么现在没响")
    print("""
  1. 项目仓库的写操作几乎都是 session.execute(...) —— Core 语句直接发 SQL,
     根本不走「排队队列」,所以队列通常是空的,查询自然读得到。
  2. 唯一的 ORM 排队场景,项目已经显式 flush 了:
     author.py 的 ensure_exists() 写完后 self.session.flush() —— 先例已存在。
  3. 地雷的触发条件 = 有人用 session.add() 后在同一事务里查询同表。
     现在没人踩,但这是约定不是机制 —— 所以立了 uow.flush() 这个工具,
     并写进了 engine.py 的 docstring。
""")


if __name__ == "__main__":
    asyncio.run(main())
