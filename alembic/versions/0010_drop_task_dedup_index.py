r"""任务去重下线：删除 task_history 部分唯一索引

Revision ID: 0010_drop_task_dedup_index
Revises: 0009_drop_legacy_fts
Create Date: 2026-10-21 00:00:00.000000

A2「去 409」：入队不再去重。``ux_task_history_running`` 只把第二个
pending/running 入队变成 ``IntegrityError``（再由上层映射成 HTTP 409 拒绝），
删掉它之后同一 ``task_func`` 的重复入队照常插行、按 FIFO 排队各跑一次；
串行由 worker 的结构保证，不再需要 DB 唯一约束，也不加任何替代去重。

**回滚**：``downgrade`` 原样重建 0001 的部分唯一索引（只覆盖
pending/running 行，NULL ``task_func`` 不受约束）。注意它只接受**没有**
重复 pending/running 行的数据 —— 回滚前必须先把重复行处理掉，否则
``CREATE UNIQUE INDEX`` 会失败并让整段迁移回滚，不会留下半成品。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0010_drop_task_dedup_index"
down_revision: Union[str, Sequence[str], None] = "0009_drop_legacy_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_task_history_running")


def downgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX ux_task_history_running ON task_history (task_func) "
        "WHERE status IN ('pending','running')"
    )
