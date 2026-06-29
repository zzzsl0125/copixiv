"""Use case: download a single novel from Pixiv."""

import asyncio
import logging

from copixiv.domain.services.novel_factory import build_from_webview
from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.repositories.author import AuthorRepository
from copixiv.infrastructure.repositories.series import SeriesRepository
from copixiv.infrastructure.storage.file_storage import FileStorage
from copixiv.infrastructure.storage.image_downloader import ImageDownloader

logger = logging.getLogger("copixiv")


class DownloadNovelUseCase:
    """Download a single novel from Pixiv and persist it."""

    def __init__(
        self,
        client,
        novel_repo: NovelRepository,
        author_repo: AuthorRepository,
        series_repo: SeriesRepository,
        file_storage: FileStorage,
        image_downloader: ImageDownloader,
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
        resp = await self._client.webview_novel(novel_id)
        if resp is None:
            return 0

        data = build_from_webview(resp, self._file_storage.download_dir)

        # Save text
        if content := data.pop("content", None):
            self._file_storage.save_novel_text(
                data["id"], data["title"], content, force=redownload
            )

        # Download assets in background
        await self._image_downloader.process_novel_assets(
            data, force=redownload
        )

        # Upsert
        count = await self._novel_repo.upsert_novels([data])
        if count:
            await self._author_repo.update_summary({data["author_id"]})
            if sid := data.get("series_id"):
                await self._series_repo.update_summary({sid})

        return count
