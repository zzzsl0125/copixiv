"""Use case: 编辑单本小说的元数据（卡片「编辑」按钮）。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §4（已拍板不做字段锁）/ §5（PATCH 语义与
校验表）。三条拍板直接决定本模块的形态：

* ``title`` / ``author_id`` / ``series_id`` **仅本地书**（``source='local'``）
  可改（2026-09-22 起第三个也收掉）：三列都是**来源拥有的身份 / 权威值**，
  pixiv 书提交 → 服务端**硬拒 400**——静默忽略更糟，前端会以为保存成功。
  清单与 400 文案的唯一所有者在 ``field_policy.py``（同一份策略经
  ``NovelBase.editable_fields`` 下发前端）；错配的修正入口是
  ``POST /api/novels/{id}/sync``（``sync_novel.py``），不再靠手改。理由分两种：
  ``title`` / ``series_id`` 会被来源刷新顶回去（``repo.py::_upsert_rows`` 的
  ``update_fields_set``），放开编辑只是制造"改完又变回去"的假象——系列尤其明显：
  ``series_id`` 是"非空者胜"不会被顶掉，可它的名字下一轮就变了，于是 id 指向新系列、
  名字还是旧的；``author_id`` 则相反，它压根不会被来源顶掉，手改会永久地让
  「作者是谁」与来源不符。
* ``caption`` **仅本地书**可改（2026-09-22；此前是全局不可编辑）：它在
  ``_upsert_rows`` 的覆盖白名单里，所以 pixiv 书手改会被下一轮抓取顶掉
  （→ 400）；而本地书永不进那条写路径、上传时这一列写死 ``None``，填进去的
  就是用户自己的话。空串与显式 null 同义（都清成 ``NULL``，与 pixiv 的
  "没有简介就是 NULL"同一形态）——见 :func:`_caption`。
* **作者与系列编辑的都是 id，不是名字**（2026-09-19 拍板）。上传的本地书
  （``author_id`` / ``series_id`` 均为 NULL）因此才能真正挂到 pixiv 作者与系列名下：
  作者页、系列页、``author_id:`` / ``series_id:`` 搜索全部按 id 工作，而
  ``author_name`` / ``series_name`` 降级为**由 id 解析出来的显示名**
  （本地表 → pixiv 兜底）。名字解析失败不算失败——id 是身份、名字是缓存：此时写
  NULL（"待解析"），而名字待解析是**仓库认可的中间态**、**不是**"有任务会兜底"
  （``sync_empty_name`` 默认禁用：``scheduled_task.is_enabled`` 列默认 false，
  只有手动启用才跑）。会把它补回来的是**再抓一次该作品 / 作者**或下一次「同步」；
  系列名则由 ``update_summary`` 在下次抓到该系列时从小说行推导。
* **解除关联（显式 null）时两者刻意不同**（2026-09-22 修）：作者保留显示名——它在
  上传时来自文件名解析，有独立来源；系列连名字与章节号一起清——本地书的系列名只
  可能由 id 解析而来（上传时是 NULL），留着它就是一串"点不动的系列名"，而名字本身
  提交即 422，用户没有任何出口能清掉。见 :meth:`_series_columns`。

写路径纪律：只做**单行更新**（``SQLAlchemyNovelWriteRepository.update_metadata``），
**绝不走 ``_upsert_rows``**——那是来源刷新的写路径，带"来源权威"白名单与
"非空者胜"规则；混用会把用户编辑卷进来源语义（前置提交 b76e8cd 刚把两件事拆开）。

可编辑集合 = ``field_policy.EDITABLE_FIELDS``（local：``title`` / ``author_id`` /
``series_id`` / ``series_index`` / ``tags`` / ``create_time`` / ``caption``；
pixiv 只留 ``series_index`` / ``tags`` / ``create_time``）。
``author_name`` / ``series_name`` 只作为 id 的派生物被写；``like`` / ``view`` /
``text`` 是机器事实，只读展示；``source`` / ``source_key`` / ``path`` / ``id`` /
``has_epub`` 提交即拒。

``series_index`` 的口径是**正整数或空**（2026-09-22 统一）：``0`` 与 NULL 一样表示
"没有章节号"，所以 0 提交即 400（detail 正好是 :data:`SERIES_INDEX_POSITIVE`），
负数 / 非整数 / 布尔的既有拒绝保持不变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.models import Novel
from copixiv.core.services import parse_pixiv_time, parse_tags
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.resolve_names import lookup_author_profile
from copixiv.features.novels import field_policy
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, SQLAlchemySeriesRepository
from copixiv.features.novels.resolve_series import lookup_series_profile
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.pixiv.client import PixivClient

# 本地书的 ``author_name`` 在上传时永不为 NULL（LIBRARY_DESIGN.md §3.3）：解析
# 不出写「未知作者」。它是**产品承诺**「每本书都有作者名可显示」的落点——这条
# 承诺不外包给任何任务（``sync_empty_name`` 默认禁用，见 §7）。解除作者关联时
# 若名字仍是 NULL，会被补回本值（见 :meth:`_author_columns` 的守卫）。
UNKNOWN_AUTHOR = "未知作者"

MAX_TITLE = 300
MAX_TAGS = 50
MAX_TAG_LENGTH = 100
# 简介上限。列是 ``Text``（无长度约束），这里给的是**产品口径**而不是数据库
# 约束：pixiv 的 caption 本来就能装下几千字的长简介，卡太死会把"从别处粘一段
# 简介"的正常用法挡在门外；2000 足够，也拦得住误粘一整篇正文。
MAX_CAPTION = 2000

# 章节号的取值口径：正整数或空（0 = 无章节号，与 NULL 同义）。写侧由本模块执法
# （0 提交即 400），读侧由命名模板执法（``core/services.py``：0 与 None 一样让
# token 消失）。``fix_series_index`` 按名册位置从 1 开始编号、不会写 0（补 NULL +
# 纠正错值 + 孤儿分支，另见 ``sweep_all`` 全量复查）；而且它默认禁用
# （``scheduled_task.is_enabled`` 列默认 false）——别把它当兜底。
SERIES_INDEX_POSITIVE = "章节号必须是正整数"


class UpdateNovelMetadataUseCase:
    """按"提交了什么就改什么"更新一行——PATCH 三态：未提交 / 给值 / 显式 null。

    调用方传 ``NovelUpdateRequest.model_dump(exclude_unset=True)``：键不出现 =
    不动，显式 ``null`` = 清空（``series_index`` / ``create_time`` / ``tags`` /
    ``author_id`` / ``series_id``）。两个 id 的差别只在"名字怎么收尾"：
    ``series_id = null`` 连名字与章节号一起清，``author_id = null`` 保留显示名
    ——原因见 :meth:`_series_columns` 与 :meth:`_author_columns`。
    """

    def __init__(
        self,
        novel_repo: SQLAlchemyNovelRepository,
        tag_repo: SQLAlchemyTagRepository,
        author_repo: SQLAlchemyAuthorRepository,
        series_repo: SQLAlchemySeriesRepository,
        client: PixivClient | None = None,
    ):
        self._repo = novel_repo
        self._tags = tag_repo
        self._authors = author_repo
        self._series = series_repo
        # 可选：为 None 时名字只能来自本地表（测试与离线场景）。
        self._client = client

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

        # 作者与系列是仅有的"先解析再落库"的字段（其余全是纯函数归一）：解析要读
        # 本地表、可能还要问一次 Pixiv，所以它们不在 _build_values 里。
        resolved_author: tuple[int, str] | None = None
        if "author_id" in changes:
            author_values, resolved_author = await self._author_columns(
                changes["author_id"], novel,
            )
            values.update(author_values)

        resolved_series: tuple[int, str] | None = None
        if "series_id" in changes:
            series_values, resolved_series = await self._series_columns(
                changes["series_id"],
            )
            values.update(series_values)

        await self._repo.update_metadata(novel_id, values)

        # 作者行本身也要有名字（`ensure_exists` 刚建的占位行是 NULL），并把
        # 该名字规范到同作者的全部 novel 行——`update_author_name` 两个都做，
        # 且用 `IS DISTINCT FROM` 过滤掉无变化的行（刚写的这一行就在其中）。
        if resolved_author is not None:
            await self._authors.update_author_name(*resolved_author)

        # 系列只写"这一个系列行"的名字：系列名的规范方向是 novel → series
        # （`update_summary` 取 max(novel.series_name)），改写同系列其它
        # 小说行既没必要也会覆盖来源值。见 `update_series_name` 的注释。
        if resolved_series is not None:
            await self._series.update_series_name(*resolved_series)

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
        # 先判"这个字段这本书能不能改"：pixiv 书的只读字段即便传的是合法值也拒。
        # 迭代 ``LOCAL_ONLY_FIELDS`` 而不是请求体的键顺序——报错顺序是契约的一部分
        # （同一请求提交多个非法字段时，报的是清单里最靠前的那个）。
        # 判定本身走 ``is_editable``：清单将来若变动（某字段在 pixiv 放开），
        # 这里不用跟着改。
        for field in field_policy.LOCAL_ONLY_FIELDS:
            if field in changes and not field_policy.is_editable(
                novel.source, field,
            ):
                raise ValidationError(field_policy.rejection_reason(field))

        values: dict[str, Any] = {}

        if "title" in changes:
            title = _text(changes["title"])
            if not title:
                raise ValidationError("标题不能为空")
            _check_length("标题", title, MAX_TITLE)
            values["title"] = title

        if "series_index" in changes:
            values["series_index"] = _series_index(changes["series_index"])

        if "caption" in changes:
            values["caption"] = _caption(changes["caption"])

        if "tags" in changes:
            values["tags"] = self._normalize_tags(changes["tags"])

        if "create_time" in changes:
            values["create_time"] = _create_time(changes["create_time"])

        return values

    async def _author_columns(
        self, raw: Any, novel: Novel,
    ) -> tuple[dict[str, Any], tuple[int, str] | None]:
        """``author_id`` 提交值 → 列值 + 需要写回作者行的 ``(id, 名字)``。

        三态与三种结果：

        * 显式 ``null``（解除关联）→ 写 ``author_id=NULL``；**显示名保持不动**
          ——本地书的名字来自文件名解析，把它一起清掉等于凭空丢信息。而
          「每本书都有作者名可显示」是产品承诺（``docs/LIBRARY_DESIGN.md``
          §3.3）：没有名字的行就是这条承诺破了的形状。唯一的例外：名字本来
          就是 NULL（上一轮"解析失败"留下的中间态）时补回「未知作者」——让
          这一行回到"有名字可显示"的终态，而不是停在待解析。
        * 给了 id、解析出名字 → 写 ``author_id`` + ``author_name``，并返回名字
          供 ``update_author_name`` 写进 ``author`` 行与同作者其它行。
        * 给了 id、解析不出名字 → 仍然写 ``author_id``，``author_name`` 落
          ``NULL``（"待解析"）。**这不是失败**：id 才是身份，名字是缓存。这个
          中间态是仓库认可的终态之一——**别把它说成"有兜底任务"**：
          ``sync_empty_name`` 扫的确实是这种形状（``author_name IS NULL`` 且
          ``author_id`` 可用），但它默认禁用（``scheduled_task.is_enabled`` 列
          默认 false，只有手动启用才跑）；会把名字补回来的是**再抓一次该作品 /
          作者**或下一次「同步」。见模块 docstring。
        """
        if raw is None:
            # 「每本书都有作者名可显示」（LIBRARY_DESIGN.md §3.3）是产品承诺，
            # 这里只是**纵深防御**：不变量由承诺 + 编辑器补写共同守——上传路径
            # 已经保证了它，这一句兜住"解析失败后解除关联"这条中间态。
            values: dict[str, Any] = {"author_id": None}
            if novel.author_name is None:
                values["author_name"] = UNKNOWN_AUTHOR
            return values, None

        author_id = _author_id(raw)

        # 外键安全：``novel.author_id → author.author_id``，先建占位行
        # （``ON CONFLICT DO NOTHING``，已有名字的行不受影响）。
        self._authors.ensure_exists({author_id})

        profile = await lookup_author_profile(
            author_id, session=self._authors.session, client=self._client,
        )
        values = {"author_id": author_id, "author_name": profile.author_name}
        resolved = (
            (author_id, profile.author_name) if profile.author_name else None
        )
        return values, resolved

    async def _series_columns(
        self, raw: Any,
    ) -> tuple[dict[str, Any], tuple[int, str] | None]:
        """``series_id`` 提交值 → 列值 + 需要写回系列行的 ``(id, 名字)``。

        与作者同构，只有两处刻意的不同：

        * **解除关联的收尾不同**。作者那边"id 为空 ⟹ 名字非空"是一条不变量
          （LIBRARY_DESIGN.md §3.3「每本书都有作者名可显示」），所以解除关联时
          名字保留、必要时补「未知作者」；系列没有这条不变量，而且本地书的系列名
          没有独立来源（上传时是 NULL，只可能由 id 解析而来）——留着它就是一个
          用户清不掉的孤儿显示名，所以解除关联时**连名字与章节号一起清**。
        * 解析链更长（系列行 → 同系列小说行 → pixiv ``novel_series``），
          因为 ``series`` 行的名字是从小说行推导出来的，可能还没写上。
        """
        if raw is None:
            # 解除关联 = 清掉整个"系列三元组"：id 是身份，名字是它的解析结果，
            # 章节号是它在系列里的位置——三者描述同一个事实，一起进一起出。
            #
            # 名字**不能**像作者那样留着（2026-09-22 修）：``author_name`` 在上传
            # 时来自文件名解析，有独立来源，所以解除作者关联后它仍是这本书的展示
            # 信息；本地书的 ``series_name`` 上传时是 NULL，只可能由 ``series_id``
            # 解析而来，留着它就只剩一条"点不动的系列名"——而 ``series_name``
            # 提交即 422（名字不可编辑），用户没有任何出口能把它清掉。
            # 章节号同理：脱离系列之后"第几本"没有指代对象。本地书没有来源，
            # 这一列不会被来源刷新补回；``fix_series_index`` 的孤儿分支虽然会扫
            # ``series_id IS NULL`` 且号非空的行，但那要问一次 webview——本地书
            # 没有 pixiv id、取不到响应，会被原样留着；而且该任务默认禁用。
            return {
                "series_id": None,
                "series_name": None,
                "series_index": None,
            }, None

        series_id = _series_id(raw)

        # 外键安全：``novel.series_id → series.series_id``，先建占位行。
        self._series.ensure_exists({series_id})

        profile = await lookup_series_profile(
            series_id, session=self._series.session, client=self._client,
        )
        values = {"series_id": series_id, "series_name": profile.series_name}
        resolved = (
            (series_id, profile.series_name) if profile.series_name else None
        )
        return values, resolved

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


def _caption(raw: Any) -> str | None:
    """简介：去首尾空白；空串与显式 null 一起落成 ``NULL``（清空）。

    为什么空串要归一成 ``NULL``：库里"没有简介"本来就**已经有两副面孔**——
    pixiv 抓取路径在作品没有简介时落的是空串（``features/novels/ingest.py``
    的 ``safe_get(n, "caption", "")``），而本地书上传路径写死 ``None``
    （``features/library/ingest.py``）。编辑器这一侧不必再贡献第三种写法：
    用户清空简介时给 ``NULL``，读侧一律 ``caption or ''`` 即可（前端现在就是
    这么渲染的）。**既有行的空串不回填**——这是写入侧的口径，不是数据迁移。

    长度上限是**产品口径**（见 :data:`MAX_CAPTION`）：列本身是 ``Text``，
    这里拒的是"误粘一整篇正文"，不是数据库装不下。
    """
    text = _text(raw)
    if not text:
        return None
    _check_length("简介", text, MAX_CAPTION)
    return text


def _series_index(raw: Any) -> int | None:
    """正整数或空（显式 null = 清空章节号）。

    ``0`` 被当作"没有章节号"而不是"第 0 本"（2026-09-22 统一口径）：来源的
    ``guess_series_order`` 在拿到 ``nextNovel.contentOrder == 1`` 时会算出 0，
    存进库里就是一个看起来像"第 0 本"的假值；命名模板同样把 0 与 None 一样
    处理（``core/services.py``）。所以这一层明确拒绝 0，把歧义挡在写入侧。
    """
    if raw is None:
        return None
    # ``bool`` 是 ``int`` 的子类：True 不该被当成 1 悄悄收下。
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("章节号必须是整数")
    if raw < 0:
        raise ValidationError("章节号不能为负数")
    if raw == 0:
        # 文案是契约（前端按 detail 提示），也与负数那句刻意分开：
        # "0 不是没写对类型，是不该用 0 表示无章节号"。
        raise ValidationError(SERIES_INDEX_POSITIVE)
    return raw


def _author_id(raw: Any) -> int:
    """正整数 pixiv 作者 id（显式 null 由调用方在更早处处理）。"""
    return _positive_id(raw, "作者", "pixiv 用户 ID")


def _series_id(raw: Any) -> int:
    """正整数 pixiv 系列 id（显式 null 由调用方在更早处处理）。"""
    return _positive_id(raw, "系列", "pixiv 系列 ID")


def _positive_id(raw: Any, label: str, hint: str) -> int:
    """正整数 pixiv id —— ``author_id`` 与 ``series_id`` 共用的那一条规则。

    为什么必须是 ``int`` 而不是"能转成数字的字符串"：这两条字段都是**身份**，
    不是展示值——前端的名字预览、作者页 / 系列页跳转、``author_id:`` /
    ``series_id:`` 搜索全按它们工作。Pydantic 的 ``int`` 字段已经把 ``"123"``
    收成 ``123``（宽松模式），这里只拦下"解析后仍然不是整数"（``true`` /
    浮点）与无意义的 0 / 负数。

    上界用 BIGINT：列是 ``BigInteger``，超出会在驱动层报错，不如在这里给 400。
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError(f"{label} ID 必须是整数")
    if raw <= 0:
        raise ValidationError(f"{label} ID 必须是正整数（{hint}）")
    if raw > 2**63 - 1:
        raise ValidationError(f"{label} ID 超出取值范围")
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
