"""死系列台账：给「pixiv 侧已不存在」的系列一个可判定的终态

Revision ID: 0012_dead_series_ledger
Revises: 0011_drop_scheduled_task_config
Create Date: 2026-10-24 00:00:00.000000

``fix_series_index`` 的候选集是「``series_id`` 有值、``series_index`` 为空」的
系列。这个谓词**无法区分**「这次拉不到」和「永远拉不到」——系列在 pixiv 侧被
删除（``novel_series`` 稳定 404）之后，本地 ``novel.series_id`` 仍指着它，于是
它每一轮都重新成为候选，每一轮都重新发一次必然失败的请求。

生产实测（task_history id=1564 / 1569）：

| 轮次 | 候选 | 其中 404 | 有效产出 | 耗时 |
|---|---:|---:|---:|---:|
| 1564（2026-10-22） | 200 | **183** | 补号 0–47 | 1740 s |
| 1569（2026-10-23） | 82 | **49** | 补号 26 | 481 s |

即 **60–92% 的预算烧在注定拿不到任何东西的系列上**，而候选集总共只有 462 个
系列。修法不是加预算，而是给死系列一个终态并让候选查询排除它。

**为什么新建一张表而不是给 ``series`` 加一列。** ``series`` 是纳入统计的实体
（``update_summary`` 会把 ``novel_count`` / like / view 聚合进去，作者页、
批量编辑、系列预览都读它），给一个「它其实已经不在 pixiv 上了」的运维事实塞进
实体表会让每个读 ``series`` 的地方都得先问一次「这行还算数吗」。台账是**判定**
（verdict），不是实体数据——与 ``failed_novel`` 同构：独立表、独立 owner、
不影响业务读路径。

**为什么带 ``consecutive_failures`` 而不是一遇 404 就判死。** 单次 404 可能是
pixiv 的瞬时行为（限流兜底、CDN 回源失败也会以 404 形态出现）。规则是
**连续** N 次（默认 3 轮）都拿到「确定性 404」才判死；任何一次成功把计数清零。
``last_error`` 保留最后一轮的原始错误，运维能看出「为什么判死」。

**为什么 ``retired_at`` 而不是直接删行。** 判死之后 pixiv 侧若恢复（系列被
重建、或 404 其实是误判），``sweep_all`` 与手动重跑仍能让它复活：一次成功会
把 ``retired_at`` 清空并归零计数。台账只影响**常规候选集**，不是不可撤销的
删除——这正是「写一个标记后续不再处理」应有的可逆性。

Revises: 0011
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_dead_series_ledger"
down_revision: Union[str, Sequence[str], None] = "0011_drop_scheduled_task_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "series_dead_letter",
        # 与 ``failed_novel`` 同构：业务主键就是 pixiv 的 series_id，无 FK
        # （台账可能先于 series 行存在，且 series 行被删不该带走判定记录）。
        sa.Column("series_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "last_failed_at", sa.DateTime(timezone=True), nullable=True,
        ),
        # 非 NULL = 已判死，候选查询排除它。一次成功会把它清回 NULL。
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 候选查询只关心「已判死」的行（取集合），给它一个部分索引。
    op.create_index(
        "ix_series_dead_letter_retired_at",
        "series_dead_letter",
        ["retired_at"],
        postgresql_where=sa.text("retired_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_series_dead_letter_retired_at", table_name="series_dead_letter",
    )
    op.drop_table("series_dead_letter")
