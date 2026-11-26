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

``caption`` 已从响应与编辑界面完全移除（2026-11：卡片列表跳过大字段 IO，
编辑页不再展示或编辑简介；数据库 ``novel.caption`` 列与抓取逻辑 ``is_chinese``
保持不变）。

**单一所有者**：这份策略由后端下发给前端（``NovelBase.editable_fields``，由本模块
的 :func:`editable_fields` 派生），前端不再自己判断 ``source``——两端只有一份判断，
不会各自漂移。

来源常量复用 ``copixiv.core.models`` 里已有的 ``SOURCE_LOCAL`` / ``SOURCE_PIXIV``，
本模块不新增字符串字面量。
"""

from __future__ import annotations

from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV
from copixiv.core.sources import FALLBACK_EDITABLE_FIELDS, FallbackSourcePolicy
from copixiv.sources.local import LocalSourcePlugin
from copixiv.sources.pixiv import PixivSourcePlugin
from copixiv.sources.registry import SourceRegistry

_source_registry: SourceRegistry | None = None
_default_registry: SourceRegistry | None = None


def set_source_registry(registry: SourceRegistry | None) -> None:
    """Bind the active runtime :class:`SourceRegistry` for metadata policy lookups."""
    global _source_registry
    _source_registry = registry


def _get_registry() -> SourceRegistry:
    global _default_registry
    if _source_registry is not None:
        return _source_registry
    if _default_registry is None:
        _default_registry = SourceRegistry([PixivSourcePlugin(), LocalSourcePlugin()])
    return _default_registry


# 每个来源可编辑的字段。元组顺序 = 响应里 ``editable_fields`` 的顺序。
EDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    SOURCE_LOCAL: LocalSourcePlugin.manifest.editable_fields,
    SOURCE_PIXIV: PixivSourcePlugin().manifest.editable_fields,
}

# 仅本地书可编辑的字段：**从表派生**（local 有、fallback/pixiv 没有），不手写第二份清单。
# 顺序即 400 报错顺序——同一请求提交多个非法字段时，报的是这里最靠前的那个。
LOCAL_ONLY_FIELDS: tuple[str, ...] = tuple(
    field for field in EDITABLE_FIELDS[SOURCE_LOCAL] if field not in FALLBACK_EDITABLE_FIELDS
)

FIELD_LABELS = {
    "title": "标题",
    "author_id": "作者",
    "series_id": "系列",
}

# ``author_id`` 的理由与另外两个刻意不同：title / series_id 是"会被来源覆盖"，
# 而作者不是覆盖问题——来源压根不覆盖它，是"身份以来源为准"，修正入口是同步。
AUTHOR_LOCAL_ONLY_REASON = "作者仅本地书可编辑（pixiv 书的作者身份以来源为准，可用「同步」修正）"


def editable_fields(source: str | None) -> tuple[str, ...]:
    """该来源可编辑的字段；未知或空来源按保守 fallback 策略处理。"""
    if not source:
        return FallbackSourcePolicy.editable_fields
    return _get_registry().editable_fields(source)


def is_editable(source: str | None, field: str) -> bool:
    """*field* 在 *source* 的行上是否可编辑（编辑器与 PATCH 共用这一条）。"""
    return field in editable_fields(source)


def rejection_reason(field: str, source: str | None = None) -> str:
    """不可编辑字段提交时的 400 文案——按字段给，集中在这里。

    title / series_id 共用一个模板（同一个"来源会覆盖"的理由），
    author_id 单独一句（见 :data:`AUTHOR_LOCAL_ONLY_REASON`）。两句话都带
    「仅本地书可编辑」，前端按这句话做统一提示即可。
    """
    if field == "author_id":
        if source is None or source == SOURCE_PIXIV:
            return AUTHOR_LOCAL_ONLY_REASON
        return f"作者仅本地书可编辑（{source} 书的作者身份以来源为准，可用「同步」修正）"
    label = FIELD_LABELS.get(field, field)
    src_label = source or SOURCE_PIXIV
    return f"{label}仅本地书可编辑（{src_label} 来源会覆盖该字段）"


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
    "set_source_registry",
]
