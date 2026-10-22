"""计划任务下线：删除 ``scheduled_task.config`` 死字段

Revision ID: 0011_drop_scheduled_task_config
Revises: 0010_drop_task_dedup_index
Create Date: 2026-10-22 00:00:00.000000

``config`` 是任务内核从 APScheduler 时代带过来的遗留列：调度行只存
``name / task / cron / params / is_enabled / sort_index``，执行器读的是
``params``（JSONB），``config`` 既没有任务读取，UI 也没有输入框——前端每次
提交都写死 ``{}``，history_repo 只是把它 json.dumps 进 TEXT 列。这属于 F 块
「同一个东西被描述多遍」的残留：一个永远为空的对象占着一列、一个 API 字段、
两处序列化分支。

删除后：DB 列、``ScheduledTaskCreate/Update/Response.config``、
``history_repo`` 的两处 ``json.dumps``、前端 ``TaskEditModal`` 的
``formState.config`` 全部消失；``params``（JSONB，真在用的那个）不受影响。

**回滚**：``downgrade`` 原样加回 ``TEXT`` 列，``server_default '{}'``——旧行
（删除后新建的）会得到合法的空对象，保证回滚后的应用读到的是 ``{}`` 而不是
NULL。PostgreSQL 11+ 把默认值存进目录，不重写表。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_drop_scheduled_task_config"
down_revision: Union[str, Sequence[str], None] = "0010_drop_task_dedup_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("scheduled_task", "config")


def downgrade() -> None:
    op.add_column(
        "scheduled_task",
        sa.Column("config", sa.Text(), nullable=True, server_default="{}"),
    )
