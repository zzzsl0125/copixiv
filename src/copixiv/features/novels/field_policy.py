"""可编辑字段策略 —— 「哪个来源、哪些字段能改」的唯一所有者。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §3（字段清单）/ §5（PATCH 校验表）。
2026-09-22 拍板把 pixiv 书的三列全部收成只读——它们都是**来源拥有的身份/
权威值**，手改会与来源打架：

* ``title``：来源刷新**无条件覆盖**（``repo.py::_upsert_rows`` 的
  ``update_fields_set``），手改等于制造"改完又变回去"的假象；
* ``series_id``：``series_name`` 在同一个覆盖白名单里，改掉 id 之后名字下一轮
  就被来源顶回去 —— id 指向新系列、名字还是旧的；
* ``author_id``：作者身份以来源为准（webview 的 ``user_id``）。它不在覆盖白名单
  里（"非空者胜"），所以手改**不会**被顶回去，而是永久地让「这本书的作者是谁」
  与来源不符 —— 更隐蔽，所以同样收掉。

错配的出口不是手改，而是**同步**（``POST /api/novels/{id}/sync``，
``sync_novel.py``）：按 id 从来源重抓一遍，把来源值恢复回来。

``caption`` 在这一版**从"全局不可编辑"改成"仅本地书可编辑"**（2026-09-22）。
原拍板（§3 / §10-4）的理由通篇是拿 pixiv 书论证的——``caption`` 在
``_upsert_rows`` 的无条件覆盖白名单里，手改会被下次抓取顶回去，所以干脆不给入口。
本地书三条都不成立：

* **没有来源会覆盖**：本地行永不进 ``_upsert_rows``（LIBRARY_DESIGN.md §8-1），
  而 ``_upsert_rows`` 是唯一的"来源权威"写路径；
* **"编辑了看不见效果"不成立**：上传路径写死 ``caption=None``
  （``features/library/ingest.py``），本地书这一列**必然为空**，不是"来源的展示性
  字段"——填进去的就是用户自己的话；
* **没有恢复出口也不构成理由**：pixiv 书"改坏了能同步回来"，本地书本来就没有来源，
  同步对它是 400——这也正是它不会与任何东西打架的原因。

影响面为零：``is_chinese``（读 title+caption 判语种）只在 pixiv 抓取路径调用，
关键字索引读 ``title/author_name/series_name/tags``（不含 caption），``shuffle``
是独立随机列。放开它只多一条写库路径（``update_metadata.py::_caption``）。

**单一所有者**：这份策略由后端下发给前端（``NovelBase.editable_fields``，由本模块
的 :func:`editable_fields` 派生），前端不再自己判断 ``source``——两端只有一份判断，
不会各自漂移。

来源常量复用 ``features/library/local_axis.py`` 里已有的那一份，本模块不新增
字符串字面量（``local_axis`` 是 ``novel.source`` 两值的原始所有者）。
"""

from __future__ import annotations

from copixiv.features.library.local_axis import SOURCE_LOCAL, SOURCE_PIXIV

# 每个来源可编辑的字段。元组顺序 = 响应里 ``editable_fields`` 的顺序。
#
# 未知来源按 pixiv 处理（最保守）：来源没认领的行本来就不该被手改，宁可少放开
# 一个字段，也不要让一条来源不明的行接受手改后被某次刷新顶掉。
EDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    SOURCE_LOCAL: (
        "title", "author_id", "series_id", "series_index", "tags", "create_time",
        "caption",
    ),
    SOURCE_PIXIV: ("series_index", "tags", "create_time"),
}

# 仅本地书可编辑的字段：**从表派生**（local 有、pixiv 没有），不手写第二份清单。
# 顺序即 400 报错顺序——同一请求提交多个非法字段时，报的是这里最靠前的那个。
LOCAL_ONLY_FIELDS: tuple[str, ...] = tuple(
    field
    for field in EDITABLE_FIELDS[SOURCE_LOCAL]
    if field not in EDITABLE_FIELDS[SOURCE_PIXIV]
)

FIELD_LABELS = {
    "title": "标题",
    "author_id": "作者",
    "series_id": "系列",
    "caption": "简介",
}

# ``author_id`` 的理由与另外三个刻意不同：title / series_id 是"会被来源覆盖"，
# 而作者不是覆盖问题——来源压根不覆盖它，是"身份以来源为准"，修正入口是同步。
# ``caption`` 也属"会被来源覆盖"那一类（它在 ``_upsert_rows`` 的覆盖白名单里），
# 所以复用下面那条模板，不单独造句。
AUTHOR_LOCAL_ONLY_REASON = (
    "作者仅本地书可编辑（pixiv 书的作者身份以来源为准，可用「同步」修正）"
)


def editable_fields(source: str | None) -> tuple[str, ...]:
    """该来源可编辑的字段；未知来源按 pixiv（最保守）。"""
    return EDITABLE_FIELDS.get(source, EDITABLE_FIELDS[SOURCE_PIXIV])


def is_editable(source: str | None, field: str) -> bool:
    """*field* 在 *source* 的行上是否可编辑（编辑器与 PATCH 共用这一条）。"""
    return field in editable_fields(source)


def rejection_reason(field: str) -> str:
    """pixiv 书提交仅本地字段时的 400 文案——按字段给，集中在这里。

    title / series_id / caption 共用一个模板（同一个"来源会覆盖"的理由），
    author_id 单独一句（见 :data:`AUTHOR_LOCAL_ONLY_REASON`）。两句话都带
    「仅本地书可编辑」，前端按这句话做统一提示即可。
    """
    if field == "author_id":
        return AUTHOR_LOCAL_ONLY_REASON
    return f"{FIELD_LABELS[field]}仅本地书可编辑（pixiv 来源会覆盖该字段）"


__all__ = [
    "AUTHOR_LOCAL_ONLY_REASON",
    "EDITABLE_FIELDS",
    "FIELD_LABELS",
    "LOCAL_ONLY_FIELDS",
    "SOURCE_LOCAL",
    "SOURCE_PIXIV",
    "editable_fields",
    "is_editable",
    "rejection_reason",
]
