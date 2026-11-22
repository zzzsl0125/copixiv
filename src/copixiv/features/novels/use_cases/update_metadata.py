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
* **作者与系列编辑的都是 id，不是名字**（2026-09-19 拍板）。上传的本地书
  （``author_id`` / ``series_id`` 均为 NULL）因此才能真正挂到 pixiv 作者与系列名下：
  作者页、系列页、``author_id:`` / ``series_id:`` 搜索全部按 id 工作，而
  ``author_name`` / ``series_name`` 降级为**由 id 解析出来的显示名**
  （本地表 → pixiv 兜底）。名字解析失败不算失败——id 是身份、名字是缓存：此时写
  NULL（"待解析"），而名字待解析是**仓库认可的中间态**。会把它补回来的是
  **再抓一次该作品 / 作者**或下一次「同步」；系列名则由 ``update_summary``
  在下次抓到该系列时从小说行推导。
* **解除关联（显式 null）时两者刻意不同**（2026-09-22 修）：作者保留显示名——它在
  上传时来自文件名解析，有独立来源；系列连名字与章节号一起清——本地书的系列名只
  可能由 id 解析而来（上传时是 NULL），留着它就是一串"点不动的系列名"，而名字本身
  提交即 422，用户没有任何出口能清掉。见 :func:`_series_columns`。

写路径纪律：只做**单行更新**（``SQLAlchemyNovelWriteRepository.update_metadata``），
**绝不走 ``_upsert_rows``**——那是来源刷新的写路径，带"来源权威"白名单与
"非空者胜"规则；混用会把用户编辑卷进来源语义（前置提交 b76e8cd 刚把两件事拆开）。

可编辑集合 = ``field_policy.EDITABLE_FIELDS``（local：``title`` / ``author_id`` /
``series_id`` / ``series_index`` / ``tags`` / ``create_time``；
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
from copixiv.core.models import UNKNOWN_AUTHOR
from copixiv.core.models import author_id as _author_id
from copixiv.core.models import series_id as _series_id
from copixiv.core.text import MAX_TAG_LENGTH, parse_tags, validate_tags
from copixiv.core.time import parse_pixiv_time
from copixiv.db.base import ensure_parent_rows
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.service import lookup_author_profile
from copixiv.features.novels import field_policy
from copixiv.features.novels.repo import SQLAlchemyNovelRepository, StoredNovel
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.series.service import lookup_series_profile
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.pixiv.client import PixivClient

# 本地书的 ``author_name`` 在上传时永不为 NULL（LIBRARY_DESIGN.md §3.3）：解析
# 不出写「未知作者」。它是**产品承诺**「每本书都有作者名可显示」的落点——这条
# 承诺不外包给任何后台任务。解除作者关联时若名字仍是 NULL，会被补回本值
# （见 :func:`_author_columns` 的守卫）。

MAX_TITLE = 300
MAX_TAGS = 50

# 章节号的取值口径：正整数或空（0 = 无章节号，与 NULL 同义）。写侧由本模块执法
# （0 提交即 400），读侧由命名模板执法（``core/services.py``：0 与 None 一样让
# token 消失）。``fix_series_index`` 按名册位置从 1 开始编号、不会写 0（仅补
# 缺失章节号并顺带纠正同系列错值）；而且它默认禁用
# （``scheduled_task.is_enabled`` 列默认 false）——别把它当兜底。
SERIES_INDEX_POSITIVE = "章节号必须是正整数"


async def update_novel_metadata(
    novel_id: int,
    changes: dict[str, Any],
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None = None,
) -> StoredNovel:
    """按"提交了什么就改什么"更新一行——PATCH 三态：未提交 / 给值 / 显式 null。

    调用方传 ``NovelUpdateRequest.model_dump(exclude_unset=True)``：键不出现 =
    不动，显式 ``null`` = 清空（``series_index`` / ``create_time`` / ``tags`` /
    ``author_id`` / ``series_id``）。两个 id 的差别只在"名字怎么收尾"：
    ``series_id = null`` 连名字与章节号一起清，``author_id = null`` 保留显示名
    ——原因见 :func:`_series_columns` 与 :func:`_author_columns`。

    DB reads and writes run off the event loop via :func:`read_transaction`
    and :func:`run_write_transaction`; Pixiv lookups never hold an open
    DB session or transaction.

    Raises:
        NotFoundError: 小说不存在 → 404。
        ValidationError: 空 patch / 校验失败 / pixiv 书提交只读字段 → 400。
    """
    if not changes:
        raise ValidationError("没有要更新的字段")

    def _prepare(tx: SqlUnitOfWork) -> tuple[StoredNovel, dict[str, Any]]:
        novel_repo = SQLAlchemyNovelRepository(tx.session)
        novel = novel_repo.get_by_id(novel_id)
        if novel is None:
            raise NotFoundError(f"Novel {novel_id} not found")
        tag_repo = SQLAlchemyTagRepository(tx.session)
        return novel, _build_values(novel, changes, tag_repo)

    novel, values = await read_transaction(uow, _prepare)

    # 作者与系列是仅有的"先解析再落库"的字段（其余全是纯函数归一）：解析要读
    # 本地表、可能还要问一次 Pixiv，所以它们不在 _build_values 里。
    resolved_author: tuple[int, str] | None = None
    if "author_id" in changes:
        author_values, resolved_author = await _author_columns(
            changes["author_id"],
            novel,
            uow=uow,
            client=client,
        )
        values.update(author_values)

    resolved_series: tuple[int, str] | None = None
    if "series_id" in changes:
        series_values, resolved_series = await _series_columns(
            changes["series_id"],
            uow=uow,
            client=client,
        )
        values.update(series_values)

    def _commit(tx: SqlUnitOfWork) -> StoredNovel:
        novel_repo = SQLAlchemyNovelRepository(tx.session)
        author_repo = SQLAlchemyAuthorRepository(tx.session)
        series_repo = SQLAlchemySeriesRepository(tx.session)

        current = novel_repo.get_by_id(novel_id)
        if current is None:
            raise NotFoundError(f"Novel {novel_id} not found")

        ensure_parent_rows(
            tx.session,
            author_ids=(
                {values["author_id"]} if "author_id" in values and values["author_id"] is not None else ()
            ),
            series_ids=(
                {values["series_id"]} if "series_id" in values and values["series_id"] is not None else ()
            ),
        )

        novel_repo.update_metadata(novel_id, values)

        if resolved_author is not None:
            author_repo.update_author_name(*resolved_author)
        if resolved_series is not None:
            series_repo.update_series_name(*resolved_series)

        updated = novel_repo.get_by_id(novel_id)
        if updated is None:  # 并发删除的兜底——上面刚写过，实际不会发生
            raise NotFoundError(f"Novel {novel_id} not found")
        return updated

    return await run_write_transaction(uow, _commit)


# ------------------------------------------------------------------
# 校验 / 归一
# ------------------------------------------------------------------


def _build_values(
    novel: StoredNovel,
    changes: dict[str, Any],
    tag_repo: SQLAlchemyTagRepository,
) -> dict[str, Any]:
    """逐字段校验并归一，返回可直接 setattr 的列值字典。"""
    # 先判"这个字段这本书能不能改"：pixiv 书的只读字段即便传的是合法值也拒。
    # 迭代 ``LOCAL_ONLY_FIELDS`` 而不是请求体的键顺序——报错顺序是契约的一部分
    # （同一请求提交多个非法字段时，报的是清单里最靠前的那个）。
    # 判定本身走 ``is_editable``：清单将来若变动（某字段在 pixiv 放开），
    # 这里不用跟着改。
    for field in field_policy.LOCAL_ONLY_FIELDS:
        if field in changes and not field_policy.is_editable(
            novel.source,
            field,
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

    if "tags" in changes:
        values["tags"] = _normalize_tags(changes["tags"], tag_repo)

    if "create_time" in changes:
        values["create_time"] = _create_time(changes["create_time"])

    return values


async def _author_columns(
    raw: Any,
    novel: StoredNovel,
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None,
) -> tuple[dict[str, Any], tuple[int, str] | None]:
    """``author_id`` 提交值 → 列值 + 需要写回作者行的 ``(id, 名字)``。"""
    if raw is None:
        values: dict[str, Any] = {"author_id": None}
        if novel.author_name is None:
            values["author_name"] = UNKNOWN_AUTHOR
        return values, None

    author_id = _author_id(raw)

    profile = await lookup_author_profile(
        author_id,
        uow=uow,
        client=client,
    )
    values = {"author_id": author_id, "author_name": profile.author_name}
    resolved = (author_id, profile.author_name) if profile.author_name else None
    return values, resolved


async def _series_columns(
    raw: Any,
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None,
) -> tuple[dict[str, Any], tuple[int, str] | None]:
    """``series_id`` 提交值 → 列值 + 需要写回系列行的 ``(id, 名字)``。"""
    if raw is None:
        return {
            "series_id": None,
            "series_name": None,
            "series_index": None,
        }, None

    series_id = _series_id(raw)

    profile = await lookup_series_profile(
        series_id,
        uow=uow,
        client=client,
    )
    values = {"series_id": series_id, "series_name": profile.series_name}
    resolved = (series_id, profile.series_name) if profile.series_name else None
    return values, resolved


def _normalize_tags(raw: Any, tag_repo: SQLAlchemyTagRepository) -> list[str]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValidationError("标签必须是字符串数组")

    cleaned = parse_tags(raw)
    alias_map = tag_repo.get_alias_map()
    merged = sorted({alias_map.get(tag, tag) for tag in cleaned})

    if len(merged) > MAX_TAGS:
        raise ValidationError(f"标签过多（最多 {MAX_TAGS} 个）")
    validate_tags(merged, max_length=MAX_TAG_LENGTH)
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


def _create_time(raw: Any) -> datetime | None:
    """``YYYY-MM-DD`` 或 ISO 时间 → tz-aware datetime；空 = 清空（NULL）。"""
    text = _text(raw)
    if not text:
        return None
    parsed = parse_pixiv_time(text)
    if parsed is None:
        raise ValidationError("日期无法解析（用 YYYY-MM-DD 或 ISO 时间）")
    return parsed
