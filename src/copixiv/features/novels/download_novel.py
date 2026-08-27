"""Fetch a single novel's text + assets — the shared query half of ingest.

The canonical download fetch (``fetch_novel_and_assets``) is consumed by
:func:`copixiv.features.novels.ingest.ingest` — the single ingestion
pipeline shared by every novel-fetching task.  Persistence and author-name
resolution live in ``ingest``; this module owns only the network + file
half of the journey so the two paths never drift apart.
"""

from copixiv.core.exceptions import NovelNotFoundError
from copixiv.core.draft import NovelDraft, build_from_webview
from copixiv.pixiv.client import PixivClient
from copixiv.storage.file_storage import FileStorage
from copixiv.storage.image_downloader import ImageDownloader

from copixiv.log import logger


async def fetch_novel_and_assets(
    novel_id: int,
    client: PixivClient,
    file_storage: FileStorage,
    image_downloader: ImageDownloader,
    redownload: bool = False,
) -> NovelDraft | None:
    """Fetch a novel from Pixiv and persist its text + assets.

    Consumed by :func:`copixiv.features.novels.ingest.ingest` (which
    persists later, inside the write lock).  This is the first part of the
    download journey; the repository write happens in ``ingest``'s persist
    phase.

    Returns the canonical :class:`~copixiv.core.draft.NovelDraft`
    (transient ``content`` still attached — the repository's column
    whitelist excludes it from the DB) or ``None`` when the novel does not
    exist / is not fetchable.
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
