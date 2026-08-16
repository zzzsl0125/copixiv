"""Batch ZIP archive builder — pure function."""

import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .filename import NovelNamingTemplate
from copixiv.domain.models.novel import EpubStatus

# Buffers up to 8 MB stay in memory; larger batches (e.g. 200 EPUBs)
# spill to a disk-backed temp file instead of exhausting RAM.
_SPOOL_MAX = 8 * 1024 * 1024

# Progress callback fires every N processed files (used by the background
# batch_export task to report live progress).
_PROGRESS_EVERY = 500


def build_batch_zip(
    novels: list[dict[str, Any]],
    format_mode: str = "txt",
    naming_template: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[BinaryIO, list[str], list[str]]:
    """Build a ZIP of novel files matching the given criteria.

    Args:
        novels: List of novel dicts, each containing at least ``id``, ``path``,
                ``title``, ``author_name``, ``series_id``, ``series_name``,
                ``series_index``, and ``has_epub``.
        format_mode: ``'txt'`` or ``'prefer_epub'`` (prefers EPUB when available).
        naming_template: Token-based naming template for ZIP arcnames.
                Defaults to ``{author_name}/{series_name}/#{series_index}_{title}_{id}``.
        progress: Optional callback ``(processed, total)`` invoked every
                ~500 files — lets long-running exports report progress.

    Returns:
        ``(zip_buffer, added_titles, missing_ids)`` — the ZIP as a seekable
        binary file object (spooled: memory up to 8 MB, then disk), the
        titles successfully added, and the IDs whose files were missing.
    """
    zip_buffer: BinaryIO = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX)
    added_titles: list[str] = []
    missing_ids: list[str] = []

    template = NovelNamingTemplate(
        naming_template or "{author_name}/{series_name}/#{series_index}_{title}_{id}"
    )

    try:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for index, novel in enumerate(novels, start=1):
                novel_id = novel.id
                novel_path_str = novel.path
                title = novel.title or str(novel_id)

                actual_fmt = (
                    "epub"
                    if (format_mode == "prefer_epub"
                        and novel.has_epub == EpubStatus.DONE)
                    else "txt"
                )

                if not novel_path_str:
                    missing_ids.append(str(novel_id))
                else:
                    file_path = Path(novel_path_str).with_suffix("." + actual_fmt)
                    if not file_path.is_file():
                        missing_ids.append(str(novel_id))
                    else:
                        arcname = template.resolve(novel) + "." + actual_fmt
                        zf.write(str(file_path), arcname)
                        added_titles.append(title)

                if progress is not None and index % _PROGRESS_EVERY == 0:
                    progress(index, len(novels))
    except Exception:
        # Never leak the spooled file on partial builds.
        zip_buffer.close()
        raise

    return zip_buffer, added_titles, missing_ids
