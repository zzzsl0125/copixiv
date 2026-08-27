"""Unified ingestion pipeline — plan → download → gate → persist → writeback.

This module is the single ingestion pipeline for every novel-fetching task.
Registered tasks (``tasks/novels.py``) are now thin adapters over
:func:`ingest`: they only do the discovery/enumeration work (ranking,
search, follow feeds, author catalogues) and then hand the collected novel
list (or an explicit id list) to :func:`ingest`, which owns the rest:

1. plan (read-only, no write lock) — decide what still needs downloading;
2. download (concurrent, no DB access);
3. asset gate (``image_downloader.await_all()``);
4. author-name collect (lock-free) before the write transaction;
5. persist + author-name writeback in a single ``db_write()`` transaction.

Nothing here depends on :class:`~copixiv.tasks.kernel.TaskContext` — task
adapters pass the fields they need (``session_factory``, ``client``,
``file_storage``, ``image_downloader``) explicitly.
"""

import asyncio
import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import db_write

from copixiv.core.draft import NovelDraft, NovelInfoLike, build_from_novel_info
from copixiv.core.services import is_chinese, safe_get
from copixiv.features.novels.download_novel import fetch_novel_and_assets
from copixiv.features.novels.persist import persist_novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.failures.repo import FailedNovelRepository
from copixiv.features.authors.resolve_names import collect_author_names, writeback_author_names

from copixiv.log import logger


# ---------------------------------------------------------------------------
# Pure helpers (no side effects)
# ---------------------------------------------------------------------------


def _month_ranges(end_date: datetime, months: int):
    for offset in range(months):
        if offset == 0:
            yield end_date.replace(day=1), end_date
        else:
            y, m = end_date.year, end_date.month
            for _ in range(offset):
                m -= 1
                if m == 0:
                    m = 12
                    y -= 1
            last_day = calendar.monthrange(y, m)[1]
            yield datetime(y, m, 1), datetime(y, m, last_day)


def _filter_chinese_novels(novels: list) -> list:
    """Return only Chinese-language novels from a Pixiv API response list."""
    return [
        n for n in novels
        if is_chinese(
            title=safe_get(n, "title", ""),
            caption=safe_get(n, "caption", ""),
            tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", [])],
        )
    ]


# ---------------------------------------------------------------------------
# Batch upsert
# ---------------------------------------------------------------------------


async def batch_upsert(
    novels: list[NovelDraft], uow, force_update: list[str] | None = None,
) -> int:
    """Upsert novels and update author/series summaries.

    Thin wrapper over :func:`copixiv.features.novels.persist.persist_novels`
    that adds batch-level logging.

    Pure write helper: the caller is responsible for wrapping it in
    ``db_write()`` + ``uow.begin()`` so the whole batch (including the
    commit) happens while holding the global write lock.
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
) -> tuple[list[NovelDraft], list[tuple[int, str]]]:
    """Download novels via webview concurrently.

    Fires all webview_novel calls in parallel — the client semaphore + LRU
    account selection distribute the load across accounts automatically.
    Each novel's text, images, and EPUB are processed as it completes.

    Pure download phase: no database access at all (the write lock is
    never held across network I/O).  Failed downloads are returned as
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
) -> tuple[list[NovelDraft], list[int]]:
    """Plan phase (read-only): decide what to download and what to upsert.

    Must run outside the write lock — it only reads.  Returns
    ``(existing_meta, download_ids)`` where ``existing_meta`` holds the
    metadata dicts of already-known novels (empty when ``redownload``),
    and ``download_ids`` are the novel IDs that still need downloading.
    """
    if not novels:
        return [], []

    ids = {n.id for n in novels}
    existing = await SQLAlchemyNovelRepository(uow.session).get_existing_ids(ids)

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

    download_ids = [n.id for n in novels if n.id in need_download]
    return existing_meta, download_ids


async def _persist_batch(
    existing_meta: list[NovelDraft],
    downloaded: list[NovelDraft],
    uow,
    failed_records: list[tuple[int, str]] | None = None,
    failed_repo=None,
    titles: dict[int, str] | None = None,
) -> tuple[list[str], set[int], int]:
    """Persist phase (write-only): run inside ``db_write()`` + ``uow.begin()``.

    Records download failures, upserts existing metadata + downloaded
    novels, and forgets success markers — all in the caller's write
    transaction.  Returns ``(titles, new_author_ids, new_count)`` where
    ``new_count`` is the number of newly-inserted novels.
    """
    title_map = titles or {}
    titles: list[str] = []
    new_author_ids: set[int] = set()
    new_count: int = 0

    # Failed downloads — recorded in the same write transaction.
    if failed_records and failed_repo is not None:
        for nid, reason in failed_records:
            failed_repo.record(
                nid, "download", reason, title=title_map.get(nid),
            )

    # Metadata-only upsert for existing
    if existing_meta:
        upserted = await batch_upsert(existing_meta, uow)
        new_count += upserted
        logger.info(
            f"_persist_batch: metadata upsert done for {upserted} existing novels",
        )

    if downloaded:
        success_ids = {d.id for d in downloaded}
        upserted = await batch_upsert(downloaded, uow)
        new_count += upserted
        if failed_repo is not None:
            # A novel that both downloaded successfully AND has a recorded
            # failure (e.g. an asset-processing failure) keeps its ledger
            # entry — forgetting it would erase the "downloaded but asset
            # failed" trace.  Only clean successes clear the ledger.
            failed_ids = {nid for nid, _ in (failed_records or [])}
            failed_repo.forget_many(success_ids - failed_ids)
        titles = [d.title for d in downloaded if d.title]
        new_author_ids = {d.author_id for d in downloaded if d.author_id}
        logger.info(
            f"_persist_batch: {len(downloaded)} novels downloaded and upserted",
        )

    return titles, new_author_ids, new_count


# ---------------------------------------------------------------------------
# Ingest outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestOutcome:
    """Result of one :func:`ingest` call.

    ``titles`` lists the titles of novels downloaded this call;
    ``new_author_ids`` is the set of authors seen among downloaded novels;
    ``failed`` carries every recorded failure ``(novel_id, reason)``
    (webview-empty / network / asset processing);
    ``new_count`` is the number of newly-inserted novel rows (0 when the
    novel already existed — lets the single-novel adapter distinguish
    "downloaded" from "already known / skipped").
    """

    titles: list[str] = field(default_factory=list)
    new_author_ids: set[int] = field(default_factory=set)
    failed: list[tuple[int, str]] = field(default_factory=list)
    new_count: int = 0


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


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
    """Process a batch of novels end-to-end: plan → download → persist.

    The pipeline keeps every database write inside ``db_write()`` and
    never holds a transaction (or the write lock) across network
    downloads.  Concurrent calls are safe: the plan phase reads without
    the lock, the download phase touches no database, the collect phase
    resolves author names without the lock, and the persist phase
    serializes through ``db_write()``.

    When *novels* is provided, the plan phase decides what to download.
    When *novels* is omitted but *ids* is given, every id is downloaded
    unconditionally (no plan) — this is the single-novel path
    (``novel_fetch``); the failure ledger still gets its record/forget
    semantics through the persist phase.

    Author-name resolution is two-phase: :func:`collect_author_names` runs
    *outside* the write lock (lock-free reads + network), and
    :func:`writeback_author_names` runs *inside* the same ``db_write()``
    transaction as the persist — the final D3 shape.
    """
    if not novels and not ids:
        return IngestOutcome()

    uow = SqlUnitOfWork(session_factory)

    # 1. Plan — read-only, short transaction, no lock.
    if novels:
        async with uow.begin():
            existing_meta, download_ids = await _plan_batch(
                novels, uow, redownload=force,
                failed_repo=FailedNovelRepository(uow.session),
            )
    else:
        existing_meta: list[NovelDraft] = []
        download_ids: list[int] = list(ids or [])

    # 2. Download — concurrent network/file I/O, no database.
    downloaded: list[NovelDraft] = []
    failed_records: list[tuple[int, str]] = []
    if download_ids and client is not None:
        downloaded, failed_records = await _download_novels(
            download_ids, client, file_storage, image_downloader,
            redownload=force,
        )
    elif download_ids:
        logger.warning(
            f"ingest: {len(download_ids)} novels need download "
            f"but no client provided — skipping download",
        )

    # 2.5 Gate — wait for in-flight image/EPUB tasks before persisting, so
    # the write transaction only sees novels whose files are actually on
    # disk (no "downloaded but EPUB not ready yet" race).  Failures are
    # collected and persisted in the same transaction as the downloads.
    asset_failures: list[tuple[int, str]] = []
    if image_downloader is not None:
        asset_failures = await image_downloader.await_all()
        if asset_failures:
            logger.warning(
                f"ingest: {len(asset_failures)} novels failed "
                f"image/EPUB processing",
            )

    # 3. Collect author names — lock-free, before the write transaction.
    new_author_ids: set[int] = {
        d.author_id for d in downloaded if d.author_id
    }
    mapping: dict[int, str] = {}
    if new_author_ids and client is not None:
        mapping = await collect_author_names(new_author_ids, uow=uow, client=client)

    # 4. Persist — one write transaction inside the global write lock.
    # Titles accompany the failure records so the "下载失败" view can show
    # a human-readable label without querying Pixiv again.  In the *ids*
    # path there is no novelInfo payload, so downloaded titles fill in the
    # label for asset failures.
    titles_by_id: dict[int, str] = {}
    for n in (novels or []):
        if getattr(n, "title", None):
            titles_by_id.setdefault(n.id, n.title)
    for d in downloaded:
        if d.title:
            titles_by_id.setdefault(d.id, d.title)

    async with db_write():
        async with uow.begin():
            titles, _new_author_ids, new_count = await _persist_batch(
                existing_meta, downloaded, uow,
                failed_records=failed_records + asset_failures,
                failed_repo=FailedNovelRepository(uow.session),
                titles=titles_by_id,
            )
            if mapping:
                await writeback_author_names(mapping, uow)

    return IngestOutcome(
        titles=titles,
        new_author_ids=new_author_ids,
        failed=failed_records + asset_failures,
        new_count=new_count,
    )
