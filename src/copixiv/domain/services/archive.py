"""Batch ZIP archive builder — pure function."""

import io
import zipfile
from pathlib import Path
from typing import Any

from .filename import NovelNamingTemplate


def build_batch_zip(
    novels: list[dict[str, Any]],
    format_mode: str = "txt",
    naming_template: str | None = None,
) -> tuple[io.BytesIO, list[str], list[str]]:
    """Build an in-memory ZIP of novel files matching the given criteria.

    Args:
        novels: List of novel dicts, each containing at least ``id``, ``path``,
                ``title``, ``author_name``, ``series_id``, ``series_name``,
                ``series_index``, and ``has_epub``.
        format_mode: ``'txt'`` or ``'prefer_epub'`` (prefers EPUB when available).
        naming_template: Token-based naming template for ZIP arcnames.
                Defaults to ``{user}/{series_title}/#{series_order}_{title}_{novel_id}``.

    Returns:
        ``(zip_buffer, added_titles, missing_ids)`` — the ZIP as a ``BytesIO``,
        the titles successfully added, and the IDs whose files were missing.
    """
    zip_buffer = io.BytesIO()
    added_titles: list[str] = []
    missing_ids: list[str] = []

    template = NovelNamingTemplate(
        naming_template or "{author_name}/{series_name}/#{series_index}_{title}_{id}"
    )

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for novel in novels:
            novel_id = novel.get("id")
            novel_path_str = novel.get("path")
            title = novel.get("title", str(novel_id))

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

            arcname = template.resolve(novel) + "." + actual_fmt
            zf.write(str(file_path), arcname)
            added_titles.append(title)

    return zip_buffer, added_titles, missing_ids
