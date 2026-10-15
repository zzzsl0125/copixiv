r"""标签别名建议换底座：物化 lower 名字列 + pg_bigm bigram GIN 索引

Revision ID: 0008_tag_search_bigm
Revises: 0007_search_bigm
Create Date: 2026-10-15 12:00:00.000000

动机：``features/tags/repo.py::_find_similar_tags`` 每个候选标签发一条

    ... WHERE (tag.name ILIKE 'X%' OR tag.name ILIKE '%name%') AND tag.id != :id
        ORDER BY tag.reference_count DESC LIMIT 50

两个毛病都在同一条谓词上：

1. ``ILIKE`` **用不上任何索引** —— ``tag.name`` 上只有 btree ``tag_name_key``，
   它服务大小写敏感的 ``LIKE``/前缀比较，永远不服务 ``ILIKE``。实测 75,187 行
   的 ``tag`` 上这条语句是 ``Seq Scan on tag (Rows Removed by Filter: 74909)
   → Sort (top-N heapsort)``，``EXPLAIN (ANALYZE)`` 16.769ms / 568 buffers；
   同一份数据上 ``name LIKE 'R%'`` 走 ``Index Only Scan using tag_name_key``
   只要 0.079ms。调用侧又是 N+1：``suggest_aliases(limit=5, offset=0)`` 实测
   7 条语句 / 99–125ms，``limit=50`` → 50 次探测 / 927ms。
2. 通配符没转义：库里真有名为 ``%高中生`` 的标签，``ILIKE '%高中生%'``
   于是匹配全表 75,186 行，端点把全局最热的标签当成「相似」返回。

本迁移照搬 0007 为 ``novel`` 建的那套底座（同一套 pg_bigm 扩展、同一个
「由 PostgreSQL 从行生成」的保证），只是这里只有一个字段：

* ``tag.search_lower`` —— **STORED 生成列** = ``lower(name)``；
* ``tag_search_bigm`` —— ``USING gin (search_lower gin_bigm_ops)``；查询侧
  每个分支一个 ``search_lower LIKE lower(:p) ESCAPE '\'``（模式由
  ``features/novels/search.py::escape_like`` 转义，两侧都用数据库的 ``lower()``
  折叠大小写 —— 与本库 ``datcollate = C`` 下 ``lower()`` 只折 ASCII 的事实一致）；
* ``tag_reference_count_id_idx`` —— ``(reference_count DESC, id DESC)`` btree。
  GIN 只回答「哪些行匹配」，不回答「最热的 50 个」和「从游标处续扫」：
  旧谓词按 ``reference_count DESC`` 排序在 447 个并列值上没有任何可用索引，
  新的 keyset 分页游标 ``(reference_count DESC, id DESC)`` 同样需要这条 btree，
  否则规划器只能对 75,187 行做 ``Seq Scan + Sort``（见 0008 配套的
  ``features/tags/repo.py`` 重写）。

**为什么这次不需要 IMMUTABLE 包装函数**：0007 的 ``copixiv_search_text`` 只是
为了绕开 ``concat_ws`` / ``array_to_string`` 在 ``pg_proc`` 里被标成 STABLE。
``lower(text)`` 本身就是 IMMUTABLE（``pg_proc.provolatile = 'i'``，本机
PostgreSQL 16.2 实测），``GENERATED ALWAYS AS (lower(name)) STORED`` 可以直接
内联，不需要任何包装 —— 实测 DDL 被接受，没有
``generation expression is not immutable``。

**故意不删任何东西**：upgrade 不 DROP 任何既有对象（``tag_name_key`` /
``tag_pkey`` 原样保留），所以 downgrade 只 drop 本迁移新建的两个索引与生成列，
旧查询路径（``name ILIKE``）本来就不依赖索引、立刻可用，不必重放任何历史迁移。
与 0007 不同的另一点：本迁移**不改任何数据**（没有 UPDATE/DELETE/INSERT），
生成列由 PostgreSQL 按行回填。

**成本（2026-10-15 在活库 copixiv 上实测，75,187 行 tag / 289 行 tag_alias；
逐条语句计时 = 在 alembic 的 engine 上挂 before/after_cursor_execute）**：
``ALTER TABLE tag ADD COLUMN`` **94.5ms**（STORED 生成列逐行求值，并顺带重建
``tag`` 上的既有索引）、``CREATE INDEX tag_search_bigm`` **232.8ms**（7,304kB）、
``CREATE INDEX tag_reference_count_id_idx`` **28.1ms**（2,344kB）、
``ANALYZE tag`` **34.1ms**、``UPDATE alembic_version`` 0.6ms，从 ``upgrade()``
入口算全程 **0.47s**。Alembic 把整段跑在一个事务里，所以期间持
``ACCESS EXCLUSIVE`` 锁；表只有 75k 行，短到可以忽略
（对照 0007 在 244,719 行的 ``novel`` 上是 23.13s）。
downgrade → upgrade 已在 copixiv_test 上实跑验证：只掉本迁移建的 4 个对象
（两个索引 + 生成列；``tag_pkey`` / ``tag_name_key`` 原样保留）。

末尾 ``ANALYZE tag``：bigram 谓词的选择率估计来自 GIN 索引统计与列统计，而本
项目**没有任何后台任务在批量导入后跑 ANALYZE**（与 0007 同一个理由）——不补这
一次，库里第一次查询就要靠猜。

**运维注意**：本迁移只加对象，正在运行的旧后端进程（内存里还是
``name ILIKE`` 谓词）不会报错，只是继续走 seq scan；重启后才会用上新列与索引。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_tag_search_bigm"
down_revision: Union[str, Sequence[str], None] = "0007_search_bigm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # lower(text) 是 IMMUTABLE（见模块 docstring），可内联进生成列，
    # 不需要 0007 那种 IMMUTABLE 包装函数。
    op.execute(
        "ALTER TABLE tag ADD COLUMN search_lower text "
        "GENERATED ALWAYS AS (lower(name)) STORED"
    )
    op.execute(
        "CREATE INDEX tag_search_bigm ON tag "
        "USING gin (search_lower gin_bigm_ops)"
    )
    # 分页/排序用的有序索引（GIN 不提供顺序）。DESC 与查询里的
    # ORDER BY reference_count DESC, id DESC 对齐。
    op.execute(
        "CREATE INDEX tag_reference_count_id_idx ON tag "
        "(reference_count DESC, id DESC)"
    )
    # 统计是 bigram 选择率估计的唯一来源（见模块 docstring 末尾）。
    op.execute("ANALYZE tag")


def downgrade() -> None:
    # 先索引后列：列在时索引可以独立 drop，反过来靠级联也行，但显式顺序更清楚。
    # 不 DROP EXTENSION pg_bigm —— 它属于 0007 的 novel 搜索路径。
    op.execute("DROP INDEX IF EXISTS tag_reference_count_id_idx")
    op.execute("DROP INDEX IF EXISTS tag_search_bigm")
    op.execute("ALTER TABLE tag DROP COLUMN IF EXISTS search_lower")
