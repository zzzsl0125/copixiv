"""Batch ZIP archive builder — pure function."""

import io
import zipfile
from pathlib import Path
from typing import Any

from .filename import safe_filename


def build_batch_zip(
    novels: list[dict[str, Any]],
    format_mode: str = "txt",
) -> tuple[io.BytesIO, list[str], list[str]]:
    """Build an in-memory ZIP of novel files matching the given criteria.

    Args:
        novels: List of novel dicts, each containing at least ``id``, ``path``,
                ``title``, ``author_name``, ``series_id``, ``series_name``,
                ``series_index``, and ``has_epub``.
        format_mode: ``'txt'`` or ``'prefer_epub'`` (prefers EPUB when available).

    Returns:
        ``(zip_buffer, added_titles, missing_ids)`` — the ZIP as a ``BytesIO``,
        the titles successfully added, and the IDs whose files were missing.
    """
    zip_buffer = io.BytesIO()
    added_titles: list[str] = []
    missing_ids: list[str] = []

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for novel in novels:
            novel_id = novel.get("id")
            novel_path_str = novel.get("path")
            title = novel.get("title", str(novel_id))
            author_name = novel.get("author_name") or "未知作者"
            series_id = novel.get("series_id")
            series_name = novel.get("series_name")
            series_index = novel.get("series_index")

            actual_fmt = (
                "epub"
                if (format_mode == "prefer_epub" and novel.get("has_epub") == 2)
                else "txt"
            )

            if not novel_path_str:
                missing_ids.append(str(novel_id))
                continue

            file_path = Path(novel_path_str).with_suffix("." + actual_fmt)
            if not file_path.is_file():
                missing_ids.append(str(novel_id))
                continue

            safe_name = safe_filename(title)
            safe_author = safe_filename(author_name)

            if series_id and series_name:
                safe_series = safe_filename(series_name)
                prefix = (
                    f"{int(series_index):02d}_"
                    if series_index is not None
                    else ""
                )
                arcname = (
                    f"{safe_author}/{safe_series}/"
                    f"{prefix}{safe_name}_{novel_id}.{actual_fmt}"
                )
            else:
                arcname = f"{safe_author}/{safe_name}_{novel_id}.{actual_fmt}"

            zf.write(str(file_path), arcname)
            added_titles.append(title)

    return zip_buffer, added_titles, missing_ids
