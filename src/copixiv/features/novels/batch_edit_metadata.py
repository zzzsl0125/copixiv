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

**③ 计数契约。** ``matched`` = 调用方给的整个选择集（``all_matched`` 模式下就是
解析出的匹配集），``applicable`` = 其中**本地书**的数量（走
``filter_ids_by_source(..., {SOURCE_LOCAL})``，来源常量取自
``features/library/local_axis.py``），``skipped = matched - applicable``，
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
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.authors.resolve_names import lookup_author_profile
from copixiv.features.library.local_axis import SOURCE_LOCAL
from copixiv.features.novels.repo import (
    SQLAlchemyNovelRepository,
    SQLAlchemySeriesRepository,
)
from copixiv.features.novels.resolve_series import lookup_series_profile
from copixiv.features.novels.update_metadata import (
    UNKNOWN_AUTHOR,
    _author_id,
    _series_id,
)
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.pixiv.client import PixivClient


class _Unset:
    """「这个字段这次没提交」的哨兵类型 —— 与显式 ``None``（解除关联）不同。

    用类型而不是裸 ``object()``：``execute`` 的签名要能写出
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


class BatchEditMetadataUseCase:
    """把"一个作者 id / 一个系列 id"写到一批**本地书**上（预演或落库）。

    与 :class:`~copixiv.features.novels.update_metadata.UpdateNovelMetadataUseCase`
    同构（同样的仓库装配、同样的三态、同样的解析链），差别全在"批量"这两个字上：
    选择集先按来源收窄、pixiv 书跳过而不是 400、解析只做一次、落库是一条 UPDATE。
    """

    def __init__(
        self,
        novel_repo: SQLAlchemyNovelRepository,
        write_repo: SQLAlchemyNovelRepository,
        tag_repo: SQLAlchemyTagRepository,
        author_repo: SQLAlchemyAuthorRepository,
        series_repo: SQLAlchemySeriesRepository,
        client: PixivClient | None = None,
    ):
        # 读 / 写两半显式分开：收窄选择集是 ``filter_ids_by_source``（SELECT），
        # 落库是 ``update_metadata_many``（一条 UPDATE）。装配点通常把同一个
        # facade（``SQLAlchemyNovelRepository`` 同时实现两半）传两次。
        self._repo = novel_repo
        self._write = write_repo
        # 标签仓库：与单本用例**同一套装配形状**（两个用例在 API 层交替出现，
        # 签名一致才好对照）。本功能不写标签——批量标签有专门的
        # ``BatchTagUseCase``（并集 / 差集语义不同，见 ``batch_operations.py``）。
        self._tags = tag_repo
        self._authors = author_repo
        self._series = series_repo
        # 可选：为 None 时名字只能来自本地表（测试与离线场景），与单本一致。
        self._client = client

    async def execute(
        self,
        eligible_ids: list[int],
        *,
        author_id: int | None | _Unset = _UNSET,
        series_id: int | None | _Unset = _UNSET,
        apply: bool = False,
    ) -> BatchMetadataCounts:
        """计数（``apply=False``）或"计数 + 写库"（``apply=True``）。

        Args:
            eligible_ids: 调用方解析出的整个选择集（含 pixiv 书）。
            author_id: ``_UNSET`` = 不动作者；``None`` = 解除关联；正整数 = 挂上去。
            series_id: 同上，收尾差异见模块 docstring。
            apply: ``False`` = 只算数不写库（预演），``True`` = 先算数再写库。

        Raises:
            ValidationError: 两个字段都没提交 / 空选择集 / id 非法
                （``0`` / 负数 / 非整数 / 布尔，文案与单本 PATCH 逐字一致——
                校验规则与文案的唯一所有者在 ``update_metadata.py::_positive_id``，
                这里直接调 ``_author_id`` / ``_series_id``，不抄第二份）。
        """
        if author_id is _UNSET and series_id is _UNSET:
            raise ValidationError("没有要修改的字段")
        if not eligible_ids:
            raise ValidationError("请先勾选要修改的小说")

        # 先校验再收窄：id 非法的 400 与"选择集里有没有本地书"无关。校验过的值
        # 覆盖参数，下面只管三态分支，不必再判类型。
        if author_id is not _UNSET and author_id is not None:
            author_id = _author_id(author_id)
        if series_id is not _UNSET and series_id is not None:
            series_id = _series_id(series_id)

        # 只有本地书接受这批字段（field_policy：两个 id 都是"仅本地书"）。
        # 收窄走索引、不读行——pixiv 书的数量进 ``skipped``，不是错误。
        applicable = await self._repo.filter_ids_by_source(
            eligible_ids, {SOURCE_LOCAL},
        )
        counts = BatchMetadataCounts(
            matched=len(eligible_ids),
            applicable=len(applicable),
            skipped=len(eligible_ids) - len(applicable),
            affected=0,
        )

        if not apply or not applicable:
            # 预演：一个字都不写（不 ensure_exists、不解析、不打网络）。
            # 可写行为 0 时同理——没有行要写，就不该留下占位 author / series 行，
            # 也不该为一次不会落库的编辑去问 Pixiv。
            return counts

        values: dict[str, Any] = {}
        resolved_author: tuple[int, str] | None = None
        resolved_series: tuple[int, str] | None = None
        # 解除作者关联的第二条语句（只补空名），见下方注释。
        backfill_unknown_author = False

        if author_id is not _UNSET:
            if author_id is None:
                # 解除关联只清 id：显示名保留（本地书的名字来自文件名解析，有独立
                # 来源）。缺失的名字由下面那条带条件的 UPDATE 补——单本语义里是
                # ``if novel.author_name is None``，批量里退化成
                # ``WHERE author_name IS NULL``。
                values["author_id"] = None
                backfill_unknown_author = True
            else:
                # 外键安全：``novel.author_id → author.author_id``，先建占位行
                # （``ON CONFLICT DO NOTHING``，已有名字的行不受影响）。
                self._authors.ensure_exists({author_id})
                profile = await lookup_author_profile(
                    author_id, session=self._authors.session,
                    client=self._client,
                )
                values["author_id"] = author_id
                # 解析不出 → ``None``（"待解析"），不是失败：见模块 docstring ⑤。
                values["author_name"] = profile.author_name
                if profile.author_name:
                    resolved_author = (author_id, profile.author_name)

        if series_id is not _UNSET:
            if series_id is None:
                # 解除关联 = 清掉整个"系列三元组"（与单本逐字一致：id 是身份、
                # 名字是它的解析结果、章节号是它在系列里的位置）。
                values["series_id"] = None
                values["series_name"] = None
                values["series_index"] = None
            else:
                self._series.ensure_exists({series_id})
                profile = await lookup_series_profile(
                    series_id, session=self._series.session,
                    client=self._client,
                )
                values["series_id"] = series_id
                values["series_name"] = profile.series_name
                if profile.series_name:
                    resolved_series = (series_id, profile.series_name)

        # 一条 set-based UPDATE 覆盖整个 applicable 列表（两个字段同时提交也是
        # 一条——它们描述的是同一批行的同一件事）。``affected`` 只数这一条：
        # 它是"这次批量编辑改了几本"，名字回写与补空名是收尾，混进来会让
        # ``affected`` 与 ``applicable`` 失去可比性。
        counts["affected"] = await self._write.update_metadata_many(
            applicable, values,
        )

        if backfill_unknown_author:
            # 第二条带条件的 set-based UPDATE：``author_name IS NULL`` 的行补
            # 「未知作者」，有名字的行一个字节不动。常量复用
            # ``update_metadata.UNKNOWN_AUTHOR``（产品承诺的唯一所有者）。
            await self._write.update_metadata_many(
                applicable,
                {"author_name": UNKNOWN_AUTHOR},
                only_where_null="author_name",
            )

        # 作者行的名字与"同作者的全部 novel 行"一起规范（``update_author_name``
        # 两个都做，并用 ``IS DISTINCT FROM`` 跳掉无变化的行）。它与
        # ``resolve_names.writeback_author_names({aid: name}, uow)`` 是同一件事
        # ——后者就是这个调用的一层循环包装，而本用例按单本用例的装配形状只拿
        # 仓库、不拿 uow（这里也只有一个 id，没有可循环的东西）。
        if resolved_author is not None:
            await self._authors.update_author_name(*resolved_author)

        # 系列只命名"这一个系列行"：系列名的规范方向是 novel → series
        # （``update_summary`` 取 ``max(novel.series_name)``），改写同系列其它
        # 小说行既没必要也会覆盖来源值。见 ``repo.update_series_name`` 的注释。
        if resolved_series is not None:
            await self._series.update_series_name(*resolved_series)

        return counts
