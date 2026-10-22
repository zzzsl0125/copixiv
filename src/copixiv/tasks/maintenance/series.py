"""``fix_series_index`` — chapter-number / series-id data repair.

Split verbatim out of ``copixiv.tasks.maintenance`` (see that package's
docstring).  The three branch cursors and the page/draft helpers below are
series-only; the cursor *helpers* come from :mod:`._cursor`.
"""

from pydantic import BaseModel, field_validator

from copixiv.core.models import TaskResult
from copixiv.db.write_lock import run_write_transaction
from copixiv.features.novels.repo import SQLAlchemySeriesRepository
from copixiv.log import logger

from ._cursor import _read_cursor, _write_cursor
from ..kernel import TaskContext
from ..kernel import register


# ---------------------------------------------------------------------------
# fix_series_index —— 章节号 / 系列 id 修复（见任务 docstring）
# ---------------------------------------------------------------------------

# 三个分支各推各的游标。check_epub 的 _CURSOR_KEY 是模块级共享常量，这里绝不复用：
# 一个任务的断点被另一个任务覆盖，等于两边都在瞎跑。
_SERIES_CURSOR_KEY = "fix_series_index.cursor"          # 缺号系列
_ORPHAN_CURSOR_KEY = "fix_series_index.orphan_cursor"   # 孤儿号行（按 novel.id）
_SWEEP_CURSOR_KEY = "fix_series_index.sweep_cursor"     # 全量复查（sweep_all）


class FixSeriesIndexArgs(BaseModel):
    """``fix_series_index`` 的 JSON 参数。

    三个"每轮上限"都可缺省（``None`` = 不限），缺省时行为与旧版一致——一轮把
    候选集走完。任务每轮都要打 pixiv（每个待修 / 待查系列一次名册、每本孤儿一次
    webview），限量只是把一轮切成几轮跑：游标保证不漏行、下轮从断点续。

    **生产必须给上限**（2026-10-07 实测）：不限量 = 一轮 1,243 个缺号系列 + 1,580
    条孤儿，30 分钟的超时跑不完；而超时被杀时整页从未扫完，游标也就一直停在 0，
    下一轮又从同一个系列从头重扫，永远推不完一轮。给了上限，游标每查完一个系列就
    落一次，超时 / 中断都能从断点续。

    ``sweep_all`` 打开"全量复查"：把库里号齐全的系列也拉名册核对一遍
    （生产口径 25,069 个系列 ≈ 2.5 万次请求，一次跑不完就靠它自己的游标续）。
    默认关闭——那时只扫"有缺号"的系列（1,179 个）与孤儿行（1,594 行）。
    """

    max_series: int | None = None     # 本轮最多处理的"缺号系列"数
    max_orphans: int | None = None    # 本轮最多处理的"孤儿号"行数
    max_sweep: int | None = None      # 本轮最多复查的"号齐全系列"数
    sweep_all: bool = False           # 是否开启"号齐全系列"的全量复查

    @field_validator("max_series", "max_orphans", "max_sweep", mode="before")
    @classmethod
    def _blank_means_unlimited(cls, value):
        """空串按"不限"处理。

        任务编辑器对"默认 null"的参数会填空串
        （``TaskEditModal.vue``：``arg.default !== null ? arg.default : ''``），
        不转换的话，用户没碰过的输入框会让整个任务参数校验失败。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sweep_all", mode="before")
    @classmethod
    def _blank_means_off(cls, value):
        """同理：空串按"关闭"处理（手改 params JSON 时会遇到）。"""
        if isinstance(value, str) and not value.strip():
            return False
        return value


def _page(ids: list[int], cap: int | None) -> tuple[list[int], bool]:
    """游标之后的候选 *ids* 与本轮分页：返回 ``(本轮要跑的页, 是否被上限截断)``。"""
    if cap is None:
        return ids, False
    return ids[:cap], len(ids) > cap


async def _stored_columns(uow, novel_id: int) -> dict | None:
    """一行的库内现值（只读），没有这一行则 None。

    只改一两列的 draft 必须带上这些列的现值：``repo._upsert_rows`` 的覆盖白名单
    （like / view / title / text / caption / series_name）是**无条件写入**的，
    draft 里少了哪一列就等于把它清成 NULL / 0。
    """
    async with uow.begin():
        return await SQLAlchemySeriesRepository(uow.session).novel_columns(novel_id)


def _draft_from_row(columns: dict, **overrides):
    """以库内现值为底造 draft（``has_epub=None`` = 不碰 EPUB 状态列）。

    这是本任务**唯一**的 draft 来源。刻意不从 ``novel_series`` 的名册条目造：
    那个载荷顶层是 ``pixivpy3.utils.ParsedJson``、条目是 ``JsonDict``（dict 子类，
    ``__getattr__`` 回落到键访问），``build_from_novel_info`` 的属性访问在生产能
    跑通纯属上游实现细节——换成普通 dict 就 ``AttributeError``；而且以条目为底
    造 draft 时，条目缺哪一列就会把库里那一列洗掉。名册条目只用来取号。
    """
    from copixiv.core.draft import NovelDraft

    values = {**columns, **overrides}
    values.pop("has_epub", None)
    return NovelDraft(has_epub=None, **values)


@register(
    "fix_series_index",
    description="修复小说章节号与系列归属的数据问题",
    args=FixSeriesIndexArgs,
)
async def fix_series_index(args: FixSeriesIndexArgs, ctx: TaskContext) -> TaskResult:
    """修复「小说缺章节号」与「丢了系列 id」两类数据问题。

    ``novel.series_index`` 只在**下载那一刻**从 webview 的
    ``seriesNavigation.prevNovel/nextNovel.contentOrder`` 推算
    （``core/services.py::guess_series_order``）；元数据刷新路径永远拿不到号——
    pixiv 的 ``novelInfo`` / ``novel_series`` / novel detail 三个端点都不返回
    index 字段，``core/draft.py::build_from_novel_info`` 读的 ``series.index``
    因此恒为空。漏掉的号只能靠后补，本任务就是那个"后补"。

    ① **编号公式（2026-09-24 用系列 16242552 全 9 本实测钉死）**：``novel_series``
       的返回顺序就是作者的章节顺序（**从旧到新**），**章节号 = 名册 1 基位置**
       （第 1 本 → 1，第 N 本 → N）。实测对照：名册
       ``[28670265, 28701995, 28943643, 28761898, 28820329, 28886104, 29049733,
       29092404, 29173929]`` ↔ 导航权威号 1..9 ↔ 标题 第一幕 / 第二幕 / 第2.5幕 /
       第三幕 / 第四幕 / 第五幕 / 第5.5幕 / 第六幕 / 第七幕。注意这条顺序**既不是**
       id 序**也不是** create_time 序（第 3 位"第2.5幕"插在第二幕之后，而它的 id
       和发布时间都比第 4 位晚）——旧版 ``novels.sort(key=id)`` 正是把这条作者顺序
       覆盖掉，才写出 6.8% 的错值。

    ② **缺号**（有系列、号为空）：拉名册，按上面的公式补号；同一批次里号已经是
       错值的行也一并纠正——纠正必须 ``force_update=["series_index"]``
       （``repo._upsert_rows`` 只在"列是 NULL"或"列进了 ``update_fields_set``"时
       才写，``series_index`` 不在后者的默认集合里）。draft **以库内现值为底**
       （``_stored_columns`` + ``_draft_from_row``），名册条目**只用来取号**：
       条目在生产是 ``JsonDict``、测试里可能是普通 dict 或 pydantic 对象，
       ``safe_get`` 是三种形状都成立的唯一取法；从条目造 draft 则条目缺哪列就洗掉
       库里哪列。只有"号为空或与权威值不一致"的行才下写，不整系列无脑 UPDATE。

    ③ **孤儿号**（``series_id`` 为空、``series_index`` 还留着）：2026-07-13 之前
       那版元数据刷新把 ``series_id`` 抹成 NULL 的化石行（生产实测 1,594 行，全部
       ``create_time`` 更早），``series_with_empty_index()`` 永远扫不到它们。逐本
       ``webview_novel`` 直查，按 id 走独立游标：

       * ``series_id`` 有值 → 并进名册批次挂回：身份列（``series_id`` /
         ``series_name`` / 作者）取自 **webview 响应**（``safe_get``，允许覆盖
         NULL），号由名册算出的权威值决定；库里没有别的行挂在这个系列下、且导航
         能算出号时退化为单本推算，省一次名册请求。
       * ``series_id`` 为 NULL → 作品确实已不在任何系列：清号（写 None，同样要
         ``force_update``，否则"非空者胜"的填充规则写不动它），``series_name``
         **保留不动**——编辑器那条"解除关联"是用户主动清（连名字一起清，因为清的是
         本地书），这里清的是"来源已经没有系列"的化石行，名字留着仍是这本书的信息
         （``docs/NOVEL_EDIT_DESIGN.md`` §5.6-3：「有名无 id 的行不归编辑器管」）。
       * 请求失败 / 404 / 响应解析不出来 → **什么都不改**：行留在候选集里、游标
         不越过它，下一轮先重试（不吞异常，也不假装已处理）。

    ④ **错值全量复查**（``sweep_all=true``，默认关闭）：旧版只扫"有缺号"的系列，
       "号齐全但值是错的"系列（生产实测 1,538 个系列、约 9,780 行）永远没人管。
       开启后按 ``repo.series_with_stored_index``（至少一本带号）逐系列拉名册核对，
       复用同一条纠正路径与 ``force_update=["series_index"]``。它与缺号系列候选
       **互补**（并集 = 库里全部 26,248 个系列、互不重叠），所以一轮全量复查约
       2.5 万次名册请求——30 分钟的超时跑不完，靠 ``_SWEEP_CURSOR_KEY`` 续跑。

    三个分支各有独立游标（``setting`` 表，:data:`_SERIES_CURSOR_KEY` /
    :data:`_ORPHAN_CURSOR_KEY` / :data:`_SWEEP_CURSOR_KEY`）：**先落库、再推进
    游标**，而且**每查完一个系列（成功或失败）都推**——失败不是"没查"，是"查过了、
    这轮拿不到"；不推的话，配上"每轮上限"，一页开头一个永久失败行（已删作品 /
    受限系列）就会把游标钉住，后面的行永远轮不到。一轮走完候选集就归零，下一轮从
    头复查，失败的行仍在候选集里，所以会被再查一遍——"跳过"只持续到下一次整轮
    复查。每个系列一次提交，超时 / 中断不丢进度。默认禁用
    （``scheduled_task.is_enabled`` 列默认 false，显示名「修复章节」）。

    开销账（2026-09 生产口径）：默认一轮 = 1,179 个缺号系列 + 1,594 条孤儿 ≈
    2.8k 次请求；``sweep_all`` 再叠加 25,069 次名册请求。名册一轮内按系列缓存
    （三个分支的候选常常是同一个系列），空跑一轮（候选集为空）一次请求都不发。
    """
    from copixiv.core.services import guess_series_order, safe_get
    from copixiv.features.novels.ingest import batch_upsert

    uow = ctx.uow

    series_cursor = await _read_cursor(uow, key=_SERIES_CURSOR_KEY)
    orphan_cursor = await _read_cursor(uow, key=_ORPHAN_CURSOR_KEY)
    sweep_cursor = await _read_cursor(uow, key=_SWEEP_CURSOR_KEY)

    # ---- 候选集：缺号系列（②）+ 孤儿号行（③）+ 号齐全系列（④，sweep_all）----
    async with uow.begin():
        repo = SQLAlchemySeriesRepository(uow.session)
        candidates = sorted(await repo.series_with_empty_index())
        orphan_candidates = await repo.novels_with_orphan_index(
            limit=(
                args.max_orphans + 1 if args.max_orphans is not None else None
            ),
            after_id=orphan_cursor,
        )
        sweep_candidates = (
            await repo.series_with_stored_index(
                after_id=sweep_cursor,
                limit=(
                    args.max_sweep + 1 if args.max_sweep is not None else None
                ),
            )
            if args.sweep_all
            else []
        )

    series_page, series_truncated = _page(
        [sid for sid in candidates if sid > series_cursor], args.max_series,
    )
    orphan_truncated = (
        args.max_orphans is not None and len(orphan_candidates) > args.max_orphans
    )
    orphan_page = (
        orphan_candidates[:args.max_orphans]
        if args.max_orphans is not None
        else orphan_candidates
    )
    sweep_truncated = (
        args.max_sweep is not None and len(sweep_candidates) > args.max_sweep
    )
    sweep_page = (
        sweep_candidates[:args.max_sweep]
        if args.max_sweep is not None
        else sweep_candidates
    )

    if not series_page and not orphan_page and not sweep_page:
        # 候选集为空：一次网络请求都不发。游标停在半路（上轮已走完 / 行已被修好）
        # 就归零，让下一轮从头复查。
        if series_cursor:
            await _write_cursor(uow, 0, key=_SERIES_CURSOR_KEY)
        if orphan_cursor:
            await _write_cursor(uow, 0, key=_ORPHAN_CURSOR_KEY)
        if sweep_cursor:
            await _write_cursor(uow, 0, key=_SWEEP_CURSOR_KEY)
        return TaskResult(summary="系列章节号检查: 无需修复")

    stats = {
        "filled": 0, "fixed": 0, "glued": 0, "cleared": 0,
        "series_done": 0, "orphan_done": 0, "sweep_done": 0, "failed": 0,
    }

    # 一轮内的名册缓存：同一个系列一轮只拉一次名册（② / ③ / ④ 的候选常常是同一个
    # 系列，去重后一次补一批）。
    rosters: dict[int, list | None] = {}

    async def _roster(series_id: int) -> list | None:
        """该系列的名册（作者章节顺序：从旧到新）；None = 本轮拿不到。"""
        if series_id not in rosters:
            try:
                resp = await ctx.client.novel_series(series_id, fetch_all=True)
            except Exception as exc:                     # 网络 / 限流 / 404
                logger.warning(
                    f"fix_series_index: 系列 {series_id} 名册请求失败: {exc}"
                )
                rosters[series_id] = None
            else:
                novels = safe_get(resp, "novels", None)
                rosters[series_id] = list(novels) if novels else None
        return rosters[series_id]

    async def _apply_roster(
        series_id: int,
        roster: list,
        forced: dict[int, tuple[dict, str | None, int | None]],
    ) -> tuple[int, int, set[int]]:
        """把名册的权威号落库（一个系列一次提交）。

        权威号 = **名册位置（1 基）**：``novel_series`` 的返回顺序就是作者的章节
        顺序（从旧到新，2026-09-24 用系列 16242552 全 9 本实测钉死）。

        返回 ``(补空数, 纠正数, 实际挂回的 id 集合)``。*forced* 是"孤儿挂回"的
        ``{id: (库内现值列, webview 给的系列名, webview 给的作者 id)}``：这些行的
        ``series_id`` 还是 NULL，不在"库内挂在该系列下"的名单里，要单独走一批带
        身份列的 ``force_update``；身份来自 webview 响应，不从名册条目取。

        draft 一律以**库内现值**为底（``_draft_from_row``），名册条目只用来取号
        ——条目缺列不会洗掉 title / text / tags，也不依赖上游对象类型。
        """
        authority: dict[int, int] = {}
        for pos0, item in enumerate(roster):
            item_id = safe_get(item, "id")
            if item_id is None:
                continue
            authority[int(item_id)] = pos0 + 1          # 名册位置（1 基）= 章节号

        async with uow.begin():
            stored = await SQLAlchemySeriesRepository(uow.session).novels_of_series(
                series_id
            )

        fills: list = []      # 号为空 → 直接补（不需要 force_update）
        fixes: list = []      # 号是错值 → 必须 force_update 才写得进去
        for columns in stored:
            novel_id = int(columns["id"])
            want = authority.get(novel_id)
            if want is None:
                continue            # 名册里没有这一本（受限 / 未收录）→ 不动它
            draft = _draft_from_row(columns, series_index=want)
            if not draft.author_id:
                continue            # 连作者都没有：写不进去（见 _write_glue_single）
            if columns["series_index"] is None:
                fills.append(draft)
            elif int(columns["series_index"]) != want:
                fixes.append(draft)

        glue: list = []
        glued: set[int] = set()
        for novel_id, (columns, series_name, author_id) in forced.items():
            want = authority.get(novel_id)
            if want is None:
                continue            # 名册里没有它 → 交给调用方退化为单本推算
            draft = _draft_from_row(
                columns,
                author_id=columns.get("author_id") or author_id,
                series_id=series_id,
                series_name=series_name or columns.get("series_name"),
                series_index=want,
            )
            if not draft.author_id:
                continue
            glue.append(draft)
            glued.add(novel_id)

        if fills or fixes or glue:
            async def _write(uw) -> None:
                if fills:
                    await batch_upsert(fills, uw)
                if fixes:
                    await batch_upsert(fixes, uw, force_update=["series_index"])
                if glue:
                    await batch_upsert(
                        glue, uw,
                        force_update=["series_id", "series_name", "series_index"],
                    )

            await run_write_transaction(uow, _write)

        return len(fills), len(fixes), glued

    async def _write_glue_single(
        columns: dict, series_id: int, series_name: str | None,
        index: int | None, author_id: int | None,
    ) -> bool:
        """单本挂回（系列里只此一本 / 名册里没有它），不拉名册。

        号用 webview 导航推算（``guess_series_order``，与下载那一刻同一个算法）；
        导航给不出号时**不动**这一列。draft 以库内现值为底，只改系列三列。
        *author_id* 是 webview 给的作者兜底：库里这一列是 NULL 时按"只补空"规则
        填上（生产实测 1,594 本孤儿里有 13 本没有 author_id，否则根本写不进去——
        ``persist_novels`` 会把 None 拿去建作者占位行，撞 NOT NULL）。
        """
        author_id = columns.get("author_id") or author_id
        if not author_id:
            return False                  # 连作者都缺 → 什么都不改（下轮重试）
        force = ["series_id", "series_name"]
        if index:
            force.append("series_index")
        draft = _draft_from_row(
            columns,
            author_id=author_id,
            series_id=int(series_id),
            series_name=series_name or columns.get("series_name"),
            series_index=index if index else columns.get("series_index"),
        )
        await run_write_transaction(
            uow, lambda uw: batch_upsert([draft], uw, force_update=force),
        )
        return True

    async def _glue_orphan(
        novel_id: int, series_id: int, columns: dict, data,
        series_name: str | None, author_id: int | None,
    ) -> tuple[bool, int, int]:
        """把一本孤儿挂回系列：优先并进名册批次，必要时退化为单本推算。

        身份三列（series_id / series_name / 作者）一律取自 **webview 响应**
        （``safe_get``：dict / JsonDict / pydantic 三种形状都成立），不从名册条目取。
        """
        async with uow.begin():
            siblings = await SQLAlchemySeriesRepository(
                uow.session
            ).count_novels_by_series(series_id)
        nav_index = guess_series_order(safe_get(data, "series_navigation"))
        title = safe_get(data, "series_title")
        if not siblings and nav_index:
            # 库里这个系列只有这一本、导航又能算出号 → 省一次名册请求
            ok = await _write_glue_single(
                columns, series_id, title, nav_index, author_id,
            )
            return ok, 0, 0

        roster = await _roster(series_id)
        if roster is None:
            return False, 0, 0
        if not any(int(safe_get(item, "id", 0) or 0) == novel_id for item in roster):
            # 名册里没有它（受限 / 未收录）→ 别猜位置，退化为单本推算
            ok = await _write_glue_single(
                columns, series_id, title, nav_index, author_id,
            )
            return ok, 0, 0

        filled, fixed, glued = await _apply_roster(
            series_id, roster, {novel_id: (columns, title, author_id)},
        )
        return novel_id in glued, filled, fixed

    async def _clear_orphan_index(
        columns: dict, author_id: int | None = None,
    ) -> bool:
        """作品已不在任何系列 → 清章节号（``series_name`` 保留不动）。"""
        author_id = columns.get("author_id") or author_id
        if not author_id:
            return False                  # 连作者都缺 → 什么都不改（下轮重试）
        await run_write_transaction(
            uow,
            lambda uw: batch_upsert(
                [_draft_from_row(columns, author_id=author_id, series_index=None)],
                uw,
                force_update=["series_index"],
            ),
        )
        return True

    async def _run_roster_branch(
        page: list[int], cursor: int, key: str, counter: str, truncated: bool,
    ) -> None:
        """逐系列拉名册 → 落库 → 推游标（缺号系列与全量复查共用的骨架）。

        游标语义：**先落库、再推进**，而且**每查完一个系列（成功或失败）都推**。

        为什么失败也推：失败不是"没查"，是"查过了、这轮拿不到"。失败不推时，配上
        "每轮上限"，一页开头一个永久失败行（已删作品 / 受限系列）就会把游标钉在
        原地，后面的行永远轮不到（2026-10-07 生产实测，task_history id=1489：任务
        从系列 1244949 扫到 15764828 后 30 分钟超时被杀，游标因整页从未扫完而一直
        是 0，每天从头重来）。失败的行仍在候选集里，跑完一轮归零后会再复查，所以
        "跳过"只持续到下一次整轮复查。
        """
        next_cursor = cursor
        for series_id in page:
            next_cursor = series_id
            roster = await _roster(series_id)
            if roster is None:
                stats["failed"] += 1
            else:
                filled, fixed, _ = await _apply_roster(series_id, roster, {})
                stats["filled"] += filled
                stats["fixed"] += fixed
                stats[counter] += 1
            # 每查完一个系列就落一次游标：超时 / 中断也保住这一页的进度，下一轮从
            # 断点续，而不是重扫整页。
            await _write_cursor(uow, next_cursor, key=key)

        if not truncated and (cursor or page):
            # 候选集走完了 → 归零，下一轮从头复查（失败的行还在候选集里）。
            await _write_cursor(uow, 0, key=key)

    # ---- ② 缺号系列：名册权威序补号 + 顺带纠正错值 ----
    await _run_roster_branch(
        series_page, series_cursor, _SERIES_CURSOR_KEY,
        "series_done", series_truncated,
    )

    # ---- ③ 孤儿号：series_id 被抹成 NULL，号还留着 ----
    orphan_next = orphan_cursor
    for novel_id, series_name, _index in orphan_page:
        orphan_next = novel_id
        columns = await _stored_columns(uow, novel_id)
        if columns is None:
            # 行没了 = 没处理（也不值得为它打一次 pixiv）。照推游标：它是永久失败，
            # 留着只会饿死后面的孤儿行。
            stats["failed"] += 1
            await _write_cursor(uow, orphan_next, key=_ORPHAN_CURSOR_KEY)
            continue
        try:
            data = await ctx.client.webview_novel(novel_id)
        except Exception as exc:                         # 网络 / 限流 / 404
            logger.warning(
                f"fix_series_index: #{novel_id} webview 请求失败: {exc}"
            )
            data = None
        if not data:
            # 空响应 / 解析不出来：什么都不改，但**游标照样推**——否则配上"每轮
            # 上限"，开头一条永久失败行会把后面的孤儿永远饿死。失败行仍在候选集
            # 里，跑完一轮归零后再复查。
            stats["failed"] += 1
            await _write_cursor(uow, orphan_next, key=_ORPHAN_CURSOR_KEY)
            continue

        series_id = safe_get(data, "series_id")
        author_id = safe_get(data, "user_id")     # 库里 author_id 为 NULL 时的兜底
        if series_id:
            ok, filled, fixed = await _glue_orphan(
                novel_id, int(series_id), columns, data, series_name, author_id,
            )
            stats["filled"] += filled
            stats["fixed"] += fixed
            stats["glued"] += 1 if ok else 0
        else:
            ok = await _clear_orphan_index(columns, author_id)
            stats["cleared"] += 1 if ok else 0
        if not ok:
            stats["failed"] += 1
        else:
            stats["orphan_done"] += 1
        await _write_cursor(uow, orphan_next, key=_ORPHAN_CURSOR_KEY)

    if not orphan_truncated and (orphan_cursor or orphan_page):
        await _write_cursor(uow, 0, key=_ORPHAN_CURSOR_KEY)

    # ---- ④ 错值全量复查（sweep_all）：号齐全的系列也拉名册核对一遍 ----
    if args.sweep_all:
        await _run_roster_branch(
            sweep_page, sweep_cursor, _SWEEP_CURSOR_KEY,
            "sweep_done", sweep_truncated,
        )

    if (
        stats["series_done"] == 0
        and stats["orphan_done"] == 0
        and stats["sweep_done"] == 0
    ):
        return TaskResult(summary="系列章节号检查: API 请求全部失败")

    summary = (
        f"系列章节号修复: 补号 {stats['filled']} 本, 纠正 {stats['fixed']} 本, "
        f"孤儿挂回 {stats['glued']} 本, 孤儿清号 {stats['cleared']} 本 "
        f"(系列 {stats['series_done']}, 孤儿 {stats['orphan_done']}, "
        f"复查 {stats['sweep_done']}, 失败 {stats['failed']})"
    )
    if series_truncated or orphan_truncated or sweep_truncated:
        summary += "；本轮达上限，下轮续跑"
    return TaskResult(summary=summary)
