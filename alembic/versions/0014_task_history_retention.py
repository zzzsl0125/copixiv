"""``task_history``：队列/查找索引 + 保留策略

Revision ID: 0014_task_history_retention
Revises: 0013_analyze_tuning
Create Date: 2026-10-24 00:00:00.000000

**问题。** ``task_history`` 只有主键索引，而它承担三件事：

1. **它是队列本身。** worker 每秒一次
   ``WHERE status = 'pending' ORDER BY id LIMIT 1``（``history_repo.py``），
   现在靠表小（1,566 行）才便宜——实测计划是 ``Seq Scan + Sort``，**0.46ms**。
   随着历史增长，这是唯一一处「功能会随数据量线性变慢」的地方。
2. **按函数查 busy。** ``/api/tasks/busy`` 每次轮询
   ``SELECT DISTINCT task_func WHERE status IN ('pending','running')``。
3. **只增不清。** ``result`` 已累计 **13MB** / 1,566 行，单行最大 **525KB**
   （其中 ``result.log`` 高达 1.26M 字符）。没有任何裁剪任务。

**索引。** 两个部分索引，各自只覆盖真正会被查询的行：

* ``ix_task_history_pending`` — ``(id) WHERE status = 'pending'``。队列查询只
  关心 pending 行，通常是 0–2 行；部分索引让它变成一次几乎空索引的 range scan，
  而不是全表扫描 + 排序。
* ``ix_task_history_active_func`` — ``(task_func) WHERE status IN ('pending',
  'running')``。busy 集合的 DISTINCT 查询直接走它。

两者都是部分索引：**不**为 1,500 条终态历史行付代价，写入也只更新相关条目。

**保留策略。** 没有自动删除是刻意的取舍——历史是审计资料，静默删掉会丢证据。
但「永不裁剪」也不是策略，尤其日志已经上千 KB。规则写成**两层**：

* 保留最近 ``TASK_HISTORY_KEEP``（默认 2,000）行，**无论多老**；
* 同时删除超过 ``TASK_HISTORY_MAX_AGE_DAYS``（默认 180 天）的行。

配置放在 ``setting`` 表（``task_history.retention``，JSON），由
``prune_orphan_files`` 之外的独立任务 ``prune_task_history`` 执行（默认试运行）。
迁移本身不删任何行——它只建索引，让「清理」这件事第一次成为可能。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_task_history_retention"
down_revision: Union[str, Sequence[str], None] = "0013_analyze_tuning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The queue read: only pending rows, only the ordering key.
    op.create_index(
        "ix_task_history_pending",
        "task_history",
        ["id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # The busy read: which functions currently have work.
    op.create_index(
        "ix_task_history_active_func",
        "task_history",
        ["task_func"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_history_active_func", table_name="task_history")
    op.drop_index("ix_task_history_pending", table_name="task_history")
