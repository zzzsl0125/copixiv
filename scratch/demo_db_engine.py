"""演示:SQLAlchemy 的「数据库连接引擎」到底是怎么回事

stage5 落库那几行代码看不懂,是因为它们背后压着一整套 SQLAlchemy 概念。
本脚本从最底层开始,一幕一幕把它搭起来:

  第 1 幕:没有 SQLAlchemy 时 —— 裸 sqlite3 怎么连库
  第 2 幕:SQLAlchemy 三件套 —— Engine / sessionmaker / Session
  第 3 幕:stage5 里三个神秘参数逐个拆
          3.1 内存库「每个连接各是各的库」→ 所以要用 StaticPool
          3.2 check_same_thread=False 解决什么
          3.3 PRAGMA foreign_keys=ON 解决什么
  第 4 幕:Base.metadata.create_all() 是什么
  第 5 幕:事务 —— commit 和 rollback

跑完再看 stage5,那几行代码就全是熟人了。
"""

from __future__ import annotations

import sqlite3
import threading

from sqlalchemy import (
    Column, Integer, String, ForeignKey, create_engine, event, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool


from sqlalchemy.schema import CreateTable


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


# ---------------------------------------------------------------------------
# 第 1 幕:没有 SQLAlchemy 时 —— 裸 sqlite3
# ---------------------------------------------------------------------------

def act1_plain_sqlite() -> None:
    # sqlite3 是 Python 自带的模块,直接连数据库文件
    conn = sqlite3.connect("scratch/demo_plain.db")   # 一个文件 = 一个数据库
    conn.execute(
        "CREATE TABLE IF NOT EXISTS authors "
        "(author_id INTEGER PRIMARY KEY, author_name TEXT)"
    )
    conn.execute("INSERT INTO authors (author_name) VALUES ('夜猫子')")
    conn.commit()   # ① 没有 commit,数据不会真正落盘
    rows = conn.execute("SELECT * FROM authors").fetchall()
    print(f"裸 sqlite3 查询结果: {rows}")
    print("这里没有 ORM:写 SQL 字符串,拿到的是一行行元组。")
    conn.close()

    # 顺带记一个事实:sqlite3.connect(":memory:") 每次都是全新空库
    c1 = sqlite3.connect(":memory:")
    c1.execute("CREATE TABLE t (x INTEGER)")
    c2 = sqlite3.connect(":memory:")   # 第二条连接,和 c1 毫无关系
    try:
        c2.execute("SELECT * FROM t")
    except sqlite3.OperationalError as e:
        print(f"内存库隔离事实: c1 建的表,c2 查不到 → {e}")


# ---------------------------------------------------------------------------
# 第 2 幕:SQLAlchemy 三件套
# ---------------------------------------------------------------------------

def act2_engine_session() -> None:
    # Engine:一个「总开关」—— 它自己不管业务,只管两件事:
    #   1. 知道连哪个数据库(engine.url)
    #   2. 管着一个连接池(要用连接时找它要)
    engine = create_engine("sqlite:///scratch/demo_sa.db")
    print(f"engine 类型: {type(engine).__name__}")
    print(f"engine.url : {engine.url}")

    # sessionmaker:造 Session 的「工厂」—— 它记住绑定了哪个 engine
    SessionFactory = sessionmaker(bind=engine)
    print(f"sessionmaker: {SessionFactory}")

    # Session:一个「工作区」—— 你在这个工作区里增删改查,
    # 一切都在一个事务里;commit 才落盘,rollback 全部作废。
    s = SessionFactory()
    s.execute(text(
        "CREATE TABLE IF NOT EXISTS authors "
        "(author_id INTEGER PRIMARY KEY, author_name TEXT)"
    ))
    s.execute(text(
        "INSERT INTO authors (author_name) VALUES ('工作区里的猫')"
    ))
    s.commit()
    row = s.execute(text("SELECT * FROM authors")).fetchone()
    print(f"Session 查询结果: {row}")
    s.close()


# ---------------------------------------------------------------------------
# 第 3.1 幕:内存库「每个连接各是各的库」→ 所以要用 StaticPool
# ---------------------------------------------------------------------------

def act3_1_memory_isolated() -> None:
    print("场景:内存库(sqlite://)+ 代码跑在线程池里(upsert_novels 正是这样)")

    # 内存库没有文件,数据库内容跟着「连接」走。
    # SQLAlchemy 默认每个线程拿到的连接是独立的内存库 ——
    # 主线程里建好的表,工作线程里根本不存在。
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},   # 先关线程检查,单独看这个问题
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
        conn.commit()

    result: list[str] = []

    def worker() -> None:
        try:
            with engine.connect() as conn:
                n = conn.execute(text("SELECT COUNT(*) FROM t")).scalar()
                result.append(f"工作线程查到了 {n} 行")
        except Exception as e:
            result.append(f"工作线程报错: {type(e).__name__}: {e}")

    threading.Thread(target=worker).start()
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    print(f"主线程: 建表 + 插数据 OK")
    print(f"工作线程: {result[0]}")

    # 修复:StaticPool —— 强制所有 session/连接共享同一条底层连接
    # (项目测试和本演示的 stage5 都这么干,因为内存库没有文件可以共享)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
        conn.commit()

    result2: list[str] = []

    def worker2() -> None:
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM t")).scalar()
            result2.append(f"工作线程查到了 {n} 行")

    threading.Thread(target=worker2).start()
    threading.Thread(target=worker2).start()   # 两个线程同时用同一条连接
    import time
    time.sleep(0.1)
    print(f"StaticPool 后,工作线程们: {result2}")
    print("→ 结论:内存库要么别跨线程,要么 StaticPool 共享同一条连接。")


# ---------------------------------------------------------------------------
# 第 3.2 幕:check_same_thread=False 解决什么
# ---------------------------------------------------------------------------

def act3_2_check_same_thread() -> None:
    print("场景:一条 SQLite 连接被另一个线程使用")

    # ① 裸 sqlite3 的规则:连接有「线程户口」,只能创建它的线程用
    conn = sqlite3.connect("scratch/demo_thread.db")
    err: list[str] = []

    def worker() -> None:
        try:
            conn.execute("SELECT 1")
            err.append("成功")
        except Exception as e:
            err.append(f"{type(e).__name__}: {str(e)[:50]}")

    threading.Thread(target=worker).start()
    import time
    time.sleep(0.1)
    print(f"裸 sqlite3(默认): {err[0]}")

    # ② 但 SQLAlchemy 的文件库连接池,默认已经把检查关掉了
    engine = create_engine("sqlite:///scratch/demo_thread.db")
    c = engine.connect()
    c.execute(text("SELECT 1"))
    err2: list[str] = []

    def worker2() -> None:
        try:
            c.execute(text("SELECT 1"))
            err2.append("成功")
        except Exception as e:
            err2.append(type(e).__name__)

    threading.Thread(target=worker2).start()
    time.sleep(0.1)
    print(f"SQLAlchemy 文件库(什么都没写): {err2[0]}")
    print(f"    pool={type(engine.pool).__name__} —— 连接要跨线程归还,所以默认放行")

    # ③ 内存库则相反:默认保持检查
    engine_mem = create_engine("sqlite://")
    c3 = engine_mem.connect()
    c3.execute(text("SELECT 1"))
    err3: list[str] = []

    def worker3() -> None:
        try:
            c3.execute(text("SELECT 1"))
            err3.append("成功")
        except Exception as e:
            err3.append(type(e).__name__)

    threading.Thread(target=worker3).start()
    time.sleep(0.1)
    print(f"SQLAlchemy 内存库(什么都没写): {err3[0]}")
    print(f"    pool={type(engine_mem.pool).__name__} —— 单线程池,保持检查")

    c.close()
    c3.close()
    print("→ 所以 stage5/测试里对内存库写 check_same_thread=False 是必须的:")
    print("  内存库 + StaticPool + 线程池执行,三道坎要一起拆。")


# ---------------------------------------------------------------------------
# 第 3.3 幕:PRAGMA foreign_keys=ON 解决什么
# ---------------------------------------------------------------------------

def act3_3_foreign_keys() -> None:
    print("场景:novels.author_id 外键指向 authors.author_id")

    def make_engine(with_fk: bool):
        eng = create_engine("sqlite://")
        if with_fk:
            @event.listens_for(eng, "connect")
            def _fk(dbapi_connection, _connection_record):
                cur = dbapi_connection.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        return eng

    for with_fk, label in ((False, "默认(SQLite 不检查外键)"),
                           (True, "PRAGMA foreign_keys=ON 之后")):
        eng = make_engine(with_fk)
        with eng.connect() as conn:
            conn.execute(text(
                "CREATE TABLE authors "
                "(author_id INTEGER PRIMARY KEY, author_name TEXT)"
            ))
            conn.execute(text(
                "CREATE TABLE novels (id INTEGER PRIMARY KEY, title TEXT, "
                "author_id INTEGER REFERENCES authors(author_id))"
            ))
            try:
                # 往 novels 里插一篇作者 ID=999 的小说,但 authors 表里没有 999
                conn.execute(text(
                    "INSERT INTO novels (title, author_id) VALUES ('孤儿', 999)"
                ))
                conn.commit()
                print(f"{label}: 插入成功 —— 孤儿数据混进来了")
            except Exception as e:
                print(f"{label}: 报错 {type(e).__name__}: {str(e)[:60]}")
    print("→ SQLite 默认不守外键,SQLAlchemy 也帮不上忙,必须在每次连接时手动开。")


# ---------------------------------------------------------------------------
# 第 4 幕:Base.metadata.create_all() 是什么
# ---------------------------------------------------------------------------

def act4_create_all() -> None:
    # 用 Python 类描述表结构(ORM 模型),注册进 Base.metadata
    Base = declarative_base()

    class Author(Base):
        __tablename__ = "authors"
        author_id = Column(Integer, primary_key=True)
        author_name = Column(String)

    class Novel(Base):
        __tablename__ = "novels"
        id = Column(Integer, primary_key=True)
        title = Column(String)
        author_id = Column(Integer, ForeignKey("authors.author_id"))

    print(f"Base.metadata 里注册了: {list(Base.metadata.tables)}")

    # create_all:按 metadata 里注册的表,在目标库里执行 CREATE TABLE
    engine = create_engine("sqlite:///scratch/demo_schema.db")
    print(f"create_all() 会执行的 SQL:\n{CreateTable(Novel.__table__).compile(engine)}")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    print(f"建库后 sqlite_master 里的表: {tables}")


# ---------------------------------------------------------------------------
# 第 5 幕:事务 —— commit 和 rollback
# ---------------------------------------------------------------------------

def act5_transaction() -> None:
    Base = declarative_base()

    class Author(Base):
        __tablename__ = "authors"
        author_id = Column(Integer, primary_key=True)
        author_name = Column(String)

    engine = create_engine("sqlite:///scratch/demo_tx.db")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    s = SessionFactory()
    s.add(Author(author_name="猫猫"))
    s.commit()                      # ① 提交 → 永久
    s.add(Author(author_name="狗狗"))
    s.rollback()                    # ② 回滚 → 狗狗根本没进数据库
    s.close()

    s2 = SessionFactory()
    names = [r[0] for r in s2.execute(text("SELECT author_name FROM authors"))]
    print(f"commit 后库里有: {names}")
    s2.close()
    print("→ Session 里的一切修改默认都在「一个事务」里,commit 才生效。")


def main() -> None:
    sep("第 1 幕:裸 sqlite3 —— 没有 ORM 的世界")
    act1_plain_sqlite()

    sep("第 2 幕:SQLAlchemy 三件套 —— Engine / sessionmaker / Session")
    act2_engine_session()

    sep("第 3.1 幕:内存库「每个连接各是各的库」→ StaticPool")
    act3_1_memory_isolated()

    sep("第 3.2 幕:check_same_thread=False")
    act3_2_check_same_thread()

    sep("第 3.3 幕:PRAGMA foreign_keys=ON")
    act3_3_foreign_keys()

    sep("第 4 幕:Base.metadata.create_all()")
    act4_create_all()

    sep("第 5 幕:事务 —— commit / rollback")
    act5_transaction()


if __name__ == "__main__":
    main()
