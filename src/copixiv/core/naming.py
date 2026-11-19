"""File format conventions, path generation, and the batch-download naming template."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class FormatMode(StrEnum):
    """Supported export/download format modes."""

    TXT = "txt"
    PREFER_EPUB = "prefer_epub"


TEXT = "txt"
EPUB = "epub"

SUFFIXES: dict[str, str] = {TEXT: ".txt", EPUB: ".epub"}
MEDIA_TYPES: dict[str, str] = {TEXT: "text/plain", EPUB: "application/epub+zip"}
ALL_SUFFIXES: tuple[str, ...] = (SUFFIXES[TEXT], SUFFIXES[EPUB])

DEFAULT_NAMING_TEMPLATE = "{author_name}/{series_name}/#{series_index}_{title}_{id}"

# ZIP segment UTF-8 byte bounds (NAME_MAX=255; leaf leaves 5 bytes for '.epub').
_ZIP_DIR_SEGMENT_MAX_BYTES = 255
_ZIP_LEAF_SEGMENT_MAX_BYTES = 250


def suffix_for(kind: str) -> str:
    """Return file extension for format *kind* (e.g. ``".txt"``)."""
    return SUFFIXES[kind]


def sibling_path(path: Path, kind: str) -> Path:
    """Switch extension on a novel path between ``.txt`` and ``.epub``."""
    return path.with_suffix(SUFFIXES[kind])


def media_type_for(kind: str) -> str:
    """Return HTTP Content-Type for *kind*."""
    return MEDIA_TYPES[kind]


def preferred_format(format_mode: str, *, epub_ready: bool) -> str:
    """Choose ``epub`` only when *format_mode* is ``prefer_epub`` and EPUB is ready."""
    return EPUB if (format_mode == FormatMode.PREFER_EPUB and epub_ready) else TEXT


_ILLEGAL_CHARS: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes without splitting codepoints."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def safe_filename(text: str, max_length: int = 240) -> str:
    """Clean *text* of illegal filename characters and truncate to *max_length* bytes."""
    clean = _ILLEGAL_CHARS.sub("", text)
    clean = _WHITESPACE_RUN.sub(" ", clean).strip()
    if not clean:
        return "untitled"
    clean = _truncate_utf8(clean, max_length)
    return clean.strip() or "untitled"


def build_path(
    novel_id: int | str,
    title: str,
    download_dir: str = "download",
) -> str:
    """Generate ``{download_dir}/{subdir}/{safe_title}_{novel_id}.txt`` within NAME_MAX."""
    id_int = int(novel_id)
    subdir = "0000" if id_int < 10_000_000 else str(id_int).zfill(8)[:4]
    reserved = len(f"_{id_int}.epub") + 1
    filename = f"{safe_filename(title, max_length=254 - reserved)}_{id_int}.txt"
    return str(Path(download_dir) / subdir / filename)


_FULLWIDTH_MAP: dict[int, int] = {
    0x5C: 0xFF3C,
    0x2F: 0xFF0F,
    0x3A: 0xFF1A,
    0x3F: 0xFF1F,
    0x22: 0xFF02,
    0x3C: 0xFF1C,
    0x3E: 0xFF1E,
    0x2A: 0xFF0A,
    0x7C: 0xFF5C,
    0x7E: 0xFF5E,
}

_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

_SEPARATORS: str = "#_- ,&"

_CONTROL_CHARS: re.Pattern[str] = re.compile(
    "[\x00-\x1f\x7f-\x9f\xad"
    "\u0600-\u0605\u061c\u06dd\u070f\u08e2\u180e"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f"
    "\ufdd0-\ufdef\ufeff\ufff9-\ufffb\ufffe\uffff]"
)
_TOKEN_RE: re.Pattern[str] = re.compile(r"\{([a-z_]+)\}")


def _replace_illegal_chars(value: str) -> str:
    return _CONTROL_CHARS.sub("", value).translate(_FULLWIDTH_MAP)


def _sanitize_path_segment(segment: str) -> str:
    segment = segment.strip().lstrip(".")
    if not segment:
        return "untitled"

    name, _dot, ext = segment.partition(".")
    if name.upper() in _WINDOWS_RESERVED:
        segment = segment.replace(".", "．") if ext else name + "[WinReserved]"

    if segment[-1] == ".":
        segment = segment[:-1] + "．"

    return segment or "untitled"


def _clamp_segment_bytes(
    segment: str,
    *,
    max_bytes: int,
    preserve_suffix: str | None = None,
) -> str:
    """Truncate *segment* so its UTF-8 encoding does not exceed *max_bytes*."""
    if len(segment.encode("utf-8")) <= max_bytes:
        return segment
    if preserve_suffix:
        suffix_bytes = len(preserve_suffix.encode("utf-8"))
        if segment.endswith(preserve_suffix) and suffix_bytes < max_bytes:
            prefix = _truncate_utf8(segment[: -len(preserve_suffix)], max_bytes - suffix_bytes).rstrip()
            return (prefix + preserve_suffix) or preserve_suffix
        if preserve_suffix not in segment and suffix_bytes < max_bytes:
            prefix = _truncate_utf8(segment, max_bytes - suffix_bytes).rstrip()
            return (prefix + preserve_suffix) or preserve_suffix
    clipped = _truncate_utf8(segment, max_bytes).rstrip(".")
    return clipped or "untitled"


def _remove_empty_token(text: str, placeholder: str) -> str:
    if placeholder not in text:
        return text
    while True:
        idx = text.find(placeholder)
        if idx == -1:
            break
        left = idx
        while left > 0 and text[left - 1] in _SEPARATORS:
            left -= 1
        right = idx + len(placeholder)
        while right < len(text) and text[right] in _SEPARATORS:
            right += 1
        text = text[:left] + text[right:]
    return text


def _post_process(path: str, *, novel_id_suffix: str | None = None) -> str:
    cleaned: list[str] = []
    for seg in path.split("/"):
        seg = _replace_illegal_chars(seg.strip())
        if not seg:
            continue
        cleaned.append(_sanitize_path_segment(seg))

    if not cleaned:
        return "untitled" + (novel_id_suffix or "")

    for i in range(len(cleaned) - 1):
        cleaned[i] = _clamp_segment_bytes(cleaned[i], max_bytes=_ZIP_DIR_SEGMENT_MAX_BYTES)
    cleaned[-1] = _clamp_segment_bytes(
        cleaned[-1],
        max_bytes=_ZIP_LEAF_SEGMENT_MAX_BYTES,
        preserve_suffix=novel_id_suffix,
    )
    return "/".join(cleaned)


class NovelNamingTemplate:
    """Resolve a token-based naming template into a bounded, safe ZIP arcname."""

    def __init__(self, template: str) -> None:
        if "{id}" not in template:
            raise ValueError("Template must contain '{id}' to guarantee unique filenames.")
        self._template = template

    def resolve(self, novel: Any) -> str:
        """Produce a sanitized relative path (caller appends the extension)."""
        result = self._template
        values: dict[str, str] = {}
        for key, raw in self._iter_token_values(novel):
            val = _replace_illegal_chars(raw)
            if not val:
                result = _remove_empty_token(result, "{" + key + "}")
            else:
                values[key] = val

        result = _TOKEN_RE.sub(
            lambda m: values[m.group(1)] if m.group(1) in values else m.group(0),
            result,
        )
        return _post_process(result, novel_id_suffix=f"_{novel.id}")

    def _iter_token_values(self, novel: Any):
        yield "id", str(novel.id)

        if "{date}" in self._template:
            yield "date", _format_date(novel.create_time)

        for key in (
            "title",
            "author_name",
            "author_id",
            "like",
            "view",
            "text",
            "series_name",
            "series_index",
        ):
            placeholder = "{" + key + "}"
            if placeholder not in self._template:
                continue
            value = getattr(novel, key, None)
            if key == "author_name" and not value:
                yield key, "未知作者"
            elif key == "series_index" and value == 0 or value is None:
                yield key, ""
            else:
                yield key, str(value)


def _format_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


__all__ = [
    "ALL_SUFFIXES",
    "DEFAULT_NAMING_TEMPLATE",
    "EPUB",
    "FormatMode",
    "MEDIA_TYPES",
    "NovelNamingTemplate",
    "SUFFIXES",
    "TEXT",
    "build_path",
    "media_type_for",
    "preferred_format",
    "safe_filename",
    "sibling_path",
    "suffix_for",
]
