"""Novel / batch-operation Pydantic schemas — carried with the novels feature.

Moved out of the former ``web_api/schemas.py`` as part of the S1 structure
simplification.  The v2 API is now unfrozen — response details can evolve
freely (kept in sync with the client bundle).
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel, ConfigDict, Field, computed_field, field_validator,
)

from copixiv.core.models import EpubStatus
from copixiv.features.novels import field_policy


# ---------------------------------------------------------------------------
# Novels
# ---------------------------------------------------------------------------

class NovelBase(BaseModel):
    id: int
    title: str
    author_id: int | None = None
    author_name: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0
    caption: str | None = None
    create_time: str | None = None
    has_epub: EpubStatus = EpubStatus.NO
    tags: list[str] = []
    is_favourite: int = 0
    is_special_follow: int = 0
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    # 行来源：``'pixiv'`` 抓取 / ``'local'`` 本地上传的 TXT / EPUB
    # （docs/LIBRARY_DESIGN.md §2）——前端据此在卡片上显示「本地」徽章。
    source: str = "pixiv"
    model_config = ConfigDict(from_attributes=True)

    # Coerce int columns from DB into proper types
    _coerce_has_epub = field_validator("has_epub", mode="before")(
        lambda v: EpubStatus(v) if v is not None else EpubStatus.NO
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def editable_fields(self) -> list[str]:
        """该行可编辑的字段清单 —— 由 ``source`` 派生（前端据此置灰输入框）。

        策略的唯一所有者在 ``features/novels/field_policy.py``：前端**不再**
        自己判断来源，两端共用这一份清单，避免各写一套后悄悄漂移。列表响应与
        PATCH 响应都带它（同一个 ``NovelBase``），前端拿到即可渲染。

        为什么是 ``computed_field`` 而不是普通字段：它是 ``source`` 的纯函数、
        不是数据库列，也不接受客户端提交；放在响应模型上等于把"能改什么"从
        服务端下发，而不是让客户端猜。
        """
        return list(field_policy.editable_fields(self.source))


class NovelListResponse(BaseModel):
    novels: list[NovelBase]
    cursor: dict | None = None
    # 首屏响应附带「当前搜索范围内是否存在被厌恶标签排除的小说」——
    # 前端据此决定 ExclusionBar（查看被隐藏的小说）是否显示；load-more
    # 与无关键词浏览不计算，保持默认 False。
    has_excluded: bool = False


# ---------------------------------------------------------------------------
# 元数据编辑（PATCH /api/novels/{novel_id}）—— docs/NOVEL_EDIT_DESIGN.md §5
# ---------------------------------------------------------------------------

def _reject_bool_id(value):
    """JSON ``true`` 不能变成"1 号作者 / 1 号系列"。

    Pydantic 宽松模式把 ``bool`` 收成 ``int``（``True`` → ``1``），而这两个 id
    是**身份**：静默错配到 1 号比报错严重得多。用例层还有一道同样的防御
    （``update_metadata.py::_author_id`` / ``_series_id``），这里拦住的是 HTTP
    路径。单本 PATCH 与批量编辑两个请求体**共用这一份规则**（各自用
    ``field_validator`` 挂上，不抄第二遍）。
    """
    if isinstance(value, bool):
        raise ValueError("id 必须是整数")
    return value


class NovelUpdateRequest(BaseModel):
    """单本元数据编辑的部分更新体。

    只列**可编辑**字段。结构性事实（``source`` / ``source_key`` / ``path`` /
    ``id`` / ``has_epub``）与任何未知字段一样，提交即被 ``extra="forbid"`` 拒掉
    （FastAPI 的请求体校验错误 → 422，与仓库既有约定一致；业务规则拒绝走
    ``ValidationError`` → 400）。

    ``title`` / ``author_id`` / ``series_id`` / ``caption`` 能否改由 **``source``
    决定**（仅本地书），这条业务规则在用例里执法并返回 400——schema 层表达不了
    "取决于行"的约束。四者的**理由与清单的唯一所有者**是
    ``features/novels/field_policy.py``，同一份策略由
    ``NovelBase.editable_fields`` 下发给前端；pixiv 书提交它们 → 400，错配的
    修正入口是 ``POST /api/novels/{id}/sync``（按 id 从来源重抓、恢复来源值）。

    ``caption`` 自 2026-09-22 从"全局不可编辑"改成"仅本地书可编辑"：原理由
    （来源刷新会覆盖、不在界面展示）通篇是拿 pixiv 书论证的，而本地书永不进
    ``_upsert_rows``、上传时 ``caption`` 写死 ``None``——填进去的就是用户自己的
    话，没有来源会来顶掉它。详见 ``field_policy.py`` 的模块 docstring。

    **作者与系列都是 id 而不是名字**（2026-09-19 拍板）：``author_name`` /
    ``series_name`` 已从可编辑集合移除，提交即 422；身份由 ``author_id`` /
    ``series_id`` 表达，显示名是它们的解析结果
    （``features/authors/resolve_names.py::lookup_author_profile``、
    ``features/novels/resolve_series.py::lookup_series_profile``）。这样上传的
    本地书才能真正挂到 pixiv 作者/系列名下——作者页、系列页、``author_id:`` /
    ``series_id:`` 搜索全部按 id 工作，而名字只是一份可刷新的缓存。

    "未提交"与"显式 null"必须可区分（PATCH 三态）：调用方用
    ``model_dump(exclude_unset=True)`` 取值——键不出现 = 不动，显式 ``null``
    = 清空（``series_index`` / ``create_time`` / ``caption`` 允许清空；
    ``author_id`` / ``series_id`` 显式 null = 解除关联，收尾见
    ``update_metadata.py::_series_columns``：作者保留显示名，系列连名字与章节号
    一起清，2026-09-22）。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    author_id: int | None = None
    series_id: int | None = None
    series_index: int | None = None
    tags: list[str] | None = None
    create_time: str | None = None
    # 仅本地书（pixiv 书的这一列是来源的展示性字段，在覆盖白名单里 → 400）。
    caption: str | None = None

    @field_validator("author_id", "series_id", mode="before")
    @classmethod
    def _reject_bool_ids(cls, value):
        """``true`` / ``false`` 不是 id —— 规则见 :func:`_reject_bool_id`。"""
        return _reject_bool_id(value)


class AuthorLookupResponse(BaseModel):
    """作者 ID 预览（``GET /api/novels/author/{author_id}``）。

    编辑器里填完 ID 就调它，好在保存*之前*告诉用户这个名字对不对；保存路径
    （PATCH）用同一个解析函数，所以预览与实际落库结果一致。

    ``source`` 是诚实性字段：``local`` = 库里已知（离线可用）、``pixiv`` =
    本次现查（说明网络通、作者存在）、``unknown`` = 拿不到名字。它**不影响
    可保存性**——id 才是身份，名字只是缓存，拿不到名字照样允许挂上去。
    """

    author_id: int
    author_name: str | None = None
    source: Literal["local", "pixiv", "unknown"] = "unknown"
    # 库内已有多少本挂在这个 id 下（拼错 id 的直觉校验：0 本 + 没名字 ≈ 可疑）
    novel_count: int = 0


class SeriesLookupResponse(BaseModel):
    """系列 ID 预览（``GET /api/novels/series/{series_id}``）—— 与作者那份同构。

    差别只在名字的来源链：``series`` 行 → 同系列的 novel 行 → pixiv
    ``novel_series``（前两步都是本地查询，见 ``resolve_series.py``）。
    """

    series_id: int
    series_name: str | None = None
    source: Literal["local", "pixiv", "unknown"] = "unknown"
    novel_count: int = 0


# ---------------------------------------------------------------------------
# Batch Download
# ---------------------------------------------------------------------------

class BatchDownloadRequest(BaseModel):
    keyword: str | None = None
    order_by: Literal["id", "like", "view", "text", "create_time", "random"] = "id"
    order_direction: Literal["ASC", "DESC"] = "DESC"
    min_like: int | None = None
    min_text: int | None = None
    limit: int = Field(default=500, ge=1)
    format_mode: Literal["txt", "prefer_epub"] = "txt"
    zip_name: str | None = None
    naming_template: str | None = None
    # Batch-mode selection scope — when ``novel_ids`` is set, the download
    # is restricted to exactly those novels (filters are ignored).
    novel_ids: list[int] | None = None
    # IDs to skip — only applies to the filter-based path (novel_ids=None).
    excluded_ids: list[int] | None = None


# ---------------------------------------------------------------------------
# Batch operations (delete / add_tags / remove_tags)
# ---------------------------------------------------------------------------

class BatchScope(BaseModel):
    """Which novels a batch operation applies to.

    Two modes mirror the frontend selection model:
    - ``ids``: an explicit list of selected novel IDs.
    - ``all_matched``: the full filter-matched set (optionally minus
      ``excluded_ids`` — the "select all, then uncheck a few" case).
    """

    mode: Literal["ids", "all_matched"] = "all_matched"
    novel_ids: list[int] = []
    keyword: str | None = None
    min_like: int | None = None
    min_text: int | None = None
    excluded_ids: list[int] = []


class BatchOperationRequest(BaseModel):
    operation: Literal["delete", "add_tags", "remove_tags"]
    scope: BatchScope
    tags: list[str] = []


class BatchOperationResponse(BaseModel):
    matched: int
    affected: int


class BatchMetadataRequest(BaseModel):
    """批量编辑「作者」/「系列」（``POST /api/novels/batch-metadata``）。

    与单本 ``NovelUpdateRequest`` 同一套三态：``author_id`` / ``series_id``
    **键不出现 = 不改这个字段**（调用方用 ``model_dump(exclude_unset=True)``
    取值，再映射到用例的 ``_UNSET``），**显式 ``null`` = 解除关联**
    （作者保留显示名并给空名行补「未知作者」；系列连名字与章节号一起清）。

    收尾语义与单本**完全一致**，唯一刻意的差异在**来源**上：pixiv 书不报 400，
    而是被跳过并计数（``applicable`` / ``skipped``）——批量的选择集天然是混合的，
    「全选匹配 + 挂作者」整体 400 会让功能完全不可用；"先报数再确认"里跳过的
    数量是显式字段，所以不存在"以为成功其实没写"的静默。论证见
    ``features/novels/batch_edit_metadata.py`` 模块 docstring。

    ``extra="forbid"``：结构性字段（``source`` / ``path`` / ``id`` …）与未知字段
    一律 422（与 PATCH 同一约定）；业务规则拒绝（空 patch / 非法 id）走用例的
    ``ValidationError`` → 400。``true`` 不是 id（``_reject_bool_id``）→ 422。
    """

    model_config = ConfigDict(extra="forbid")

    scope: BatchScope
    author_id: int | None = None
    series_id: int | None = None
    # 预演（默认）：只算数不写库，用户先看到"几本会改、几本会跳过"再确认。
    apply: bool = False

    @field_validator("author_id", "series_id", mode="before")
    @classmethod
    def _reject_bool_ids(cls, value):
        """``true`` / ``false`` 不是 id —— 规则见 :func:`_reject_bool_id`。"""
        return _reject_bool_id(value)


class BatchMetadataResponse(BaseModel):
    """批量编辑的计数（预演与落库同一形状）。

    ``matched`` = 调用方给的整个选择集；``applicable`` = 其中本地书（**会**被改
    的那些）；``skipped = matched - applicable``（pixiv 书——跳过不是错误）；
    ``affected`` = 实际被 UPDATE 改动的行数（预演恒 0，重复提交同一请求第二次
    也是 0：WHERE 里有逐列的 ``IS DISTINCT FROM``）。
    """

    matched: int
    applicable: int
    skipped: int
    affected: int


# ---------------------------------------------------------------------------
# Batch-mode selection helpers (search is a picking surface — the selection
# itself is an ID set independent of the current filters)
# ---------------------------------------------------------------------------

class NovelIdsResponse(BaseModel):
    """Matching novel IDs for the 「全选匹配」 bulk-add action.

    *limit*-truncated when the match set is larger than the batch cap;
    ``truncated`` tells the frontend to warn the user.
    """

    ids: list[int]
    total: int
    truncated: bool


class NovelsByIdsRequest(BaseModel):
    novel_ids: list[int]


class NovelsByIdsResponse(BaseModel):
    novels: list[NovelBase]
    truncated: bool


class MatchIdsRequest(BaseModel):
    """Intersect a selection with the current search scope (scoped clear)."""

    novel_ids: list[int]
    keyword: str | None = None
    min_like: int | None = None
    min_text: int | None = None


class SortIdsRequest(BaseModel):
    """Order an explicit id list by a novel column — 「查看已选」排序."""

    novel_ids: list[int]
    order_by: Literal["id", "like", "text"]
    order_direction: Literal["ASC", "DESC"] = "DESC"


class MatchIdsResponse(BaseModel):
    matching_ids: list[int]
    truncated: bool


class BatchTaskResponse(BaseModel):
    """A batch operation enqueued into the background task system."""

    task_id: int
    matched: int


class BatchExportRequest(BaseModel):
    """Background-task export (large selections — the ZIP is built offline)."""

    novel_ids: list[int]
    format_mode: Literal["txt", "prefer_epub"] = "txt"
    zip_name: str | None = None
    naming_template: str | None = None


class BatchExportResponse(BaseModel):
    task_id: int
    matched: int
