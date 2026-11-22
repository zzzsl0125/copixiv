"""Batch ZIP archive builder — the single owner of export packaging."""

from __future__ import annotations

import posixpath
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

from copixiv.core import naming as formats
from copixiv.core.models import EpubStatus
from copixiv.core.naming import DEFAULT_NAMING_TEMPLATE, NovelNamingTemplate
from copixiv.log import logger
from copixiv.storage.atomic import atomic_write, resolve_within_root

# Buffers up to 8 MB stay in memory; larger batches spill to a temp file.
_SPOOL_MAX = 8 * 1024 * 1024

# Progress callback fires every N processed files.
_PROGRESS_EVERY = 500


def _sanitize_arcname(arcname: str) -> str:
    """Ensure ZIP archive paths cannot escape via leading ``/`` or ``..``."""
    normalized = posixpath.normpath(arcname.replace("\\", "/").lstrip("/"))
    parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "untitled"


def _is_within_root(file_path: Path, resolved_root: Path | None) -> bool:
    if resolved_root is None:
        return True
    return resolve_within_root(file_path, resolved_root) is not None


def _write_zip_entries(
    zf: zipfile.ZipFile,
    novels: Sequence[Any],
    format_mode: str,
    template: NovelNamingTemplate,
    progress: Callable[[int, int], None] | None,
    resolved_root: Path | None,
) -> tuple[int, list[str]]:
    """Write matching novel files into an open :class:`zipfile.ZipFile`."""
    added_count = 0
    missing_ids: list[str] = []

    for index, novel in enumerate(novels, start=1):
        novel_id = novel.id
        novel_path_str = novel.path

        actual_fmt = formats.preferred_format(
            format_mode,
            epub_ready=novel.has_epub == EpubStatus.DONE,
        )

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
                arcname = _sanitize_arcname(template.resolve(novel) + formats.suffix_for(actual_fmt))
                compress_type = zipfile.ZIP_STORED if actual_fmt == formats.EPUB else zipfile.ZIP_DEFLATED
                zf.write(str(file_path), arcname, compress_type=compress_type)
                added_count += 1

        if progress is not None and index % _PROGRESS_EVERY == 0:
            progress(index, len(novels))

    return added_count, missing_ids


def build_batch_zip(
    novels: Sequence[Any],
    format_mode: str = "txt",
    naming_template: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    *,
    download_root: str | Path | None = None,
) -> tuple[BinaryIO, int, list[str]]:
    """Build a ZIP of novel files matching the given criteria.

    Returns ``(zip_buffer, added_count, missing_ids)``.
    EPUB files (already ZIP-compressed) are stored with ``ZIP_STORED`` to
    avoid double-compression CPU cost; TXT files use ``ZIP_DEFLATED``.
    When *download_root* is given, any source file outside *download_root* is
    refused and reported in *missing_ids*.
    """
    # Returned open to the caller (StreamingResponse closes it on completion).
    zip_buffer = cast(
        BinaryIO,
        tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX),  # noqa: SIM115
    )

    template = NovelNamingTemplate(naming_template or DEFAULT_NAMING_TEMPLATE)
    resolved_root = Path(download_root).resolve() if download_root is not None else None

    try:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            added_count, missing_ids = _write_zip_entries(
                zf,
                novels,
                format_mode,
                template,
                progress,
                resolved_root,
            )
    except Exception:
        zip_buffer.close()
        raise

    return zip_buffer, added_count, missing_ids


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
