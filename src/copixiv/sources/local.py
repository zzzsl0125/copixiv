"""LocalSourcePlugin and pure file/filename parsing utilities for local uploads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from copixiv.core.models import SOURCE_LOCAL, UNKNOWN_AUTHOR
from copixiv.core.sources import (
    EpubPolicy,
    SettingFieldSpec,
    SourceCapability,
    SourceManifest,
    SourceShelfDef,
)
from copixiv.core.text import sha256_text
from copixiv.sources.base import BaseSourcePlugin
from copixiv.storage.encoding import DECODE_ERROR, TextDecodeError, decode_text_bytes
from copixiv.storage.epub import reader

UNNAMED_TITLE = "未命名"
EMPTY_ERROR = "文件内容为空"

# ``【作者】标题`` / ``[作者] 标题`` —— 方括号（中英文）打头即作者。
_BRACKET_RE = re.compile(r"^\s*[【\[]\s*(?P<author>[^】\]]+?)\s*[】\]]\s*(?P<title>.+?)\s*$")

# ``作者 - 标题`` 的分隔符：半角/全角连字符**两侧要有空白**，破折号（– —）可无空白。
_DASH_SEPARATORS = (" - ", " － ", " – ", " — ", " –", "—", "–")

_STATUS_MARKERS = frozenset(
    {
        "完结",
        "已完结",
        "完结文",
        "全本",
        "全集",
        "完",
        "连载",
        "未完结",
        "txt",
        "精校",
        "校对",
    }
)
_TRAILING_GROUP_RE = re.compile(r"[（(\[【]\s*([^）)\]】]{0,16}?)\s*[）)\]】]\s*$")
_KNOWN_SUFFIXES = (".txt", ".epub")


def clean_filename(filename: str) -> str:
    """Extract the base filename, stripping any client-supplied directory prefix."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "未命名.txt"


def _strip_known_suffix(filename: str) -> str:
    stem = clean_filename(filename)
    lower = stem.lower()
    for suffix in _KNOWN_SUFFIXES:
        if lower.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.strip()


def _strip_status_markers(title: str) -> str:
    """Strip trailing completion/format tags like ``(完结)`` or ``[txt]``."""
    current = title.strip()
    while True:
        match = _TRAILING_GROUP_RE.search(current)
        if not match:
            return current
        if match.group(1).strip().lower() not in _STATUS_MARKERS:
            return current
        stripped = current[: match.start()].strip()
        if not stripped:
            return current
        current = stripped


def _split_dash(stem: str) -> tuple[str, str] | None:
    """Split ``作者 - 标题`` into ``(author, title)`` or return ``None``."""
    for separator in _DASH_SEPARATORS:
        index = stem.find(separator)
        if index <= 0:
            continue
        author = stem[:index].strip()
        title = stem[index + len(separator) :].strip()
        if author and title:
            return author, title
    return None


def _resolve_unknown_author(default_unknown_author: str | None) -> str:
    return (default_unknown_author or "").strip() or UNKNOWN_AUTHOR


def parse_filename(
    filename: str,
    *,
    parse_filename_metadata: bool = True,
    default_unknown_author: str = UNKNOWN_AUTHOR,
) -> tuple[str, str]:
    """Parse ``(title, author_name)`` from an upload filename."""
    stem = _strip_known_suffix(filename)
    fallback_author = _resolve_unknown_author(default_unknown_author)

    if not parse_filename_metadata:
        return (stem or UNNAMED_TITLE), fallback_author

    author = ""
    title = stem
    match = _BRACKET_RE.match(stem)
    if match:
        author, title = match.group("author"), match.group("title")
    else:
        split = _split_dash(stem)
        if split:
            author, title = split

    title = _strip_status_markers(title).strip() or title.strip() or stem or UNNAMED_TITLE
    return title, (author.strip() or fallback_author)


class UploadUnreadable(ValueError):
    """Uploaded file could not be decoded or parsed into non-empty novel text."""


@dataclass(frozen=True)
class ParsedUpload:
    """Normalized decoded payload from an uploaded TXT or EPUB file."""

    body: str
    title: str | None = None
    author_name: str | None = None
    original: bytes | None = None


def parse_upload(name: str, data: bytes) -> ParsedUpload:
    """Decode ``(filename, raw_bytes)`` into a :class:`ParsedUpload`."""
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
    """Normalize line endings to ``\\n`` and strip leading/trailing whitespace."""
    return text.replace("\r\n", "\n").strip()


def body_fingerprint(normalized: str) -> str:
    """Return the SHA-256 hex digest of normalized novel body text."""
    return sha256_text(normalized)


def resolve_metadata(
    filename: str,
    parsed: ParsedUpload,
    *,
    parse_filename_metadata: bool = True,
    default_unknown_author: str = UNKNOWN_AUTHOR,
) -> tuple[str, str]:
    """Resolve ``(title, author_name)``, preferring embedded EPUB metadata over filename."""
    title, author_name = parse_filename(
        filename,
        parse_filename_metadata=parse_filename_metadata,
        default_unknown_author=default_unknown_author,
    )
    return (
        (parsed.title or "").strip() or title,
        (parsed.author_name or "").strip() or author_name,
    )


class LocalSourcePlugin(BaseSourcePlugin):
    """Source plugin representing user-uploaded local TXT/EPUB novels."""

    manifest = SourceManifest(
        source=SOURCE_LOCAL,
        display_name="本地上传",
        badge_label="本地",
        badge_class="bg-amber-100 text-amber-800",
        capabilities=frozenset({SourceCapability.FILE_UPLOAD}),
        editable_fields=(
            "title",
            "author_id",
            "series_id",
            "series_index",
            "tags",
            "create_time",
        ),
        epub_policy=EpubPolicy.CONTAINER_ONLY,
        allow_proxy_config=False,
        shelf=SourceShelfDef(
            section_id="source_local",
            label="我的上传",
            keyword="source:local;",
            min_like=0,
            min_text=0,
            order_by="id",
            order_direction="DESC",
            icon="upload",
            intake_action="file_upload",
            intake_button_text="上传书籍",
        ),
        settings_schema=(
            SettingFieldSpec(
                key="parse_filename_metadata",
                label="解析文件名中的书名与作者",
                field_type="boolean",
                default=True,
                description="关闭后上传的 TXT 将直接使用去后缀文件名作为标题、使用默认未知作者名",
            ),
            SettingFieldSpec(
                key="default_unknown_author",
                label="默认未知作者名",
                field_type="string",
                default=UNKNOWN_AUTHOR,
                description="当无法从文件或文件名解析作者时使用的显示名",
            ),
        ),
        credential_kinds=(),
    )

    def validate_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate local source settings, falling back blank author names to ``UNKNOWN_AUTHOR``."""
        validated = super().validate_config(raw)
        validated["default_unknown_author"] = _resolve_unknown_author(validated.get("default_unknown_author"))
        return validated


__all__ = [
    "EMPTY_ERROR",
    "LocalSourcePlugin",
    "ParsedUpload",
    "UNNAMED_TITLE",
    "UploadUnreadable",
    "body_fingerprint",
    "clean_filename",
    "normalize_text",
    "parse_filename",
    "parse_upload",
    "resolve_metadata",
]
