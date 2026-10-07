"""用例：单本同步 —— 按 id 从 pixiv 重抓元数据并落库。

契约：``docs/NOVEL_EDIT_DESIGN.md`` §3（字段清单）/ §5.7（同步：pixiv 书那三列的
修正入口）+ ``field_policy.py``（pixiv 书的 ``title`` / ``author_id`` /
``series_id`` 已收成只读）。它是原 P1「重新抓取，恢复来源值」的**单本版**，
2026-09-22 落地：那三列错配之后的修正入口——不再靠手改，而是把这一行按 id 从
来源重抓一遍。

三件事写清楚（前两件是同一类事故的防线）：

1. **绝不动 ``has_epub`` 与 ``path``**。``build_from_webview`` 会从正文重新判定
   ``has_epub``（``PENDING`` / ``NO_IMAGES``），但那一列是**本地产物的状态机**，
   归 EPUB 产线与 ``check_epub`` 巡检所有——把 draft 的判定值写进去就会把
   ``DONE(2)`` 打回 ``NO_IMAGES(3)``。2026-08「22.6 万行 has_epub 失明」就是这类
   事故：一个"看起来更真实"的值覆盖了产线的真相。所以这里把 ``draft.has_epub``
   置为 ``None``——``_upsert_rows`` 遇 None 弹出该列，一个字节都不写。
   ``path`` 是文件锚，同样不进 ``force_update``：只在库里为 NULL 时才被填上
   （"非空者胜"），已有文件的行不会被改道。
2. **网络调用在写事务之外**（仓库纪律：抓取 / 收集不持有写事务）。
   ``webview_novel`` 抓取与作者名收集都在 ``run_write_transaction`` **之前**
   完成；写事务只做落库这一件事，短、无网络。
3. **``series_index`` 不进 ``force_update``**：webview 的序号来自
   ``guess_series_order(series_navigation)`` 的**猜测**，猜不出来就是 ``None``；
   放进覆盖白名单等于用 None 抹掉库里已有的值。

其余按"来源权威"的既有语义走，三条要说给用户听：

* ``title`` / ``caption`` / ``series_name`` / ``like`` / ``view`` / ``text``
  会被来源值覆盖——``text`` 按来源正文字数更新（webview 里没有"我改过的字数"
  这回事）；
* ``create_time`` 与 ``author_id`` / ``series_id`` / ``author_name`` 本次纳入
  覆盖（``force_update``）：它们正是这个功能要"恢复"的来源值；
* ``tags`` 是**只增不减**的并集（``_upsert_rows`` 第 4 步）：**同步不会删掉
  用户自己加的标签**。反过来，用户在编辑器里删掉的"来源也有"的标签会被加回来
  ——那是并集语义的既有边界，要彻底去掉得用 ``tag_alias``。

``author_name`` 需要一点额外动作：webview 形状**没有**作者名
（``build_from_webview`` 恒给 ``None``），而它在 ``force_update`` 里——直接落库
会把库里已有的显示名抹成 NULL。所以落库前先解析一次（见
:meth:`SyncNovelUseCase._with_author_name`）。
"""

from __future__ import annotations

from dataclasses import replace

from copixiv.core.draft import NovelDraft, build_from_webview
from copixiv.core.exceptions import (
    NotFoundError,
    NovelNotFoundError,
    ValidationError,
)
from copixiv.core.models import Novel
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.authors.resolve_names import (
    collect_author_names,
    writeback_author_names,
)
from copixiv.features.library.local_axis import SOURCE_PIXIV
from copixiv.features.novels.ingest import batch_upsert
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.pixiv.client import PixivClient
from copixiv.log import logger

# 本次覆盖的来源权威字段。刻意**不含** ``series_index``（猜测值，None 会抹掉
# 库里已有的）与 ``path``（文件锚，只在库中为 NULL 时才允许被填）——原因见模块
# docstring 第 1、3 条。``author_name`` 在列，但落库前一定先解析出真名
# （``_with_author_name``），否则等于用它把名字清空。
SYNC_FORCE_UPDATE: list[str] = [
    "author_id", "author_name", "series_id", "create_time",
]

# 本地书没有来源可恢复 —— 同步只服务 pixiv 行。
LOCAL_BOOK_HAS_NO_SOURCE = "本地书没有来源可同步（同步用于恢复 pixiv 来源值）"


class SyncNovelUseCase:
    """把一行 pixiv 小说按 id 重抓一遍，恢复来源值。

    调用方（``api.py``）传请求级的 ``Depends(get_uow)`` 单元 —— **不是**
    ``get_write_uow``：这个用例的写事务由 :func:`run_write_transaction` 自己
    开（与 ``tasks/maintenance.py`` 同一种用法），因为抓取必须在写事务之外，
    而依赖注入阶段开不出"稍后再写"的事务边界。
    """

    def __init__(
        self,
        uow: SqlUnitOfWork,
        client: PixivClient,
        download_dir: str,
    ):
        self._uow = uow
        self._client = client
        # 只为算 draft.path（``build_path``）——落库时它只在库中为 NULL 时才生效。
        self._download_dir = download_dir

    async def execute(self, novel_id: int) -> Novel:
        """重抓并落库，返回更新后的域模型（HTTP 层直接做 response_model）。

        Raises:
            NotFoundError: 库里没有这一行 → 404；或 pixiv 上已不存在 / 受限
                （webview 返回空）→ 404。
            ValidationError: 本地书 → 400（没有来源可同步）。
        """
        novel = await self._read(novel_id)
        if novel is None:
            raise NotFoundError(f"Novel {novel_id} not found")
        if novel.source != SOURCE_PIXIV:
            raise ValidationError(LOCAL_BOOK_HAS_NO_SOURCE)

        # ---- 以下两步都是网络 I/O，必须在写事务之外 ----
        draft = await self._fetch(novel_id)
        if draft is None:
            # 与 ``ingest.py::_download_novels`` 的记账口径一致：webview 返回空
            # = 已删除 / 受限（那两条路都把它当"取不到"，只是这里回 404 而不是
            # 写 failed_novel 台账——用户点一下按钮不该往失败账本里写东西）。
            raise NotFoundError(f"小说 #{novel_id} 在 pixiv 上不存在或不可访问")
        draft = await self._with_author_name(draft)

        # ---- 落库：短写事务，重试撞锁（run_write_transaction 的既有语义）----
        await run_write_transaction(
            self._uow, lambda uow: self._persist(uow, draft),
        )

        # 读回与 PATCH 同一条路（``get_by_id`` 的域模型已处理 is_special_follow
        # 的 NULL 归一），前端拿到的形状与 PATCH 响应一致。
        updated = await self._read(novel_id)
        if updated is None:  # 并发删除的兜底——上面刚写过，实际不会发生
            raise NotFoundError(f"Novel {novel_id} not found")
        return updated

    # ------------------------------------------------------------------
    # 三个阶段：读（短事务）→ 抓（网络）→ 写（短事务）
    # ------------------------------------------------------------------

    async def _read(self, novel_id: int) -> Novel | None:
        """短读事务读一行；读不到就是不存在。

        显式开一个 ``begin()``（而不是裸用 ``uow.session``）：读完即提交/释放，
        抓取阶段不会挂着一个打开的读事务。
        """
        async with self._uow.begin():
            return await SQLAlchemyNovelRepository(
                self._uow.session
            ).get_by_id(novel_id)

    async def _fetch(self, novel_id: int) -> NovelDraft | None:
        """``webview_novel`` → ``build_from_webview``；取不到返回 ``None``。

        取数方式与 ``ingest.py::_download_novels`` 的同一条路
        （``fetch_novel_and_assets`` 内部就是这两步），但这里刻意**不**做它另外
        两件事：不写正文文件、不触发资产 / EPUB 流水线——同步只动元数据，
        点一下"同步"不该顺带重建 EPUB。
        """
        try:
            response = await self._client.webview_novel(novel_id)
        except NovelNotFoundError:
            # 确定性失败（404 / 内容被删）——与下载路径同一种处理，交给调用方 404；
            # 网络错误 / 限流**不**吞，让它们照旧冒泡（那才是可重试的失败）。
            logger.warning(f"同步: #{novel_id} 在 pixiv 上不存在或不可访问")
            return None
        if not response:
            return None

        draft = build_from_webview(response, self._download_dir)
        # has_epub 归产线所有：None = "别覆盖库里的值"（见模块 docstring 第 1 条）。
        return replace(draft, has_epub=None)

    async def _with_author_name(self, draft: NovelDraft) -> NovelDraft:
        """给 draft 补上作者显示名（``force_update`` 含 ``author_name``）。

        名字的来源链与本库其它地方一致：``author`` 表优先、pixiv
        ``user_detail`` 兜底（``collect_author_names`` 的两阶段，全部在写事务
        之外；本地命中时零网络）。解析不出来就留 NULL——名字待解析是**仓库认可
        的中间态**（id 是身份、名字是缓存），**不是**"有兜底任务在等着"：
        ``sync_empty_name`` 默认禁用（``scheduled_task.is_enabled`` 列默认
        false，只有手动启用才跑）；真正会把名字补回来的是**再抓一次这个作品 /
        作者**，或者用户再点一次「同步」。
        """
        if not draft.author_id:
            return draft
        names = await collect_author_names(
            {draft.author_id}, uow=self._uow, client=self._client,
        )
        return replace(draft, author_name=names.get(draft.author_id))

    async def _persist(self, uow: SqlUnitOfWork, draft: NovelDraft) -> None:
        """落库（调用方已开写事务）：upsert 一行 + 把作者名写回 ``author`` 行。

        ``writeback_author_names`` 与 ``ingest`` 的持久化阶段同一个收尾：名字是
        作者的**属性**，只写 novel 行会让作者页与卡片显示两个名字。
        """
        await batch_upsert([draft], uow, force_update=SYNC_FORCE_UPDATE)
        if draft.author_name:
            await writeback_author_names(
                {draft.author_id: draft.author_name}, uow,
            )
