r"""关键词搜索换底座：物化 lower 文本列 + pg_bigm bigram GIN 索引

Revision ID: 0007_search_bigm
Revises: 0006_local_id_time_axis
Create Date: 2026-10-15 00:00:00.000000

动机见 ``docs/perf/PG_BIGM_FEASIBILITY.md``（完整实测报告）：

1. **旧方案有一个真 bug**：0003 的 ``copixiv_gram`` 把空白**直接丢弃**，于是
   相邻两个词的字符在 tsvector 里位置连续，短语匹配会**跨过词边界** ——
   tags 为 ``['R-18','gay']`` 的书会被 ``R-18G`` 搜到（实测 3,523 条纯假阳性，
   原始文本里没有任何一条含该子串）。真子串匹配从语义上不可能有这个洞。
2. **旧方案有一道延迟悬崖**：``tsvector @@ tsquery`` 的选择率估计不读列统计
   （``催眠`` 真实 15,780 行估成 1,092），规划器于是选中 ``Bitmap Heap Scan``
   （660ms）而不是能提前终止的 ``Index Scan``（19ms）。``ANALYZE`` / 提高
   ``default_statistics_target`` / 物化 tsvector 列都试过，修不好。
   ``pg_bigm`` 的估计来自 GIN 索引统计，误差 ≤1.4×。

本迁移建四样东西（查询侧由 ``features/novels/search.py`` 同批切换）：

* ``CREATE EXTENSION pg_bigm`` —— 需要以编译它的同一套 PG 二进制部署；
* ``copixiv_search_text(title, author_name, series_name, tags)`` —— **只为生成列
  而存在的 IMMUTABLE 包装**（见下）；
* ``novel.search_lower`` —— **STORED 生成列**，把这四个字段按
  ``lower(concat_ws(' ', nullif(...) ...))`` 拼成一段小写文本：
  去掉了 char-gram 分词与大小写，剩下的就是「被搜索的文本」本身。
  ``nullif(...,'')`` 是必须的：``concat_ws`` 只跳过 NULL，空串会贡献一个
  多余的分隔空格，让「字段边界」在 bigram 上多出一个位置。
  **生成列由 PostgreSQL 从本行直接维护**，写入路径（仓储/任务/psql/迁移脚本）
  一行都不用改，也不可能漂移 —— 与 0003 的表达式索引同一个保证。
* ``novel_search_bigm`` —— ``USING gin (search_lower gin_bigm_ops)``，
  查询侧每段一个 ``search_lower LIKE lower(:pN) ESCAPE '\'``，AND 相连。

**为什么要一个包装函数**：设计稿写的是把
``lower(concat_ws(' ', nullif(title,''), ..., nullif(array_to_string(...),'')))``
直接内联进生成列，但 PostgreSQL 16 **拒绝**该 DDL：
``generation expression is not immutable``。原因是 ``concat_ws`` 与
``array_to_string`` 在 pg_proc 里都标成 ``STABLE``（它们的实现要调元素类型的
输出函数，对任意类型不保证 Immutable），而生成列要求表达式 Immutable。
实测三种写法：``lower(concat_ws(...))`` ✗、``lower(array_to_string(...))`` ✗、
纯 ``||`` + ``btrim`` ✓。数组→字符串的 join 没有等价的纯内联写法，所以把
**整段拼装原样**搬进一个 ``LANGUAGE sql`` 的 IMMUTABLE 函数：对 ``text`` 入参
而言 ``concat_ws`` / ``array_to_string`` 本来就是确定性的（STABLE 是面向任意
元素类型的保守标注），声明 Immutable 是可辩护的，且随迁移冻结、不随应用代码漂移。
函数体就是设计稿那一行，语义逐字节一致（``concat_ws`` 的空字段跳过、单空格
分隔、全空 → ``''`` 而非 NULL，都已实测）。

**故意不删旧对象**：``novel_search_gin`` 索引与 ``copixiv_gram`` /
``copixiv_novel_text`` 两个函数在 upgrade 里原样保留。理由是**可回滚**：
downgrade 只需要 drop 新索引/新列/新函数/扩展，旧路径（查询侧回退到 0003 版本的
``search.py``）立刻可用，不必重放 0003。代价只是多占 102MB 索引空间，等新路径
跑过一轮观察期，再由后续迁移清理（那时要一并确认没有别的调用者）。

**成本（2026-10-15 在活库 copixiv 上实测：244,719 行、迁移前 549MB（含索引））**：
``ADD COLUMN`` **12.57s**（整表重写，STORED 生成列必须逐行求值，并**顺带重建
``novel`` 上已有的全部索引** —— 旧 FTS 索引顺手从 102MB 的膨胀态缩到 34MB）、
``CREATE INDEX novel_search_bigm`` **9.11s**（81.5MB）、``ANALYZE novel``
**1.19s**、全程 **23.13s**。Alembic 把它跑在一个事务里，所以整个过程持
``ACCESS EXCLUSIVE`` 锁 —— 不是单用户实例时请改用「低峰执行」或在线重建方案。
``CREATE INDEX CONCURRENTLY`` 在这里不可用（不能在事务块内），开发实例是单用户，
普通 ``CREATE INDEX`` 足够。整段 23s < 应用连接上的 ``lock_timeout = 60s``，
空闲实例上不会打断在线请求。

末尾补一次 ``ANALYZE novel``：生成列的统计（``pg_stats`` 会正常收集）决定
bigram 谓词的选择率，而本项目**没有任何后台任务在批量导入后跑 ANALYZE** ——
不补这一次，库里第一次查询就要靠猜（旧路径正是被这个坑放大成 7.4 秒）。

**运维注意（实测踩到）**：本迁移只改数据库，**改不动正在运行的进程**。活着的老
进程内存里还是 0003 的谓词，而旧索引/旧函数被刻意保留，所以它不会报错 —— 只会
继续返回旧结果（实测：迁移后 :9000 的 ``/api/novels/ids?keyword=keyword:R-18G``
仍返回 19,948 条、含 3,523 条假阳性，与旧 FTS 集合逐条相同），进程内的 count
cache 同样以 data-version epoch 校验、DDL 不会让它失效（实测 ``/count`` 仍返回
旧值 16,533；新口径是 15,827）。**应用本迁移后必须重启后端进程**，新谓词才会
生效。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_search_bigm"
down_revision: Union[str, Sequence[str], None] = "0006_local_id_time_axis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 拼装规则（与 0003 的 copixiv_novel_text 逐段对应：同序、同分隔符、同样跳过空
# 字段），只是不再 char-gram 分词、并整体 lower()。
# ``coalesce(tags,'{}')``：tags 列 NOT NULL，但显式兜住 NULL 更稳（真出 NULL 时
# array_to_string(NULL,...) 会让整列变 NULL，搜索直接全盲）。
# 函数签名（text, text, text, text[]）是 downgrade DROP FUNCTION 的一部分，
# 改名/改型必须两处一起改。
_SEARCH_TEXT_BODY = (
    "SELECT lower(concat_ws(' ', "
    "nullif(title, ''), "
    "nullif(author_name, ''), "
    "nullif(series_name, ''), "
    "nullif(array_to_string(coalesce(tags, '{}'::text[]), ' '), '')"
    "))"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_bigm")
    # 见模块 docstring「为什么要一个包装函数」：生成列要求 Immutable，而
    # concat_ws / array_to_string 在 pg_proc 里是 STABLE。
    op.execute(
        "CREATE FUNCTION copixiv_search_text("
        "title text, author_name text, series_name text, tags text[]"
        ") RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
        f"{_SEARCH_TEXT_BODY} "
        "$$"
    )
    op.execute(
        "ALTER TABLE novel ADD COLUMN search_lower text "
        "GENERATED ALWAYS AS ("
        "copixiv_search_text(title, author_name, series_name, tags)"
        ") STORED"
    )
    op.execute(
        "CREATE INDEX novel_search_bigm ON novel "
        "USING gin (search_lower gin_bigm_ops)"
    )
    # 统计是 bigram 选择率估计的唯一来源（见模块 docstring 末尾）。
    op.execute("ANALYZE novel")


def downgrade() -> None:
    # 顺序不能反：``gin_bigm_ops`` 属于扩展，索引还在时 DROP EXTENSION 会被拒；
    # 生成列依赖函数，列还在时 DROP FUNCTION 会被拒。
    # 旧索引/旧函数不动（upgrade 就没删），所以回退后旧查询路径立刻可用。
    op.execute("DROP INDEX IF EXISTS novel_search_bigm")
    op.execute("ALTER TABLE novel DROP COLUMN IF EXISTS search_lower")
    op.execute(
        "DROP FUNCTION IF EXISTS copixiv_search_text(text, text, text, text[])"
    )
    op.execute("DROP EXTENSION IF EXISTS pg_bigm")
