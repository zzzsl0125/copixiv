"""本地书籍入库管线 — 「我的上传」的写路径（docs/LIBRARY_DESIGN.md §4）。"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import event

from copixiv.core import naming as formats
from copixiv.core.models import EpubStatus
from copixiv.core.text import sha256_text
from copixiv.db.base import ensure_parent_rows
from copixiv.features.authors.repo import SQLAlchemyAuthorRepository
from copixiv.features.library.filenames import clean_filename, parse_filename
from copixiv.features.library.repo import LocalLibraryRepository
from copixiv.log import logger
from copixiv.storage import atomic
from copixiv.storage.encoding import DECODE_ERROR, TextDecodeError, decode_text_bytes
from copixiv.storage.epub import reader
from copixiv.storage.file_storage import FileStorage

__all__ = [
    "EMPTY_ERROR",
    "IngestResult",
    "ParsedUpload",
    "STATUS_CREATED",
    "STATUS_DUPLICATE",
    "STATUS_FAILED",
    "UploadStatus",
    "UploadUnreadable",
    "body_fingerprint",
    "ingest_uploads",
    "normalize_text",
    "parse_upload",
    "resolve_metadata",
]

EMPTY_ERROR = "文件内容为空"

UploadStatus = Literal["created", "duplicate", "failed"]
STATUS_CREATED: UploadStatus = "created"
STATUS_DUPLICATE: UploadStatus = "duplicate"
STATUS_FAILED: UploadStatus = "failed"


class UploadUnreadable(ValueError):
    """本文件读不出可用正文（TXT 解码失败 / EPUB 读失败）—— 单条 ``failed``。"""


@dataclass(frozen=True)
class ParsedUpload:
    """一个上传文件解析后的形状 —— 管线其余部分只认它。"""

    body: str
    title: str | None = None
    author_name: str | None = None
    original: bytes | None = None


def parse_upload(name: str, data: bytes) -> ParsedUpload:
    """把 ``(文件名, 字节)`` 读成 :class:`ParsedUpload`；失败抛 :class:`UploadUnreadable`."""
    if reader.looks_like_epub(name, data):
        try:
            content = reader.read_epub(data)
        except reader.EpubReadError as exc:
            raise UploadUnreadable(str(exc)) from exc
        return ParsedUpload(
            body=content.body,
            title=content.title,
            author_name=content.author_name,
            original=data,
        )

    try:
        return ParsedUpload(body=decode_text_bytes(data, strict=True))
    except TextDecodeError as exc:
        raise UploadUnreadable(DECODE_ERROR) from exc


def normalize_text(text: str) -> str:
    """规范化：``\\r\\n`` → ``\\n``，去首尾空白（§4-2）。"""
    return text.replace("\r\n", "\n").strip()


def body_fingerprint(normalized: str) -> str:
    """规范化正文的 sha256（§4-3）—— 去重键，也是将来的合并锚。"""
    return sha256_text(normalized)


def resolve_metadata(filename: str, parsed: ParsedUpload) -> tuple[str, str]:
    """``(title, author_name)`` —— EPUB 元数据优先，文件名解析兜底（§4.1）。"""
    title, author_name = parse_filename(filename)
    return (
        (parsed.title or "").strip() or title,
        (parsed.author_name or "").strip() or author_name,
    )


@dataclass(frozen=True)
class IngestResult:
    """逐条上传结果（响应契约见 §4 / ``features/library/schemas.py``）。"""

    filename: str
    status: UploadStatus
    novel_id: int | None = None
    title: str | None = None
    author_name: str | None = None
    text: int | None = None
    error: str | None = None
    epub_attached: bool | None = None


def _attach_epub_to_existing(
    lib_repo: LocalLibraryRepository,
    novel,
    root: Path,
    epub_bytes: bytes,
    created_paths: list[Path],
) -> bool:
    """把上传的 EPUB 原件补挂到已在库的本地书上；返回本次是否真的挂上。"""
    if novel is None or not novel.path:
        return False

    path = formats.sibling_path(Path(novel.path), formats.EPUB)
    if novel.has_epub == EpubStatus.DONE and path.is_file():
        return False

    existed_before = path.is_file()
    try:
        atomic.write_atomic(path, epub_bytes, root=root)
    except OSError as exc:
        logger.warning(
            "EPUB 补挂失败（书已在库，按 duplicate 上报）: #{} {}: {}",
            novel.id,
            path,
            exc,
        )
        return False

    if not existed_before:
        created_paths.append(path)
    lib_repo.mark_epub_done(novel)
    return True


def _read_upload(name: str, data: bytes) -> tuple[str, ParsedUpload]:
    """读一份上传 → ``(规范化正文, 解析结果)``；读不出就抛 :class:`UploadUnreadable`."""
    parsed = parse_upload(name, data)
    body = normalize_text(parsed.body)
    if not body:
        raise UploadUnreadable(EMPTY_ERROR)
    return body, parsed


def _discard(*paths: Path | None) -> None:
    """删掉这次写了一半的文件（EPUB 写失败 / 事务回滚时）。"""
    for written in filter(None, paths):
        with contextlib.suppress(OSError):
            written.unlink(missing_ok=True)


def _track_created_files(session, created_paths: list[Path]) -> None:
    """在写事务回滚时清理本次已落盘的新文件。"""
    session.info["_library_created_paths"] = created_paths
    if "_library_rollback_hook" not in session.info:

        def _on_rollback(s) -> None:
            paths = s.info.pop("_library_created_paths", [])
            _discard(*paths)

        def _on_commit(s) -> None:
            s.info.pop("_library_created_paths", None)

        event.listen(session, "after_rollback", _on_rollback)
        event.listen(session, "after_commit", _on_commit)
        session.info["_library_rollback_hook"] = True


def _write_files(
    storage: FileStorage,
    novel_id: int,
    title: str,
    body: str,
    original: bytes | None,
    root: Path,
) -> tuple[Path, Path | None]:
    """把正文（与 EPUB 原件）落盘 → ``(正文路径, 原件路径或 None)``."""
    path = storage.local_novel_path(novel_id, title)
    epub_path = storage.local_novel_path(novel_id, title, formats.EPUB) if original is not None else None
    try:
        atomic.write_text_atomic(path, body, root=root)
        if epub_path is not None and original is not None:
            atomic.write_atomic(epub_path, original, root=root)
    except OSError:
        _discard(path, epub_path)
        raise
    return path, epub_path


def _handle_duplicate(
    lib_repo: LocalLibraryRepository,
    existing_id: int,
    existing_title: str | None,
    parsed: ParsedUpload,
    name: str,
    *,
    preset_author_id: int | None,
    preset_author_name: str | None,
    root: Path,
    ensure_preset_author: Callable[[], None],
    created_paths: list[Path],
) -> tuple[IngestResult, bool]:
    """已在库的那一条 → ``(结果, 这次是否真的写了预设作者)``."""
    needs_row = preset_author_id is not None or parsed.original is not None
    novel = lib_repo.get_novel(existing_id) if needs_row else None

    wrote_preset = False
    duplicate_author_name: str | None = None
    if preset_author_id is not None:
        ensure_preset_author()
        wrote_preset = lib_repo.apply_preset_author(
            novel,
            preset_author_id,
            preset_author_name,
        )
        duplicate_author_name = preset_author_name

    epub_attached = (
        _attach_epub_to_existing(lib_repo, novel, root, parsed.original, created_paths)
        if parsed.original is not None
        else False
    )
    return (
        IngestResult(
            name,
            STATUS_DUPLICATE,
            novel_id=existing_id,
            title=existing_title,
            author_name=duplicate_author_name,
            epub_attached=epub_attached or None,
        ),
        wrote_preset,
    )


def _create_row(
    lib_repo: LocalLibraryRepository,
    storage: FileStorage,
    name: str,
    body: str,
    parsed: ParsedUpload,
    source_key: str,
    *,
    preset_author_id: int | None,
    preset_author_name: str | None,
    root: Path,
    ensure_preset_author: Callable[[], None],
    created_paths: list[Path],
) -> tuple[IngestResult, bool]:
    """新书：取号 → 落盘 → 插行 → ``(结果, 这次是否真的写了预设作者)``."""
    title, parsed_author_name = resolve_metadata(name, parsed)
    if preset_author_id is None:
        author_id: int | None = None
        author_name: str | None = parsed_author_name
    else:
        author_id, author_name = preset_author_id, preset_author_name

    novel_id = lib_repo.next_local_id()

    try:
        path, epub_path = _write_files(
            storage,
            novel_id,
            title,
            body,
            parsed.original,
            root,
        )
    except OSError as exc:
        return (
            IngestResult(name, STATUS_FAILED, error=f"写入文件失败：{exc}"),
            False,
        )

    created_paths.append(path)
    if epub_path is not None:
        created_paths.append(epub_path)

    if preset_author_id is not None:
        ensure_preset_author()

    lib_repo.insert_local_novel(
        novel_id=novel_id,
        title=title,
        author_id=author_id,
        author_name=author_name,
        path=str(path),
        text_length=len(body),
        has_epub=(EpubStatus.DONE if epub_path is not None else EpubStatus.NO_IMAGES),
        source_key=source_key,
    )

    return (
        IngestResult(
            name,
            STATUS_CREATED,
            novel_id=novel_id,
            title=title,
            author_name=author_name,
            text=len(body),
        ),
        preset_author_id is not None,
    )


def ingest_uploads(
    session,
    storage: FileStorage,
    files: list[tuple[str, bytes]],
    *,
    preset_author_id: int | None = None,
    preset_author_name: str | None = None,
) -> list[IngestResult]:
    """把一批 ``(filename, raw_bytes)`` 收进库，逐条返回结果。"""
    if preset_author_id is None:
        preset_author_name = None

    lib_repo = LocalLibraryRepository(session)
    root = Path(storage.download_dir)
    results: list[IngestResult] = []
    created_paths: list[Path] = []
    prepared = False
    preset_ensured = False
    preset_written = False

    def _ensure_preset_author() -> None:
        nonlocal preset_ensured
        if not preset_ensured and preset_author_id is not None:
            ensure_parent_rows(session, author_ids={preset_author_id})
            preset_ensured = True

    for filename, data in files:
        name = clean_filename(filename)
        try:
            body, parsed = _read_upload(name, data)
        except UploadUnreadable as exc:
            results.append(IngestResult(name, STATUS_FAILED, error=str(exc)))
            continue

        if not prepared:
            lib_repo.lock_local_ids()
            _track_created_files(session, created_paths)
            prepared = True

        source_key = body_fingerprint(body)
        existing = lib_repo.find_local_duplicate(source_key)
        if existing is not None:
            result, wrote_preset = _handle_duplicate(
                lib_repo,
                existing[0],
                existing[1],
                parsed,
                name,
                preset_author_id=preset_author_id,
                preset_author_name=preset_author_name,
                root=root,
                ensure_preset_author=_ensure_preset_author,
                created_paths=created_paths,
            )
        else:
            result, wrote_preset = _create_row(
                lib_repo,
                storage,
                name,
                body,
                parsed,
                source_key,
                preset_author_id=preset_author_id,
                preset_author_name=preset_author_name,
                root=root,
                ensure_preset_author=_ensure_preset_author,
                created_paths=created_paths,
            )
        preset_written = preset_written or wrote_preset
        results.append(result)

    if preset_written and preset_author_id is not None and preset_author_name:
        SQLAlchemyAuthorRepository(session).update_author_name(
            preset_author_id,
            preset_author_name,
        )

    return results
