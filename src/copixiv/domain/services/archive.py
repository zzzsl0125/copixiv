"""Batch ZIP archive builder — pure function."""

import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from .filename import NovelNamingTemplate
from copixiv.domain.models.novel import EpubStatus

# Buffers up to 8 MB stay in memory; larger batches (e.g. 200 EPUBs)
# spill to a disk-backed temp file instead of exhausting RAM.
_SPOOL_MAX = 8 * 1024 * 1024


def build_batch_zip(
    novels: list[dict[str, Any]],
    format_mode: str = "txt",
    naming_template: str | None = None,
) -> tuple[BinaryIO, list[str], list[str]]:
    """Build a ZIP of novel files matching the given criteria.

    Args:
        novels: List of novel dicts, each containing at least ``id``, ``path``,
                ``title``, ``author_name``, ``series_id``, ``series_name``,
                ``series_index``, and ``has_epub``.
        format_mode: ``'txt'`` or ``'prefer_epub'`` (prefers EPUB when available).
        naming_template: Token-based naming template for ZIP arcnames.
                Defaults to ``{author_name}/{series_name}/#{series_index}_{title}_{id}``.

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
            for novel in novels:
                novel_id = novel.get("id")
                novel_path_str = novel.get("path")
                title = novel.get("title", str(novel_id))

                actual_fmt = (
                    "epub"
                    if (format_mode == "prefer_epub"
                        and novel.get("has_epub") == EpubStatus.DONE)
                    else "txt"
                )

                if not novel_path_str:
                    missing_ids.append(str(novel_id))
                    continue

                file_path = Path(novel_path_str).with_suffix("." + actual_fmt)
                if not file_path.is_file():
                    missing_ids.append(str(novel_id))
                    continue

                arcname = template.resolve(novel) + "." + actual_fmt
                zf.write(str(file_path), arcname)
                added_titles.append(title)
    except Exception:
        # Never leak the spooled file on partial builds.
        zip_buffer.close()
        raise

    return zip_buffer, added_titles, missing_ids
