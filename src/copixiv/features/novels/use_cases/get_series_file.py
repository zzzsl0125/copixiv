"""Use case: on-demand merged download of a folded series book."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from copixiv.core import naming as formats
from copixiv.core.exceptions import NotFoundError
from copixiv.core.models import SOURCE_PIXIV, EpubStatus
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.storage.epub.series_builder import (
    SeriesBookSpec,
    SeriesChapterSource,
    write_series_epub,
    write_series_txt,
)


def build_series_download(
    series_id: int,
    fmt: str = "txt",
    *,
    novel_repo: SQLAlchemyNovelRepository,
    download_root: str = "download",
) -> tuple[Path, str, str]:
    """Synthesize a merged ``.txt`` or ``.epub`` temp file for *series_id*.

    Returns ``(temp_file_path, suggested_filename, media_type)``. Callers must
    clean up ``temp_file_path`` after streaming the response.
    """
    series_meta, chapters = novel_repo.get_series_book_bundle(series_id)
    if not chapters:
        raise NotFoundError(f"Series {series_id} not found")

    first = chapters[0]
    book_title = (
        ((series_meta or {}).get("series_name") or "").strip()
        or (first.series_name or "").strip()
        or first.title
        or f"系列 #{series_id}"
    )
    author_name = ((series_meta or {}).get("author_name") or "").strip() or (first.author_name or None)
    source = (series_meta or {}).get("source") or str(first.source or SOURCE_PIXIV)
    series_key = (series_meta or {}).get("source_key") or str(series_id)

    chapter_sources = [
        SeriesChapterSource(
            novel_id=ch.id,
            title=ch.title,
            series_index=ch.series_index,
            text_path=Path(ch.path),
            has_epub=int(ch.has_epub),
        )
        for ch in chapters
        if ch.path
    ]
    if not chapter_sources:
        raise NotFoundError(f"系列 #{series_id} 没有可用的本地正文文件")

    if fmt == formats.FormatMode.PREFER_EPUB:
        actual_fmt = formats.EPUB if any(ch.has_epub == EpubStatus.DONE for ch in chapters) else formats.TEXT
    else:
        actual_fmt = fmt

    spec = SeriesBookSpec(
        series_id=series_id,
        title=book_title,
        author_name=author_name,
        source=source,
        chapters=chapter_sources,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"copixiv-series-{series_id}-",
        suffix=formats.suffix_for(actual_fmt),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    root_path = Path(download_root).resolve()

    try:
        if actual_fmt == formats.EPUB:
            write_series_epub(spec, tmp_path, download_root=root_path)
        else:
            write_series_txt(spec, tmp_path, download_root=root_path)
    except FileNotFoundError as exc:
        tmp_path.unlink(missing_ok=True)
        raise NotFoundError(str(exc)) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    filename = f"{formats.safe_filename(book_title)}_{formats.safe_filename(series_key)}{formats.suffix_for(actual_fmt)}"
    return tmp_path, filename, formats.media_type_for(actual_fmt)
