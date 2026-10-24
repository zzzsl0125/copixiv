"""Unified ingestion pipeline — plan → download → gate → persist → writeback.

This module is the single ingestion pipeline for every novel-fetching task.
Registered tasks (``tasks/novels.py``) are now thin adapters over
:func:`ingest`: they only do the discovery/enumeration work (ranking,
search, follow feeds, author catalogues) and then hand the collected novel
list (or an explicit id list) to :func:`ingest`, which owns the rest:

1. plan (read-only, no write transaction) — decide what still needs downloading;
2. download (concurrent, no DB access);
3. asset gate (``image_downloader.await_all()``);
4. author-name collect (lock-free) before the write transaction;
5. persist + author-name writeback in a single short write transaction
   (``run_write_transaction`` — no global lock, retries brief lock conflicts).

Nothing here depends on :class:`~copixiv.tasks.kernel.TaskContext` — task
adapters pass the fields they need (``session_factory``, ``client``,
``file_storage``, ``image_downloader``) explicitly.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import run_write_transaction

from copixiv.core.draft import NovelDraft, NovelInfoLike, build_from_novel_info
from copixiv.core.models import EpubStatus
from copixiv.features.novels.download_novel import fetch_novel_and_assets
from copixiv.features.novels.persist import persist_novels
from copixiv.features.novels.series_index import resolve_series_indices
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.authors.resolve_names import collect_author_names, writeback_author_names

from copixiv.log import logger


# ---------------------------------------------------------------------------
# Batch upsert
# ---------------------------------------------------------------------------


async def batch_upsert(
    novels: list[NovelDraft], uow, force_update: list[str] | None = None,
) -> int:
    """Upsert novels and update author/series summaries.

    Thin wrapper over :func:`copixiv.features.novels.persist.persist_novels`
    that adds batch-level logging.

    Pure write helper: the caller is responsible for wrapping it in a short
    write transaction (``run_write_transaction``) so the whole batch
    (including the commit) is atomic; there is no global write lock.
    """
    count = await persist_novels(uow, novels, force_update)
    novels = [n for n in novels if n]
    if novels:
        logger.info(
            f"batch_upsert: {count} newly inserted out of "
            f"{len(novels)} input novels (sample id: {novels[0].id!r})"
        )
    return count


# ---------------------------------------------------------------------------
# Concurrent download
# ---------------------------------------------------------------------------


async def _download_novels(
    novel_ids: list[int],
    client,
    file_storage,
    image_downloader,
    redownload: bool = False,
    stored_paths: dict[int, str] | None = None,
) -> tuple[list[NovelDraft], list[tuple[int, str]]]:
    """Download novels via webview concurrently.

    Fires all webview_novel calls in parallel — the client semaphore + LRU
    account selection distribute the load across accounts automatically.
    Each novel's text, images, and EPUB are processed as it completes.

    *stored_paths* maps already-known ids to their ``novel.path`` so the body
    is written to the file the read path actually resolves (see
    ``_plan_batch``); ids not in it are new and get a freshly computed path.

    Pure download phase: no database access at all (no write transaction
    is ever held across network I/O).  Failed downloads are returned as
    ``(novel_id, reason)`` records for the caller to persist inside the
    write transaction.

    An empty ``webview_novel`` response (deleted/restricted novel) is
    recorded as a failure too, with reason "webview_novel 返回空" — the
    same ledger convention as the single-novel path, so both paths leave a
    trace for permanently-gone content.
    """
    if not novel_ids:
        return [], []

    async def _fetch_one(nid: int) -> NovelDraft | None:
        data = await fetch_novel_and_assets(
            nid, client, file_storage, image_downloader, redownload,
            stored_path=(stored_paths or {}).get(nid),
        )
        if data is None:
            logger.warning(f"下载: #{nid} webview 返回空")
        return data

    results = await asyncio.gather(
        *[_fetch_one(nid) for nid in novel_ids],
        return_exceptions=True,
    )

    valid: list[NovelDraft] = []
    failed_records: list[tuple[int, str]] = []
    for nid, result in zip(novel_ids, results):
        if isinstance(result, Exception):
            logger.error(
                f"下载: #{nid} 失败: {type(result).__name__}: {result}"
            )
            failed_records.append((nid, str(result)))
        elif result is None:
            failed_records.append((nid, "webview_novel 返回空"))
        else:
            valid.append(result)

    # Chapter numbers that the webview navigation could not anchor (the series'
    # only volume, a missing neighbour pointer) are resolved here, from the
    # series roster — the same authority the nightly repair uses, fetched once
    # per series per round.  Doing it *now* is the point: the value is written
    # in the same transaction that inserts the row, and ``series_index`` is
    # fill-once, so nothing re-derives it afterwards.
    if valid and client is not None:
        valid = await resolve_series_indices(valid, client)

    logger.info(
        f"_download_novels: {len(valid)}/{len(novel_ids)} downloaded successfully",
    )
    return valid, failed_records


# ---------------------------------------------------------------------------
# Plan / persist phases
# ---------------------------------------------------------------------------


async def _plan_batch(
    novels: list[NovelInfoLike],
    uow,
    redownload: bool = False,
    failed_repo=None,
) -> tuple[list[NovelDraft], list[int], dict[int, str]]:
    """Plan phase (read-only): decide what to download and what to upsert.

    Must run outside the write transaction — it only reads.  Returns
    ``(existing_meta, download_ids, stored_paths)`` where ``existing_meta``
    holds the metadata dicts of already-known novels (empty when
    ``redownload``), ``download_ids`` are the novel IDs that still need
    downloading, and ``stored_paths`` maps every already-known id to the file
    anchor in the database.

    ``stored_paths`` exists because ``novel.path`` is written once and then
    only ever filled when NULL: it is a file anchor, not a derived value.
    The download phase must write to *that* path rather than to
    ``build_path(id, current_title)`` — otherwise a work the author renames
    gets its body stored under a new filename while the read path (download
    / export / delete) keeps resolving the old one, so readers silently get
    stale text and the "did the body change?" check compares against a file
    that does not exist.
    """
    if not novels:
        return [], [], {}

    ids = {n.id for n in novels}
    repo = SQLAlchemyNovelRepository(uow.session)
    existing = await repo.get_existing_ids(ids)

    # Exclude novels that have already failed too many times
    skip_ids: set[int] = set()
    if failed_repo is not None:
        need_check = ids if redownload else ids - existing
        skip_ids = failed_repo.get_skip_ids(need_check)

    need_download = ids if redownload else ids - existing - skip_ids

    logger.info(
        f"_plan_batch: {len(novels)} novels — "
        f"{len(existing)} in DB, {len(skip_ids)} failed-too-many-times, "
        f"{len(need_download)} need download",
    )

    existing_meta: list[NovelDraft] = []
    if not redownload:
        existing_meta = [
            build_from_novel_info(n) for n in novels if n.id in existing
        ]

    # Dedup: the same id can appear twice in one discovered batch (a ranking
    # / search page shifted between two offset requests).  Keeping both would
    # download the body twice and then INSERT the same primary key twice in
    # one transaction — a UniqueViolation that rolls back the *whole* batch,
    # losing every other novel in it.
    download_ids = list(dict.fromkeys(
        n.id for n in novels if n.id in need_download
    ))
    stored_paths = (
        await repo.get_stored_paths(set(download_ids)) if download_ids else {}
    )
    return existing_meta, download_ids, stored_paths


async def _upsert_batch(
    existing_meta: list[NovelDraft],
    downloaded: list[NovelDraft],
    uow,
    *,
    failed_records: list[tuple[int, str]],
    failed_repo,
    titles: dict[int, str],
) -> tuple[list[str], int]:
    """Persist phase（只写）：在调用方的短写事务里跑。

    记录失败、upsert 已有元数据与刚下载的条目、清掉成功项的失败标记 —— 全部
    落在**调用方**的事务里（本函数从不自己开事务，也不 commit）。返回
    ``(本轮下载到的标题, 新插入行数)``。

    失败记录写在 upsert **之后**：一本既下载成功、又 asset 处理失败的书会被
    上面那条路径写进库（于是满足了 ``failed_novel`` 的外键）。而在持久化之前
    就失败的**新**书没有 ``novel`` 行、按外键记不进去——那些被跳过，下一轮抓取
    自然重试（见 ``FailedNovelRepository.record``）。
    """
    new_count = 0
    titles_out: list[str] = []

    if existing_meta:
        new_count += await batch_upsert(existing_meta, uow)
        logger.info(f"_upsert_batch: metadata upsert done for {new_count} existing novels")

    if downloaded:
        success_ids = {d.id for d in downloaded}
        # ``path`` is fill-once by default ("non-empty wins"), because it is the
        # anchor every reader resolves and rewriting it would orphan the file
        # that is already there.  The download path is the one writer allowed to
        # replace it: ``FileStorage.adopt_anchor`` refuses a stored path that
        # escapes the root, or that is too long to write a ``.tmp`` beside (253
        # bytes stored + ".tmp" > NAME_MAX — three production rows), and the
        # body is then written to a freshly computed name.  The row must follow,
        # or the file lands where nothing reads it.
        #
        # Scoped to *this* call site rather than added to the repository's
        # global ``update_fields_set``: ``sync_novel`` also upserts drafts, and
        # its contract is explicitly "a source-side rename must NOT move the
        # path" (``tests/api/test_novel_sync.py``).  Downloading is the only
        # path that may have just written a different file.
        new_count += await batch_upsert(downloaded, uow, force_update=["path"])
        # A novel that both downloaded successfully AND has a recorded failure
        # (e.g. an asset-processing failure) keeps its ledger entry — forgetting
        # it would erase the "downloaded but asset failed" trace.  Only clean
        # successes clear the ledger.
        failed_ids = {nid for nid, _ in failed_records}
        failed_repo.forget_many(success_ids - failed_ids)
        titles_out = [d.title for d in downloaded if d.title]
        logger.info(f"_upsert_batch: {len(downloaded)} novels downloaded and upserted")

    for nid, reason in failed_records:
        failed_repo.record(nid, "download", reason, title=titles.get(nid))

    return titles_out, new_count


# ---------------------------------------------------------------------------
# Ingest outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestOutcome:
    """Result of one :func:`ingest` call.

    ``titles`` lists the titles of novels **fetched and persisted** this call;
    ``new_author_ids`` is the set of authors seen among downloaded novels;
    ``failed`` carries every recorded failure ``(novel_id, reason)``
    (webview-empty / network / asset processing);
    ``new_count`` is the number of newly-inserted novel rows (0 when the
    novel already existed);
    ``rewritten_ids`` are the novels whose **body file this call replaced**.

    ``new_count`` and ``rewritten_ids`` answer two different questions and
    neither implies the other, which is why both exist:

    * a *new* novel is always written (``new_count`` 1 → also rewritten);
    * a *re-download* of a known novel writes nothing when the body is
      byte-identical (that is the content-change check in
      ``download_novel``), but **does** write when the author edited the
      text — ``new_count`` stays 0 in both cases.

    The single-novel adapter used to fold them together (``titles and
    new_count``), so a re-download that genuinely replaced the text still
    reported 「已存在，跳过」.  Reporting the rewrite separately is what lets
    the summary state what actually happened without re-deriving it from
    ``titles``.
    """

    titles: list[str] = field(default_factory=list)
    new_author_ids: set[int] = field(default_factory=set)
    failed: list[tuple[int, str]] = field(default_factory=list)
    new_count: int = 0
    rewritten_ids: set[int] = field(default_factory=set)

    @property
    def failed_ids(self) -> list[int]:
        """The ids behind :attr:`failed`, in first-seen order and deduped.

        Task adapters put this on :class:`~copixiv.core.models.TaskResult` so
        the outcome of a batch is machine-readable.  Without it a partially
        failed run is indistinguishable from a clean one: the summary says
        "新增 N 本" either way, and the per-item failures only exist in the
        ledger and the log text.
        """
        return list(dict.fromkeys(nid for nid, _ in self.failed))


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
#
# ``ingest`` 读起来应该就是它自己的编号：plan → download → gate → collect →
# persist。每一步都在这里是一个**命名函数**，因为它各有一条自己的纪律（哪一步
# 可以碰数据库、哪一步不能持写事务），而且各自有独立的失败语义。写成一步一个
# 内联块的时候，这些纪律只能靠注释区分；写成函数之后函数边界就是它们。

@dataclass(frozen=True)
class _RoundPlan:
    """整轮的计划：已知的元数据、真正要下载的 id、已有行的落盘锚点。"""

    existing_meta: list[NovelDraft]
    download_ids: list[int]
    stored_paths: dict[int, str]


@dataclass(frozen=True)
class _AssetOutcome:
    """图片 / EPUB 收尾的结果：本轮的失败、以及真的做完的 id。"""

    failures: list[tuple[int, str]]
    done_ids: list[int]


@dataclass(frozen=True)
class _AuthorNames:
    """已解析的作者名，连同"这一轮见过哪些作者"一起带走。"""

    mapping: dict[int, str]
    author_ids: set[int]


async def _plan_round(
    novels: list[NovelInfoLike] | None,
    ids: list[int] | None,
    uow: SqlUnitOfWork,
    *,
    force: bool,
) -> _RoundPlan:
    """Plan phase（只读、短事务）—— 决定下载什么、upsert 什么。

    *novels* 与 *ids* 是两条入口：前者带来源元数据，后者只是"我要这些编号"
    （单本 ``novel_fetch``）。两条都归结成一个 :class:`_RoundPlan`，之后的
    阶段因此不必再问"是哪条入口进来的"。
    """
    if novels:
        existing_meta, download_ids, stored_paths = await _plan_batch(
            novels, uow, redownload=force,
            failed_repo=FailedNovelRepository(uow.session),
        )
        return _RoundPlan(existing_meta, download_ids, stored_paths)

    download_ids = list(dict.fromkeys(ids or []))
    stored_paths: dict[int, str] = {}
    # Single-novel path: the id may already be known (re-download), in which
    # case its body must go to the stored anchor, not to a path recomputed
    # from a title that may have changed at the source.
    if download_ids:
        async with uow.begin():
            stored_paths = await SQLAlchemyNovelRepository(
                uow.session
            ).get_stored_paths(set(download_ids))
    return _RoundPlan([], download_ids, stored_paths)


async def _download_round(
    download_ids: list[int],
    client,
    file_storage,
    image_downloader,
    *,
    force: bool,
    stored_paths: dict[int, str],
) -> tuple[list[NovelDraft], list[tuple[int, str]]]:
    """Download phase —— 并发网络 / 文件 I/O，**不碰数据库**。

    没有 client 时，要下载的那些 id 记成**失败**而不是静默跳过。装配阶段被裁掉
    的客户端不该让整轮抛异常（"这些 id 需要下载"仍然是真的），但也绝不能读成
    成功：那条路径会让 ``novel_fetch`` 落到「已存在，跳过」的汇总，于是任务行
    写着 success，而书一本都没下、行也不存在（2026-10-24 探针复现）。失败进
    ``failed_novel`` 台账，与真正的下载失败同一条流水线——operator 能在
    「下载失败」页看见它们，重试也有入口。
    """
    if not download_ids:
        return [], []
    if client is None:
        logger.warning(
            f"ingest: {len(download_ids)} novels need download "
            f"but no client provided — recording them as failed",
        )
        return [], [
            (nid, "缺少 pixiv 客户端（装配阶段未提供 client）")
            for nid in download_ids
        ]
    return await _download_novels(
        download_ids, client, file_storage, image_downloader,
        redownload=force, stored_paths=stored_paths,
    )


async def _settle_assets(image_downloader, downloaded: list[NovelDraft]) -> _AssetOutcome:
    """Gate phase —— 等本轮的后台图片 / EPUB 任务收尾。

    写事务因此只看见文件**确实在盘上**的条目（没有"下载完了但 EPUB 还没好"
    的竞态）。失败与"真的做完了"一起带回去，与下载失败在同一个事务里落库。

    ``await_all`` / ``drain_outcomes`` **都必须按本轮 id 限定**：ImageDownloader
    是应用级单例、并发的 ingest 共享它（扇出任务会 gather 若干个
    ``author_fetch``，每个都走到这里）。不限定的话，先到的那一轮会把别人那一轮
    的 future 领走，于是它 gate 了个寂寞——EPUB 落盘了而 ``has_epub`` 停在
    PENDING；drain 不限定则会把别人那一轮的 outcome 偷来，那条 UPDATE 打在本
    事务还没写入的行上，DONE 就丢了。
    """
    if image_downloader is None:
        return _AssetOutcome([], [])

    ids = [d.id for d in downloaded]
    failures = await image_downloader.await_all(ids)
    if failures:
        logger.warning(
            f"ingest: {len(failures)} novels failed image/EPUB processing",
        )

    # 生产方自报的结果：EPUB 管线自己说哪些做完了（"done"）、哪些不需要
    # （"skipped"）、哪些失败了。gate 之后立刻 drain —— 本轮每个 worker 到那时
    # 都已经登记过了。"done" 就是让 draft 的 has_epub=PENDING 在持久事务里变成
    # DONE 的依据，而不是等每周的对账任务（2026-09 根因："做完不置位"）。
    outcomes = image_downloader.drain_outcomes(ids)
    done_ids = [nid for nid, outcome in outcomes.items() if outcome == "done"]
    return _AssetOutcome(failures, done_ids)


async def _collect_names(
    downloaded: list[NovelDraft], uow: SqlUnitOfWork, client,
) -> _AuthorNames:
    """Collect phase —— 作者名解析，**在写事务之外**（免锁读 + 网络）。

    把"见到过哪些作者"和"解析出哪些名字"一起返回，因为它们来自同一批 draft
    的同一个字段：调用方要么两个都要（persist 收尾 + 任务结果），要么两个都不
    要。分成两次推导会让"这一轮见过的作者"在一个阶段被重算一遍。
    """
    author_ids = {d.author_id for d in downloaded if d.author_id}
    if not author_ids or client is None:
        return _AuthorNames({}, author_ids)
    return _AuthorNames(
        await collect_author_names(author_ids, uow=uow, client=client),
        author_ids,
    )


async def ingest(
    novels: list[NovelInfoLike] | None = None,
    *,
    ids: list[int] | None = None,
    force: bool = False,
    session_factory,
    client=None,
    file_storage=None,
    image_downloader=None,
) -> IngestOutcome:
    """Process a batch of novels end-to-end: plan → download → gate → collect → persist.

    The body below *is* that list — one named step per phase, each owning its
    own rule about when it may touch the database:

    ==================  ====================  ===================================
    phase               database              why it is shaped that way
    ==================  ====================  ===================================
    ``_plan_round``     read-only, short       decides what to download; must not
                        transaction           hold anything across the network
    ``_download_round`` **none**              concurrent webview + file I/O
    ``_settle_assets``  **none**              waits for in-flight image/EPUB work
    ``_collect_names``  lock-free reads       author names, local table first
    ``_persist_round``  one short write       upsert + status + writeback, atomic
                        transaction
    ==================  ====================  ===================================

    Concurrent calls are safe: no phase holds a write transaction across network
    I/O, and the persist phase is a ``run_write_transaction`` that retries
    transient LockNotAvailable instead of serializing every write through a
    global lock.

    When *novels* is provided, the plan phase decides what to download.  When
    *novels* is omitted but *ids* is given, every id is downloaded
    unconditionally (no plan) — this is the single-novel path
    (``novel_fetch``); the failure ledger still gets its record/forget
    semantics through the persist phase.

    Author-name resolution is two-phase: :func:`_collect_names` (i.e.
    :func:`collect_author_names`) runs *outside* the write transaction, and
    :func:`writeback_author_names` runs *inside* the same short persist
    transaction — the final D3 shape.
    """
    if not novels and not ids:
        return IngestOutcome()

    uow = SqlUnitOfWork(session_factory)

    plan = await _plan_round(novels, ids, uow, force=force)

    downloaded, failed_records = await _download_round(
        plan.download_ids, client, file_storage, image_downloader,
        force=force, stored_paths=plan.stored_paths,
    )

    assets = await _settle_assets(image_downloader, downloaded)

    names = await _collect_names(downloaded, uow, client)

    titles, new_count = await run_write_transaction(
        uow,
        lambda write_uow: _persist_round(
            write_uow,
            plan.existing_meta,
            downloaded,
            failed_records=failed_records + assets.failures,
            titles=_titles_by_id(novels, downloaded),
            done_ids=assets.done_ids,
            mapping=names.mapping,
        ),
    )

    return IngestOutcome(
        titles=titles,
        new_author_ids=names.author_ids,
        failed=failed_records + assets.failures,
        new_count=new_count,
        # 正文文件这一轮被替换过的 id。只有下载阶段知道这件事（内容比较发生在
        # 那里），而 ``new_count`` 答不出它：改写正文的重抓不插入新行。
        rewritten_ids={d.id for d in downloaded if d.rewrote_body},
    )


def _titles_by_id(
    novels: list[NovelInfoLike] | None, downloaded: list[NovelDraft],
) -> dict[int, str]:
    """``{novel_id: title}`` —— 失败台账里给人看的标签，**不为此再问 pixiv**。

    ``novels`` 里的条目优先（来源元数据本来就在手上），``downloaded`` 补上它的
    空档。单本路径（只有 ``ids``）没有元数据入口，于是 asset 失败靠下载到的
    draft 填标签；两处都没有的，标签就留空——那是"这条失败没有可显示的名字"，
    比编一个 ID 字符串好。
    """
    titles: dict[int, str] = {}
    for n in (novels or []):
        if getattr(n, "title", None):
            titles.setdefault(n.id, n.title)
    for draft in downloaded:
        if draft.title:
            titles.setdefault(draft.id, draft.title)
    return titles


async def _persist_round(
    uow: SqlUnitOfWork,
    existing_meta: list[NovelDraft],
    downloaded: list[NovelDraft],
    *,
    failed_records: list[tuple[int, str]],
    titles: dict[int, str],
    done_ids: list[int],
    mapping: dict[int, str],
) -> tuple[list[str], int]:
    """Persist phase —— 一个短写事务里的全部写操作，**没有全局写锁**。

    ``run_write_transaction`` 在 LockNotAvailable（SQLSTATE 55P03）上退避重试，
    而不是把每一次写都串行化：扇出任务并不持全局锁，热点标签行上短暂的争用
    应该自愈。

    顺序是有意义的：先 upsert（draft 的 has_epub=PENDING 落库），再按生产方
    自报的结果把它翻成 DONE。同一个事务里没有中间态对外可见。
    """
    titles_out, new_count = await _upsert_batch(
        existing_meta, downloaded, uow,
        failed_records=failed_records,
        failed_repo=FailedNovelRepository(uow.session),
        titles=titles,
    )
    if done_ids:
        # 仓库方法是协程（``repo.py::update_has_epub_status``）：漏掉 ``await``
        # 是一次**静默丢写**，不会报错。
        await SQLAlchemyNovelRepository(uow.session).update_has_epub_status(
            done_ids, EpubStatus.DONE,
        )
        logger.info(f"_persist: {len(done_ids)} novels marked EPUB DONE")
    if mapping:
        await writeback_author_names(mapping, uow)
    return titles_out, new_count
