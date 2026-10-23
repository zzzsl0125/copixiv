"""Fetch a single novel's text + assets — the shared query half of ingest.

The canonical download fetch (``fetch_novel_and_assets``) is consumed by
:func:`copixiv.features.novels.ingest.ingest` — the single ingestion
pipeline shared by every novel-fetching task.  Persistence and author-name
resolution live in ``ingest``; this module owns only the network + file
half of the journey so the two paths never drift apart.

Content-change detection (2026-09 拍板) lives here too, because this is the
only place that holds both bodies at once: the freshly fetched text and the
text on disk.  The comparison is done by reading the file and hashing it on
the spot — **nothing is persisted**, the disk *is* the truth about the local
copy.  Cost, measured on realistic Chinese bodies: read + sha256 < 2 ms per
novel (a 100 k-character novel is ~0.85 ms + ~0.85 ms), against a network
fetch that is two to three orders of magnitude slower — and it only runs for
novels that are actually re-fetched.
"""

from dataclasses import replace

from copixiv.core.exceptions import NovelNotFoundError
from copixiv.core.draft import NovelDraft, build_from_webview
from copixiv.core.services import body_unchanged
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
    stored_path: str | None = None,
) -> NovelDraft | None:
    """Fetch a novel from Pixiv and persist its text + assets.

    Consumed by :func:`copixiv.features.novels.ingest.ingest` (which
    persists later, inside the write lock).  This is the first part of the
    download journey; the repository write happens in ``ingest``'s persist
    phase.

    *stored_path* is the row's existing ``novel.path`` (``None`` for a novel
    new to this database).  When it is a usable write target — inside the
    download root — the fetched draft adopts it instead of recomputing
    ``build_path`` from the current title: the stored path is the anchor every
    reader resolves (download / export / delete all use it), so writing the
    body anywhere else makes the fresh text unreachable.  A stored path
    outside the root is ignored (the row is polluted) and a fresh path is
    computed, so the novel stays fetchable.  See
    :meth:`FileStorage.save_novel_text` for the same reasoning.

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
    if anchor := file_storage.adopt_anchor(stored_path):
        data = replace(data, path=anchor)

    # Text: write it only when it actually differs from the local copy.
    # ``rewrote`` is the single decision this module makes, and it drives two
    # consequences (2026-09 拍板):
    #   1. the asset pipeline's ``force`` — a body that did not change must not
    #      trigger an EPUB rebuild (the old build already matches the file);
    #   2. ``has_epub`` — the draft carries a freshly detected status, which is
    #      only meaningful when *this* run rewrote the text file.  Passing
    #      ``None`` means "don't touch the column" (``_upsert_rows`` pops it),
    #      so the status stays owned by the EPUB producer and the reconciler.
    # Note the write below passes ``force=True``: ``save_novel_text``'s own
    # "file exists → skip" heuristic is exactly what the comparison just
    # overruled, so it must not be allowed to veto the write a second time.
    unchanged = False
    if data.content:
        local = file_storage.read_novel_text(
            data.id, data.title, anchor=data.path,
        )
        unchanged = (not redownload) and body_unchanged(local, data.content)

    rewrote = bool(data.content) and not unchanged
    if rewrote:
        file_storage.save_novel_text(
            data.id, data.title, data.content, force=True,
            # ``data.path`` is the anchor: the stored path for a known row,
            # the freshly computed ``build_path`` for a first-time one.  Read
            # and write therefore always agree on the same file.
            anchor=data.path,
        )
    elif data.content:
        logger.info(f"下载: #{data.id} 正文未变，保留本地副本")

    if not rewrote:
        # 没重写正文文件（内容相同，或 webview 根本没给正文）→ 不重新判定
        # EPUB 状态：None = 库侧"别覆盖"。
        data = replace(data, has_epub=None)

    # Download assets in background.  Still called when the body is unchanged
    # — that path also heals a missing/corrupt EPUB — but without force, so an
    # existing valid EPUB short-circuits (image_downloader.py:210-227).
    await image_downloader.process_novel_assets(
        data, force=redownload or rewrote,
    )

    return data
