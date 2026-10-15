r"""退役 char-gram 搜索底座：删旧表达式索引与两个旧函数

Revision ID: 0009_drop_legacy_fts
Revises: 0008_tag_search_bigm
Create Date: 2026-10-15 13:00:00.000000

0007 把关键词搜索换到 ``pg_bigm`` 时**故意保留了**旧对象（``novel_search_gin``
索引与 ``copixiv_gram`` / ``copixiv_novel_text`` 两个函数），理由是可回滚：
旧后端进程内存里还是 0003 的谓词，保留旧索引它就不会报错、只是继续返回旧结果。
现在新底座已上线并验证完毕，本迁移把这份「双份维护」的代价收掉。

**为什么值得删**：``novel`` 上已有 18 个索引、341MB，而
``novel_search_gin`` 是其中一个 34MB 的 GIN；每次写入 ``novel``（抓取、刷新
元数据、批量标签、EPUB 状态回写）都要多维护它一次，换来的查询能力为零 ——
没有任何查询路径再引用它（``grep -rn novel_search_gin src/`` 只剩
``search.py`` 里指向**新**索引 ``novel_search_bigm`` 的常量）。
实测依据：2026-10-15 全库 244,719 行上，新旧两条链路在可检索内容上**逐行
等价** ——``to_tsvector('simple', copixiv_gram(copixiv_search_text(...)))`` 与
``copixiv_novel_text(...)`` 的 tsvector 在 244,719 行上 0 处差异，所以旧索引
不承载任何新底座不具备的能力。

**删的是三样东西**：
* ``novel_search_gin``（表达式 GIN，34MB）；
* ``copixiv_novel_text(text,text,text,text[])`` —— 旧索引表达式的唯一依赖者，
  删掉索引后它没有任何依赖者（``pg_depend`` 实测：``deptype='n'`` 只有旧索引
  一条）。注意它**不是** ``search_lower`` 的依赖：生成列用的是 0007 新建的
  ``copixiv_search_text``，两者名字相近但无关；
* ``copixiv_gram(text)`` —— 同理，``copixiv_novel_text`` 的私有辅助函数，
  删掉前者后也没有依赖者。

**不删**：``copixiv_search_text``（``search_lower`` 的生成表达式，删了会连带
要求删列）、``novel_search_bigm``、``tag_search_bigm``、``pg_bigm`` 扩展。

**回滚**：downgrade 原样重建索引与两个函数（DDL 与
``0003_search_expression_index`` 逐字一致）。它是**纯 DDL 重建**，不碰数据
—— ``novel`` 的搜索内容由 ``search_lower`` 生成列承担，与这两个函数无关，
所以重建后旧查询路径立刻可用（不需要 ``REINDEX`` 之外的数据修补）。

**成本（2026-10-15 实测）**：``DROP INDEX`` 释放 34MB，无表重写（索引是独立
对象；生成列 ``search_lower`` 不因它消失而失效）。重建回滚索引在 244,719 行上
约 10s（对照 0007 建 ``novel_search_bigm`` 的 9.11s）。

**运维注意**：删掉后，**仍在运行旧代码的后端进程会立刻报错**
（``relation "copixiv_novel_text" does not exist`` / 索引不存在），这正是
「必须先重启服务再应用本迁移」的原因。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0009_drop_legacy_fts"
down_revision: Union[str, Sequence[str], None] = "0008_tag_search_bigm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 与 0003_search_expression_index 逐字一致，downgrade 用。
_GRAM_FUNCTION = r"""
CREATE OR REPLACE FUNCTION copixiv_gram(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT rtrim(
           regexp_replace(
             regexp_replace(
               regexp_replace(
                 coalesce(txt, ''),
                 '[\x09-\x0D\x1C-\x1F\x20\x85\xA0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]',
                 '', 'g'),
               '[\x00-\x2F\x3A-\x40\x5B-\x60\x7B-\x7F]', '龖', 'g'),
             '(.)', '\1 ', 'g'))
$$;
"""

_NOVEL_TEXT_FUNCTION = r"""
CREATE OR REPLACE FUNCTION copixiv_novel_text(
  title text, author_name text, series_name text, tags text[]
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT copixiv_gram(concat_ws(
    ' ',
    nullif(title, ''),
    nullif(author_name, ''),
    nullif(series_name, ''),
    nullif(array_to_string(coalesce(tags, '{}'::text[]), ' '), '')
  ));
$$;
"""

_EXPRESSION_INDEX = """
CREATE INDEX novel_search_gin ON novel USING gin (
  to_tsvector('simple', copixiv_novel_text(title, author_name, series_name, tags))
)
"""


def upgrade() -> None:
    # 先索引后函数：函数在时索引可以独立 drop，反过来 DROP FUNCTION 会被
    # 索引依赖挡住（没有 CASCADE，故意的 —— 有依赖就该在这里显式失败）。
    op.execute("DROP INDEX IF EXISTS novel_search_gin")
    op.execute(
        "DROP FUNCTION IF EXISTS copixiv_novel_text(text, text, text, text[])"
    )
    op.execute("DROP FUNCTION IF EXISTS copixiv_gram(text)")


def downgrade() -> None:
    # 顺序与 upgrade 相反：先函数（索引表达式要调用它们），再索引。
    op.execute(_GRAM_FUNCTION)
    op.execute(_NOVEL_TEXT_FUNCTION)
    op.execute(_EXPRESSION_INDEX)
