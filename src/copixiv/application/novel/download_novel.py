"""Use case: download a single novel from Pixiv."""

from copixiv.domain.services.novel_factory import build_from_webview
from copixiv.domain.ports.repositories import NovelRepository
from copixiv.domain.ports.repositories import AuthorRepository
from copixiv.domain.ports.repositories import SeriesRepository
from copixiv.domain.ports.storage import FileStoragePort
from copixiv.domain.ports.storage import ImageDownloaderPort
from copixiv.domain.ports.pixiv import PixivNovelPort


async def fetch_novel_and_assets(
    novel_id: int,
    client: PixivNovelPort,
    file_storage: FileStoragePort,
    image_downloader: ImageDownloaderPort,
    redownload: bool = False,
) -> dict | None:
    """Fetch a novel from Pixiv and persist its text + assets.

    Shared by :class:`DownloadNovelUseCase` (persists immediately) and the
    batch pipeline (``tasks/pipeline.py`` — persists later, inside the
    write lock).  Both do the same first four steps of the download
    journey; only the persist step differs.

    Returns the canonical novel dict (with ``content`` popped) or ``None``
    when the API returned nothing.
    """
    resp = await client.webview_novel(novel_id)
    if resp is None:
        return None

    data = build_from_webview(resp, file_storage.download_dir)

    # Save text
    if content := data.pop("content", None):
        file_storage.save_novel_text(
            data["id"], data["title"], content, force=redownload
        )

    # Download assets in background
    await image_downloader.process_novel_assets(data, force=redownload)

    return data


class DownloadNovelUseCase:
    """Download a single novel from Pixiv and persist it."""

    def __init__(
        self,
        client,
        novel_repo: NovelRepository,
        author_repo: AuthorRepository,
        series_repo: SeriesRepository,
        file_storage: FileStoragePort,
        image_downloader: ImageDownloaderPort,
    ):
        self._client = client
        self._novel_repo = novel_repo
        self._author_repo = author_repo
        self._series_repo = series_repo
        self._file_storage = file_storage
        self._image_downloader = image_downloader

    async def execute(
        self, novel_id: int, redownload: bool = False
    ) -> int:
        """Fetch + persist one novel. Returns count of new novels (0 or 1)."""
        data = await fetch_novel_and_assets(
            novel_id,
            self._client,
            self._file_storage,
            self._image_downloader,
            redownload,
        )
        if data is None:
            return 0

        # Gate: wait for in-flight image/EPUB work so the novel row is
        # persisted only after its files exist on disk.  Asset failures
        # are NOT persisted here — this use case has no failure-record
        # repository; pipeline._batch_handle and tasks.novel_fetch (which
        # own a FailedNovelRepository) collect them via await_all().
        await self._image_downloader.await_all()

        # Upsert, then refresh author/series summaries unconditionally —
        # like/view may have changed even for already-known novels, and
        # this matches the batch pipeline's behaviour (pipeline._batch_upsert).
        count = await self._novel_repo.upsert_novels([data])
        await self._author_repo.update_summary({data["author_id"]})
        if sid := data.get("series_id"):
            await self._series_repo.update_summary({sid})

        return count
