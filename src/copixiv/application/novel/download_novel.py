"""Use case: download a single novel from Pixiv and persist it.

The canonical single-novel download flow — consumed by the ``novel_fetch``
background task.  The batch pipeline (``tasks/pipeline.py``) reuses
:func:`fetch_novel_and_assets` for the fetch half and
:func:`copixiv.application.novel.persist.persist_novels` for the write half,
so the two paths never drift apart.
"""

from copixiv.domain.exceptions import NovelNotFoundError
from copixiv.domain.models.novel import Novel
from copixiv.domain.models.task_result import TaskResult
from copixiv.domain.services.novel_factory import build_from_webview
from copixiv.domain.ports.unit_of_work import UnitOfWork
from copixiv.domain.ports.storage import FileStoragePort
from copixiv.domain.ports.storage import ImageDownloaderPort
from copixiv.domain.ports.pixiv import PixivNovelPort
from copixiv.domain.ports.write_lock import WriteLockPort

from copixiv.application.author.resolve_names import resolve_author_names
from copixiv.application.novel.persist import persist_novels
from copixiv.log import logger


async def fetch_novel_and_assets(
    novel_id: int,
    client: PixivNovelPort,
    file_storage: FileStoragePort,
    image_downloader: ImageDownloaderPort,
    redownload: bool = False,
) -> Novel | None:
    """Fetch a novel from Pixiv and persist its text + assets.

    Shared by :class:`DownloadNovelUseCase` (persists immediately) and the
    batch pipeline (``tasks/pipeline.py`` — persists later, inside the
    write lock).  Both do the same first four steps of the download
    journey; only the persist step differs.

    Returns the canonical :class:`Novel` model (transient ``content`` still
    attached — the repository's column whitelist excludes it from the DB)
    or ``None`` when the novel does not exist / is not fetchable.
    Network errors and rate limits are NOT swallowed here — they bubble up
    to the client's retry loop and eventually fail the task loudly.
    """
    try:
        resp = await client.webview_novel(novel_id)
    except NovelNotFoundError:
        logger.warning(f"小说 #{novel_id} 不存在或无法获取，跳过")
        return None
    if resp is None:
        return None

    data = build_from_webview(resp, file_storage.download_dir)

    # Save text
    if data.content:
        file_storage.save_novel_text(
            data.id, data.title, data.content, force=redownload
        )

    # Download assets in background
    await image_downloader.process_novel_assets(data, force=redownload)

    return data


class DownloadNovelUseCase:
    """Download a single novel from Pixiv and persist it.

    Follows the same write discipline as the batch pipeline: asset work is
    gated via ``await_all()`` before persisting, every database write
    happens inside ``db_write()`` + ``uow.begin()``, and failures are
    recorded into ``failed_novel`` in the same transaction as the upsert.
    """

    def __init__(
        self,
        client: PixivNovelPort,
        uow: UnitOfWork,
        file_storage: FileStoragePort,
        image_downloader: ImageDownloaderPort,
        write_lock: WriteLockPort,
    ):
        self._client = client
        self._uow = uow
        self._file_storage = file_storage
        self._image_downloader = image_downloader
        self._write_lock = write_lock

    async def execute(
        self, novel_id: int, redownload: bool = False
    ) -> TaskResult:
        """Fetch + persist one novel.

        Returns a :class:`TaskResult` describing what happened (newly
        downloaded title / already-known skip / fetch failure).
        """
        data = await fetch_novel_and_assets(
            novel_id,
            self._client,
            self._file_storage,
            self._image_downloader,
            redownload,
        )
        if data is None:
            # A failed fetch must leave a trace in failed_novel, otherwise
            # permanently-gone novels silently linger forever.
            async with self._write_lock():
                async with self._uow.begin():
                    self._uow.failed_novels.record(
                        novel_id, "download", "webview_novel 返回空"
                    )
            return TaskResult(summary=f"小说 #{novel_id} 获取失败")

        # Gate: wait for in-flight image/EPUB work so the novel row is
        # persisted only after its files exist on disk.  Asset failures are
        # recorded in the same write transaction as the upsert.
        asset_failures = await self._image_downloader.await_all()

        async with self._write_lock():
            async with self._uow.begin():
                for nid, reason in asset_failures:
                    self._uow.failed_novels.record(
                        nid, "download", reason, title=data.title,
                    )
                count = await persist_novels(self._uow, [data])
                # A successful download clears the failure ledger entry —
                # otherwise a manual retry (or failed_retry) that succeeds
                # leaves a stale "failed" record forever, and the 「下载失败」
                # view would keep showing books that are actually fine.
                asset_failed_ids = {nid for nid, _ in asset_failures}
                if novel_id not in asset_failed_ids:
                    self._uow.failed_novels.forget(novel_id)

        # Resolve author name — webview API doesn't return it.
        if count:
            await resolve_author_names(
                {data.author_id}, client=self._client, uow=self._uow,
                write_lock=self._write_lock,
            )

        title = data.title
        if count:
            return TaskResult(
                summary=f"下载完成: {title}",
                new_novel_titles=[title],
                new_novel_count=count,
            )
        return TaskResult(summary=f"已存在，跳过: {title}")
