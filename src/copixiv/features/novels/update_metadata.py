"""Use case: 编辑单本小说的元数据（卡片「编辑」按钮）。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §4（已拍板不做字段锁）/ §5（PATCH 语义与
校验表）。两条与文档旧假设不同的拍板（2026-09-18）：

* ``title`` / ``series_name`` **仅本地书**（``source='local'``）可改：pixiv
  来源会无条件覆盖这两列（``repo.py::_upsert_rows`` 的 ``update_fields_set``），
  放开编辑等于制造"改完又变回去"的假象。服务端**硬拒 400**——静默忽略更糟，
  前端会以为保存成功。
* ``caption`` 全局不可编辑，在 schema 层就被 ``extra="forbid"`` 挡掉（422）。

写路径纪律：只做**单行更新**（``SQLAlchemyNovelWriteRepository.update_metadata``），
**绝不走 ``_upsert_rows``**——那是来源刷新的写路径，带"来源权威"白名单与
"非空者胜"规则；混用会把用户编辑卷进来源语义（前置提交 b76e8cd 刚把两件事拆开）。

可编辑集合 = ``title`` / ``author_name`` / ``series_name`` / ``series_index`` /
``tags`` / ``create_time``。``like`` / ``view`` / ``text`` 是机器事实，只读展示；
``source`` / ``source_key`` / ``path`` / ``id`` / ``has_epub`` / ``caption``
提交即拒。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import Novel
from copixiv.core.services import parse_pixiv_time, parse_tags
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository

SOURCE_LOCAL = "local"

# 本地书的 ``author_name`` 永不为 NULL（LIBRARY_DESIGN.md §3.3）：解析不出写
# 「未知作者」。它同时是隔离 ``sync_empty_name`` 的关键——那个任务扫
# ``author_name IS NULL`` 的行、再拿 ``author_id`` 去 Pixiv 查名字。
UNKNOWN_AUTHOR = "未知作者"

MAX_TITLE = 300
MAX_AUTHOR_NAME = 200
MAX_SERIES_NAME = 300
MAX_TAGS = 50
MAX_TAG_LENGTH = 100

# 仅本地书可编辑的字段（原因见模块 docstring）。顺序即报错顺序。
LOCAL_ONLY_FIELDS = ("title", "series_name")
FIELD_LABELS = {"title": "标题", "series_name": "系列名"}


class UpdateNovelMetadataUseCase:
    """按"提交了什么就改什么"更新一行——PATCH 三态：未提交 / 给值 / 显式 null。

    调用方传 ``NovelUpdateRequest.model_dump(exclude_unset=True)``：键不出现 =
    不动，显式 ``null`` = 清空（``series_name`` / ``series_index`` /
    ``create_time`` / ``tags``）。
    """

    def __init__(
        self,
        novel_repo: SQLAlchemyNovelRepository,
        tag_repo: SQLAlchemyTagRepository,
    ):
        self._repo = novel_repo
        self._tags = tag_repo

    async def execute(self, novel_id: int, changes: dict[str, Any]) -> Novel:
        """应用 *changes*（只含客户端提交的键），返回更新后的域模型。

        Raises:
            NotFoundError: 小说不存在 → 404。
            ValidationError: 空 patch / 校验失败 / pixiv 书提交只读字段 → 400。
        """
        if not changes:
            raise ValidationError("没有要更新的字段")

        novel = await self._repo.get_by_id(novel_id)
        if novel is None:
            raise NotFoundError(f"Novel {novel_id} not found")

        values = self._build_values(novel, changes)
        await self._repo.update_metadata(novel_id, values)

        # 返回读路径的域模型：它已处理 ``is_special_follow`` 的 NULL 归一
        # （无作者行的本地书会走这条），HTTP 层直接拿它做 response_model 序列化。
        updated = await self._repo.get_by_id(novel_id)
        if updated is None:  # 并发删除的兜底——上面刚写过，实际不会发生
            raise NotFoundError(f"Novel {novel_id} not found")
        return updated

    # ------------------------------------------------------------------
    # 校验 / 归一
    # ------------------------------------------------------------------

    def _build_values(self, novel: Novel, changes: dict[str, Any]) -> dict[str, Any]:
        """逐字段校验并归一，返回可直接 setattr 的列值字典。"""
        is_local = novel.source == SOURCE_LOCAL

        # 先判"这个字段这本书能不能改"：pixiv 书的 title 即便传的是合法值也拒。
        for field in LOCAL_ONLY_FIELDS:
            if field in changes and not is_local:
                raise ValidationError(
                    f"{FIELD_LABELS[field]}仅本地书可编辑（pixiv 来源会覆盖该字段）"
                )

        values: dict[str, Any] = {}

        if "title" in changes:
            title = _text(changes["title"])
            if not title:
                raise ValidationError("标题不能为空")
            _check_length("标题", title, MAX_TITLE)
            values["title"] = title

        if "author_name" in changes:
            # 空串 / null 一律归一为「未知作者」——绝不落 NULL（见模块 docstring）。
            name = _text(changes["author_name"]) or UNKNOWN_AUTHOR
            _check_length("作者名", name, MAX_AUTHOR_NAME)
            values["author_name"] = name

        if "series_name" in changes:
            name = _text(changes["series_name"])
            _check_length("系列名", name, MAX_SERIES_NAME)
            values["series_name"] = name or None

        if "series_index" in changes:
            values["series_index"] = _series_index(changes["series_index"])

        if "tags" in changes:
            values["tags"] = self._normalize_tags(changes["tags"])

        if "create_time" in changes:
            values["create_time"] = _create_time(changes["create_time"])

        return values

    def _normalize_tags(self, raw: Any) -> list[str]:
        """与抓取路径同源归一：``parse_tags`` → ``tag_alias`` 映射 → 去重排序。

        为什么必须走 ``parse_tags``：库里的标签全部出自它（小写 + 去括号 +
        按 ``/|#、\\`` 切分），手工写入若不做同一套归一，立刻出现 ``R-18`` /
        ``r-18`` 这类大小写双胞胎，词表与别名建议一起退化
        （docs/NOVEL_EDIT_DESIGN.md §7）。别名映射让"用户输入的旧名"等价于
        抓取路径送来的旧名——两条写入路径对词表的贡献保持一致。

        写回是**整组替换**（编辑器所见即所得）；来源刷新的并集语义
        （``_upsert_rows`` 第 4 步，只增不减）不受影响：它只会往里加。
        """
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValidationError("标签必须是字符串数组")

        cleaned = parse_tags(raw)
        alias_map = self._tags.get_alias_map_sync()
        merged = sorted({alias_map.get(tag, tag) for tag in cleaned})

        if len(merged) > MAX_TAGS:
            raise ValidationError(f"标签过多（最多 {MAX_TAGS} 个）")
        for tag in merged:
            if len(tag) > MAX_TAG_LENGTH:
                raise ValidationError(
                    f"标签过长（单个最多 {MAX_TAG_LENGTH} 字）：{tag[:20]}…"
                )
        return merged


# ---------------------------------------------------------------------------
# 字段级校验助手（纯函数，便于单测）
# ---------------------------------------------------------------------------

def _text(raw: Any) -> str:
    """显式 null / 空串 / 纯空白 → ``""``；由调用方决定它表示"清空"还是"非法"。"""
    if raw is None:
        return ""
    return str(raw).strip()


def _check_length(label: str, value: str, limit: int) -> None:
    if len(value) > limit:
        raise ValidationError(f"{label}过长（最多 {limit} 字）")


def _series_index(raw: Any) -> int | None:
    """非负整数或 None（显式 null = 清空章节号）。"""
    if raw is None:
        return None
    # ``bool`` 是 ``int`` 的子类：True 不该被当成 1 悄悄收下。
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("章节号必须是整数")
    if raw < 0:
        raise ValidationError("章节号不能为负数")
    return raw


def _create_time(raw: Any) -> datetime | None:
    """``YYYY-MM-DD`` 或 ISO 时间 → tz-aware datetime；空 = 清空（NULL）。"""
    text = _text(raw)
    if not text:
        return None
    parsed = parse_pixiv_time(text)
    if parsed is None:
        raise ValidationError("日期无法解析（用 YYYY-MM-DD 或 ISO 时间）")
    return parsed
