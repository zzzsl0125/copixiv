"""Batch ZIP archive builder — the single owner of export packaging."""

from __future__ import annotations

import posixpath
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from copixiv.core import naming as formats
from copixiv.core.models import EpubStatus
from copixiv.core.naming import DEFAULT_NAMING_TEMPLATE, NovelNamingTemplate
from copixiv.log import logger
from copixiv.storage.atomic import atomic_write, resolve_within_root
from copixiv.storage.epub.series_builder import SeriesBookSpec, write_series_epub, write_series_txt

# Progress callback fires every N processed files.
_PROGRESS_EVERY = 500


@dataclass(slots=True)
class SeriesExportEntry:
    """Folded series export descriptor consumed by :func:`build_batch_zip_to_file`."""

    spec: SeriesBookSpec
    id: int
    title: str
    author_id: int | None = None
    author_name: str | None = None
    like: int = 0
    view: int = 0
    text: int = 0
    create_time: datetime | None = None
    has_epub: EpubStatus = EpubStatus.NO
    source: str = "pixiv"
    # Intentionally cleared for NovelNamingTemplate so default templates yield
    # ``{author_name}/{title}_{id}.ext`` rather than nested ``{series}/{title}``.
    series_name: str | None = field(default=None, init=False)
    series_index: int | None = field(default=None, init=False)


def _sanitize_arcname(arcname: str) -> str:
    """Ensure ZIP archive paths cannot escape via leading ``/`` or ``..``."""
    normalized = posixpath.normpath(arcname.replace("\\", "/").lstrip("/"))
    parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "untitled"


def _is_within_root(file_path: Path, resolved_root: Path | None) -> bool:
    if resolved_root is None:
        return True
    return resolve_within_root(file_path, resolved_root) is not None


def _format_arcname(novel: Any, actual_fmt: str, template: NovelNamingTemplate) -> str:
    return _sanitize_arcname(template.resolve(novel) + formats.suffix_for(actual_fmt))


def zip_entry_path(
    novel: Any,
    format_mode: str = "txt",
    naming_template: str | NovelNamingTemplate | None = None,
) -> str:
    """Compute the sanitized ZIP entry path (arcname) for *novel*.

    Raises:
        ValueError: If *naming_template* does not contain ``{id}``.
    """
    template = (
        naming_template
        if isinstance(naming_template, NovelNamingTemplate)
        else NovelNamingTemplate(naming_template or DEFAULT_NAMING_TEMPLATE)
    )
    actual_fmt = formats.preferred_format(
        format_mode,
        epub_ready=novel.has_epub == EpubStatus.DONE,
    )
    return _format_arcname(novel, actual_fmt, template)


def _write_zip_entries(
    zf: zipfile.ZipFile,
    novels: Sequence[Any],
    format_mode: str,
    template: NovelNamingTemplate,
    progress: Callable[[int, int], None] | None,
    resolved_root: Path | None,
) -> tuple[int, list[str]]:
    """Write matching novel files and synthesized series files into an open :class:`zipfile.ZipFile`."""
    added_count = 0
    missing_ids: list[str] = []

    with tempfile.TemporaryDirectory(prefix="copixiv-zip-series-") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        for index, item in enumerate(novels, start=1):
            actual_fmt = formats.preferred_format(
                format_mode,
                epub_ready=item.has_epub == EpubStatus.DONE,
            )

            if isinstance(item, SeriesExportEntry):
                series_out = tmp_dir_path / f"series_{index}_{item.id}{formats.suffix_for(actual_fmt)}"
                try:
                    if actual_fmt == formats.EPUB:
                        write_series_epub(item.spec, series_out, download_root=resolved_root)
                    else:
                        write_series_txt(item.spec, series_out, download_root=resolved_root)
                except FileNotFoundError:
                    missing_ids.append(f"series:{item.id}")
                else:
                    arcname = _format_arcname(item, actual_fmt, template)
                    compress_type = zipfile.ZIP_STORED if actual_fmt == formats.EPUB else zipfile.ZIP_DEFLATED
                    zf.write(str(series_out), arcname, compress_type=compress_type)
                    series_out.unlink(missing_ok=True)
                    added_count += 1
            else:
                novel_id = item.id
                novel_path_str = item.path

                if not novel_path_str:
                    missing_ids.append(str(novel_id))
                else:
                    file_path = formats.sibling_path(Path(novel_path_str), actual_fmt)
                    if not _is_within_root(file_path, resolved_root):
                        logger.warning(
                            "拒绝打包 download 根之外的文件: novel={} path={}",
                            novel_id,
                            file_path,
                        )
                        missing_ids.append(str(novel_id))
                    elif not file_path.is_file():
                        missing_ids.append(str(novel_id))
                    else:
                        arcname = _format_arcname(item, actual_fmt, template)
                        compress_type = (
                            zipfile.ZIP_STORED if actual_fmt == formats.EPUB else zipfile.ZIP_DEFLATED
                        )
                        zf.write(str(file_path), arcname, compress_type=compress_type)
                        added_count += 1

            if progress is not None and index % _PROGRESS_EVERY == 0:
                progress(index, len(novels))

    return added_count, missing_ids


class _EmptyZipError(Exception):
    """Internal sentinel to abort :func:`atomic_write` when zero files were added."""


def build_batch_zip_to_file(
    novels: Sequence[Any],
    target_path: str | Path,
    format_mode: str = "txt",
    naming_template: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    *,
    download_root: str | Path | None = None,
) -> tuple[int, int, list[str]]:
    """Build a batch ZIP directly to *target_path* via :func:`atomic_write`.

    Avoids spooling to ``/tmp`` and copying the full archive a second time.
    If zero files are added, no output file is created and ``size_bytes`` is ``0``.
    Returns ``(size_bytes, added_count, missing_ids)``.
    """
    out_path = Path(target_path)
    template = NovelNamingTemplate(naming_template or DEFAULT_NAMING_TEMPLATE)
    resolved_root = Path(download_root).resolve() if download_root is not None else None
    missing_ids: list[str] = []

    try:
        with atomic_write(out_path) as tmp_path:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                added_count, missing_ids = _write_zip_entries(
                    zf,
                    novels,
                    format_mode,
                    template,
                    progress,
                    resolved_root,
                )
            if added_count == 0:
                raise _EmptyZipError
    except _EmptyZipError:
        return 0, 0, missing_ids

    return out_path.stat().st_size, added_count, missing_ids
