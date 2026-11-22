"""用例：批量编辑本地书的「作者」与「系列」—— 混合选择集上的 set-based 元数据写。

契约与拍板依据（与单本 ``PATCH /api/novels/{id}`` 逐条对照，差异都是**刻意**的）：

**① 三态：模块级哨兵 ``_UNSET`` / 给值 / 显式 ``None``。** 与单本 PATCH 同一套
思路（``NovelUpdateRequest.model_dump(exclude_unset=True)`` 区分"键不出现"与
"显式 null"），只是批量这边调用点没有 Pydantic 可用，所以哨兵下沉到用例签名：
``author_id=_UNSET`` 表示"这次不动作者"，``author_id=None`` 表示"解除关联"。
两者的收尾语义与单本**完全一致**（见 :func:`execute` 与
``update_metadata.py::_author_columns`` / ``_series_columns``）：

* 给 id：写 ``author_id`` / ``series_id``，名字由 id **解析**出来一起写；
* ``author_id = None``：只置 NULL，**显示名保留不动**，仅给
  ``author_name IS NULL`` 的行补「未知作者」（LIBRARY_DESIGN.md §3.3
  「每本书都有作者名可显示」在批量里的退化形态——单本是"读这一行的现值再分支"，
  批量里没有那一行可读，于是条件搬进 WHERE）；
* ``series_id = None``：``series_id`` / ``series_name`` / ``series_index``
  三列一起清（本地书的系列名没有独立来源，留着就是用户清不掉的孤儿显示名）。

**② pixiv 书不是错误，是跳过——这是与单本 PATCH 唯一且刻意的差异。** 单本 PATCH
对 pixiv 书提交 ``author_id`` / ``series_id`` 是**硬拒 400**
（``field_policy.rejection_reason``：静默忽略更糟，前端会以为保存成功）。批量这条
规则必须换形态，理由只有一个：用户面对的是**混合**选择集。「全选匹配 +
挂作者」如果整体 400，这个功能就完全不可用（书架上永远混着 pixiv 书）。改成
"预先报数 + 跳过"之后，用户先看到「304 篇本地书将被修改、8 篇 pixiv 书跳过」
再确认，**不存在**"以为成功其实没写"的静默——被跳过的数量是响应里的显式字段
（``skipped``），与 400 一样是**可见**的；两者的差别只是"整批拒绝"还是"报数后
继续"，而不是"说"与"不说"。

**③ 计数契约。** ``matched`` = 调用方给的整个选择集（去重后的
``novel_ids``），``applicable`` = 其中**本地书**的数量（走
``filter_ids_by_source(..., {SOURCE_LOCAL})``，来源常量取自
``copixiv.core.models``），``skipped = matched - applicable``，
``affected`` = 本次真正被 UPDATE 改动的行数（预演恒为 0）。四个键**恒定存在**
——HTTP 层不需要按模式分支取值。

**④ 为什么不能直接复用 ``_author_columns`` / ``_series_columns``。** 那两个方法是
**单行**语义：读一本书的现值（``novel.author_name``）、按它分支、拼出**一行**的列值
再由 ``update_metadata`` 写一行。批量是"一个值写 N 行"，没有"这一行"可读，逐行复用
就等于 N 次 ``get_by_id`` + N 次 ``setattr``（还各自打一次名字解析）——正是本功能
要避免的形态。所以这里只借用它们的**规则**（``_author_id`` / ``_series_id`` 的
校验与 400 文案、``UNKNOWN_AUTHOR`` 常量、名字解析函数），落库走
``repo.update_metadata_many``：一条 set-based UPDATE，语句级触发器只跑一次。

**⑤ 解析只做一次。** 同一个 id 写 N 本：``lookup_author_profile`` /
``lookup_series_profile`` **各解析一次**（本地表优先、pixiv 兜底、永不抛），这正是
批量相对单本省下来的 N-1 次解析。解析失败**不算失败**：id 是身份、名字是缓存，
此时落 ``author_name = NULL``（"待解析"，仓库认可的中间态——``sync_empty_name``
默认禁用，别把它当兜底）。

写路径纪律：与单本一致，**绝不走 ``_upsert_rows``**（那是来源刷新的写路径，带
"来源权威"覆盖白名单与"非空者胜"规则，见 ``update_metadata.py`` 模块 docstring）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from copixiv.core.exceptions import ValidationError
from copixiv.core.models import SOURCE_LOCAL, UNKNOWN_AUTHOR
from copixiv.core.models import author_id as _author_id
from copixiv.core.models import series_id as _series_id
from copixiv.db.base import ensure_parent_rows
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.service import lookup_author_profile
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.series.resolve import lookup_series_profile
from copixiv.pixiv.client import PixivClient


class _Unset:
    """「这个字段这次没提交」的哨兵类型 —— 与显式 ``None``（解除关联）不同。

    用类型而不是裸 ``object()``：:func:`batch_edit_metadata` 的签名要能写出
    ``int | None | _Unset``，而且调试时 ``repr`` 直接说明三态里的哪一态。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只影响日志可读性
        return "<UNSET>"


#: 三态里的"未提交"：键不出现 = 不动这个字段（等于 PATCH 的 exclude_unset）。
_UNSET = _Unset()


class BatchMetadataCounts(TypedDict):
    """批量编辑的计数（预演与落库同一形状，``affected`` 预演恒为 0）。"""

    matched: int
    applicable: int
    skipped: int
    affected: int


async def batch_edit_metadata(
    eligible_ids: list[int],
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None = None,
    author_id: int | None | _Unset = _UNSET,
    series_id: int | None | _Unset = _UNSET,
    apply: bool = False,
) -> BatchMetadataCounts:
    """把"一个作者 id / 一个系列 id"写到一批**本地书**上（计数 ``apply=False`` 或落库 ``apply=True``）。

    与 :func:`~copixiv.features.novels.use_cases.update_metadata.update_novel_metadata`
    同构（同样的仓库装配、同样的三态、同样的解析链），差别全在"批量"这两个字上：
    选择集先按来源收窄、pixiv 书跳过而不是 400、解析只做一次、落库是一条 UPDATE。
    """
    if apply and author_id is _UNSET and series_id is _UNSET:
        raise ValidationError("没有要修改的字段")
    if not eligible_ids:
        raise ValidationError("请先勾选要修改的小说")

    if author_id is not _UNSET and author_id is not None:
        author_id = _author_id(author_id)
    if series_id is not _UNSET and series_id is not None:
        series_id = _series_id(series_id)

    applicable = await read_transaction(
        uow,
        lambda tx: SQLAlchemyNovelRepository(tx.session).filter_ids_by_source(
            eligible_ids,
            {SOURCE_LOCAL},
        ),
    )
    counts = BatchMetadataCounts(
        matched=len(eligible_ids),
        applicable=len(applicable),
        skipped=len(eligible_ids) - len(applicable),
        affected=0,
    )

    if not apply or not applicable:
        return counts

    values: dict[str, Any] = {}
    resolved_author: tuple[int, str] | None = None
    resolved_series: tuple[int, str] | None = None
    backfill_unknown_author = False

    if isinstance(author_id, int):
        author_profile = await lookup_author_profile(
            author_id,
            uow=uow,
            client=client,
        )
        values["author_id"] = author_id
        values["author_name"] = author_profile.author_name
        if author_profile.author_name:
            resolved_author = (author_id, author_profile.author_name)
    elif author_id is None:
        values["author_id"] = None
        backfill_unknown_author = True

    if isinstance(series_id, int):
        series_profile = await lookup_series_profile(
            series_id,
            uow=uow,
            client=client,
        )
        values["series_id"] = series_id
        values["series_name"] = series_profile.series_name
        if series_profile.series_name:
            resolved_series = (series_id, series_profile.series_name)
    elif series_id is None:
        values["series_id"] = None
        values["series_name"] = None
        values["series_index"] = None

    def _commit(tx: SqlUnitOfWork) -> int:
        write_repo = SQLAlchemyNovelRepository(tx.session)
        author_repo = SQLAlchemyAuthorRepository(tx.session)
        series_repo = SQLAlchemySeriesRepository(tx.session)

        ensure_parent_rows(
            tx.session,
            author_ids={author_id} if isinstance(author_id, int) else (),
            series_ids={series_id} if isinstance(series_id, int) else (),
        )

        affected = write_repo.update_metadata_many(applicable, values)

        if backfill_unknown_author:
            write_repo.update_metadata_many(
                applicable,
                {"author_name": UNKNOWN_AUTHOR},
                only_where_null="author_name",
            )

        if resolved_author is not None:
            author_repo.update_author_name(*resolved_author)
        if resolved_series is not None:
            series_repo.update_series_name(*resolved_series)

        return affected

    counts["affected"] = await run_write_transaction(uow, _commit)
    return counts
