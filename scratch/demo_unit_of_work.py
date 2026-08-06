"""演示:SqlUnitOfWork.begin() 到底是什么 —— 上下文管理器拆解

uow.begin() 的本质是一个 async 上下文管理器:
「进入时打开一个事务工作区,退出时自动 提交/回滚/清理」。

本脚本四幕,从最原始的 with 语法一路搭到项目的真实 UoW:

  第 1 幕:手写上下文管理器 —— with 的两个钩子
  第 2 幕:@contextmanager 的 yield 魔法
  第 3 幕:@asynccontextmanager 的 async with
  第 4 幕:真实 SqlUnitOfWork.begin() 的四种命运
"""

from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.database.models import Base, Author
from copixiv.infrastructure.database.uow import SqlUnitOfWork


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


# ---------------------------------------------------------------------------
# 第 1 幕:手写上下文管理器 —— with 的两个钩子
# ---------------------------------------------------------------------------

class Notebook:
    """记账本:with 块结束自动结账。

    __enter__ 在进入 with 块时调用,__exit__ 在离开时调用(无论正常还是异常)。
    这就是 with 的全部魔法 —— 两个钩子。
    """

    def __enter__(self):
        print("   [enter] 打开记账本")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # exc_type 是 None = 一切正常;不是 None = 块里抛了异常
        if exc_type is None:
            print("   [exit] 一切正常 → 结账存档")
        else:
            print(f"   [exit] 出事了({exc_type.__name__}) → 撕掉这页,不存档")
        return False   # False = 不吞异常,让异常继续往外冒


def act1_manual() -> None:
    print("正常路径:")
    with Notebook() as nb:
        print("   [body] 记了一笔:今天赚了 100 块")

    print("\n异常路径:")
    try:
        with Notebook() as nb:
            print("   [body] 记了一笔:今天赚了 100 块")
            raise ValueError("账对不上")
    except ValueError as e:
        print(f"   异常照常冒出来,被外面接住: {e}")
    print("→ with 的要点:不管 body 里发生什么,__exit__ 一定执行。")


# ---------------------------------------------------------------------------
# 第 2 幕:@contextmanager 的 yield 魔法
# ---------------------------------------------------------------------------

@contextmanager
def notebook():
    """装饰器版:yield 把函数劈成两半。
    yield 之前 = enter 逻辑;yield 之后 = exit 逻辑。
    """
    print("   [enter] 打开记账本")
    try:
        yield "nb"            # with xxx as nb 里拿到的就是 yield 的值
    finally:
        print("   [exit] 合上记账本(无论成败都会执行)")


def act2_contextmanager() -> None:
    with notebook() as nb:
        print(f"   [body] 用 {nb} 记了一笔")
    print("→ @contextmanager 只是帮你把「enter/exit 两个钩子」翻译成一段函数:")
    print("  yield 前是 enter,yield 后是 exit —— 这就是『语法糖』的意思。")


# ---------------------------------------------------------------------------
# 第 3 幕:@asynccontextmanager 的 async with
# ---------------------------------------------------------------------------

@asynccontextmanager
async def async_notebook():
    """async 版:一模一样,只是 enter/exit 逻辑里可以 await。"""
    print("   [enter] 打开记账本")
    try:
        yield "nb"
    finally:
        print("   [exit] 合上记账本")


async def act3_async() -> None:
    async with async_notebook() as nb:
        print(f"   [body] 用 {nb} 记了一笔(async 世界)")
    print("→ async with = 同样的两个钩子,只是 __aenter__/__aexit__ 是协程。")


# ---------------------------------------------------------------------------
# 第 4 幕:真实 SqlUnitOfWork.begin() 的四种命运
# ---------------------------------------------------------------------------

def make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return create_session_factory(engine)


def check(session_factory, author_id: int) -> str:
    with session_factory() as s:
        row = s.get(Author, author_id)
        return "在库里 ✅" if row else "不在库里 ❌"


async def act4_real_uow() -> None:
    sf = make_session_factory()

    # --- 命运 1:一切正常 → 退出时自动 commit
    print("命运 1 — 正常退出 → commit:")
    uow = SqlUnitOfWork(sf)                 # 传 sessionmaker → 自建 session
    async with uow.begin():
        uow.session.add(Author(author_id=1, author_name="alice"))
    print(f"   begin 块结束后,alice {check(sf, 1)}")
    print(f"   begin 块结束后,uow 自己建的 session 已被关闭并重置: {uow._session}")

    # --- 命运 2:块里抛异常 → 自动 rollback,异常继续往外冒
    print("\n命运 2 — 块里抛异常 → rollback + 异常上抛:")
    try:
        async with uow.begin():
            uow.session.add(Author(author_id=2, author_name="bob"))
            raise RuntimeError("boom")
    except RuntimeError as e:
        print(f"   异常冒出来了: {e}")
    print(f"   bob {check(sf, 2)}")

    # --- 命运 3:传外部 session(FastAPI 模式)→ commit,但不负责关闭
    print("\n命运 3 — 外部 session(web_api/deps.py 的 get_uow 正是这样):")
    session = sf()                          # FastAPI 的 Depends(get_db) 负责建
    uow = SqlUnitOfWork(session)            # 传 Session → 不拥有它
    async with uow.begin():
        uow.session.add(Author(author_id=3, author_name="carol"))
    print(f"   carol {check(sf, 3)}")
    print(f"   begin 结束后 session 没被关,还能继续用: "
          f"{session.get(Author, 3).author_name}")
    session.close()                         # FastAPI 在请求结束时关

    # --- 命运 4:同一次 begin 里 commit 之后再 raise → 已提交的不回滚
    print("\n命运 4 — 块里手动 commit 后再抛异常 → 已提交的保留:")
    try:
        async with uow.begin():
            uow.session.add(Author(author_id=4, author_name="dave"))
            uow.session.commit()            # 显式提前提交(通常不该这么做)
            raise RuntimeError("boom2")
    except RuntimeError:
        pass
    print(f"   dave {check(sf, 4)}  ← 因为 commit 已经发生,rollback 管不到它")


async def main() -> None:
    sep("第 1 幕:手写上下文管理器 —— with 的两个钩子")
    act1_manual()

    sep("第 2 幕:@contextmanager 的 yield 魔法")
    act2_contextmanager()

    sep("第 3 幕:@asynccontextmanager 的 async with")
    await act3_async()

    sep("第 4 幕:真实 SqlUnitOfWork.begin() 的四种命运")
    await act4_real_uow()

    sep("最后:begin() 的完整时间线(src/copixiv/infrastructure/database/uow.py:123)")
    print("""
async with uow.begin():                 # __aenter__:啥也没干(惰性)
    uow.session.add(...)                # 第一次用 session 才真正创建
    uow.novels.upsert_novels(...)       # 业务代码 …… 一切在一个事务里
                                        # __aexit__(无异常): await commit()
                                        # __aexit__(有异常): await rollback()
                                        #               再 raise 原异常
                                        # finally: 自建 session → close+清缓存
                                        #         外部 session → 不碰
""")


if __name__ == "__main__":
    asyncio.run(main())
