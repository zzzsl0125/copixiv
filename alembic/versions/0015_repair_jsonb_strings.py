"""清洗 ``task_history`` 里被写成 JSON 字符串的 ``result``

Revision ID: 0015_repair_jsonb_strings
Revises: 0014_task_history_retention
Create Date: 2026-10-24 00:00:00.000000

**问题。** ``recover_stale`` 曾经把 ``json.dumps(...)`` 的**输出字符串**直接
写进 JSONB 列，于是列里存了一个 JSON *string* 而不是对象：

```
jsonb_typeof(result) = 'string'
result::text = '"{\\"summary\\": \\"服务重启导致中断\\"}"'
```

后果是 ``result->>'summary'`` 返回 NULL，UI 上那些行的摘要是空的。代码侧的
写入已经修好（``kernel.recover_stale`` 现在传 dict），但**生产里还躺着 3 行**
（id 1323 / 1327 / 1567）。

**修法。** 把「是字符串、且能解析成对象」的行原位改写为那个对象：

```
result = (result #>> '{}')::jsonb
```

``#>> '{}'`` 取出字符串标量本身（去掉 JSON 引号），再按 jsonb 解析，所以
``'"{\\"summary\\": ...}"'`` → ``{"summary": ...}``。

**只动能安全还原的行**：``jsonb_typeof = 'string'`` 且内层文本能解析成 JSON
**对象**（``(...)::jsonb`` 成功且 ``jsonb_typeof = 'object'``）。解析不成对象
的字符串（比如一整段纯文本日志）保持原样——那属于历史脏数据，不是这次要丢掉的
信息；读侧已经能容错显示（``TaskHistoryResponse`` 会把它包进 ``{"raw": ...}``）。

**回滚**：``downgrade`` 把对象重新序列化回字符串。注意它不是逐字节可逆——只对
**本次 upgrade 改写过的行**（``end_time IS NOT NULL`` 且 result 现在是对象、且有
``summary`` 无其他键？）无法精确识别，因此 downgrade 选择**不做任何事**并在
docstring 里说明：这些行本来就是坏数据，回滚没有意义。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015_repair_jsonb_strings"
down_revision: Union[str, Sequence[str], None] = "0014_task_history_retention"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``(result #>> '{}')`` is the raw string payload; cast it back to jsonb and
    # keep it only when the result is an *object* (the shape every reader
    # expects).  Anything else stays untouched.
    op.execute("""
        UPDATE task_history
           SET result = (result #>> '{}')::jsonb
         WHERE jsonb_typeof(result) = 'string'
           AND (result #>> '{}') ~ '^\\s*\\{'
           AND jsonb_typeof((result #>> '{}')::jsonb) = 'object'
    """)


def downgrade() -> None:
    # Deliberately a no-op.  The string form was corruption produced by a bug
    # that no longer exists; re-introducing it would only re-break the rows
    # whose summary the UI needs.  Rolling back this migration means "leave the
    # data readable", not "restore the broken encoding".
    pass
