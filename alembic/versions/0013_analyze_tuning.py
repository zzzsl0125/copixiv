"""表级 autovacuum/analyze 调参：让大表在「每夜新增几千行」的节奏下也重算统计

Revision ID: 0013_analyze_tuning
Revises: 0012_dead_series_ledger
Create Date: 2026-10-24 00:00:00.000000

**问题。** ``novel`` 上有两个决定关键词搜索计划的选择率来源：pg_bigm 的 GIN
索引 ``novel_search_bigm`` 和生成列 ``search_lower`` 的统计。项目里**没有任何
代码跑 ``ANALYZE``**（只有迁移 0007 / 0008 各跑过一次），所以日常完全依赖
autovacuum。

autovacuum 的 analyze 触发条件是
``autovacuum_analyze_threshold + autovacuum_analyze_scale_factor × rowcount``，
默认 ``50 + 0.1 × 行数``。对 24.6 万行的 ``novel`` 来说，要 **约 2.47 万行发生
变化**才会重算统计 —— 而每夜 ``novel_follow`` / ``novel_ranking`` 只新增/更新
几千行。于是统计会连续多夜停留在旧快照上，直到某夜跨过阈值。

这不是理论风险：``docs/perf/REVIEW-2026-10-15-*.md`` 记录过 ``novel`` 的
``last_analyze`` / ``last_autoanalyze`` **双 NULL**（从未分析过），PG 对表达式
索引在无统计时用默认选择率 0.5 %，为 ``keyword:R-18`` 选出 **7.3 秒**的计划；
手工 ``ANALYZE novel`` 后同一查询 **1.7ms**（约 4 400×）。

**修法（表级，不改全局）。** 给本项目的两张热点表设置存储参数：

* ``novel``: ``autovacuum_analyze_scale_factor = 0.02`` —— 约 4 900 行变化就重算
  （实测 ``ANALYZE novel`` 只要 **351ms**，一夜跑几次完全负担得起）；
  ``autovacuum_analyze_threshold = 500`` 让空/小表不至于抖一下就跑。
* ``tag``: ``0.05`` + 500。``tag`` 是 **7.5 万行**、由 ``sync_tag_refs`` 触发器
  维护 ``reference_count``，而别名建议按 ``(reference_count DESC, id DESC)`` 排序
  取前 N——``reference_count`` 的直方图陈旧会直接改变建议质量。实测
  ``ANALYZE tag`` **58ms**。

**为什么不用全局** ``ALTER SYSTEM``：这台机器上的 PG 实例是本项目专用的
（``scripts/pg_dev.py``），但全局改动会连带影响将来加到同一实例的库；表级
存储参数随表走、跟着迁移一起回滚，边界更干净。

**为什么不干脆加一个「每天 ANALYZE」的任务**：那只是把同一个失灵机制（没人
触发）换成一个更显眼的失灵机制（任务没被启用 / 被关掉 / 跑失败）。autovacuum
是**已经在跑**的守护进程，把阈值调对就是让它正常工作，比再叠一层任务可靠。

**回滚**：``downgrade`` 把两组参数重置为默认（NULL = 继承全局），不重建、不扫表。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013_analyze_tuning"
down_revision: Union[str, Sequence[str], None] = "0012_dead_series_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: table → (analyze_scale_factor, analyze_threshold)
_TUNING: dict[str, tuple[float, int]] = {
    "novel": (0.02, 500),
    "tag": (0.05, 500),
}


def upgrade() -> None:
    for table, (scale, threshold) in _TUNING.items():
        op.execute(
            f"ALTER TABLE {table} SET ("
            f"autovacuum_analyze_scale_factor = {scale}, "
            f"autovacuum_analyze_threshold = {threshold}"
            f")"
        )
    # The tuning only helps the *next* automatic pass; without this the tables
    # keep plans built from whatever statistics they happened to have, and the
    # whole point is to start clean once.
    op.execute("ANALYZE novel")
    op.execute("ANALYZE tag")


def downgrade() -> None:
    for table in _TUNING:
        op.execute(
            f"ALTER TABLE {table} RESET ("
            f"autovacuum_analyze_scale_factor, "
            f"autovacuum_analyze_threshold"
            f")"
        )
