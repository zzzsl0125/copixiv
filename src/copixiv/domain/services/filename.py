"""Safe filename and path generation — pure functions."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

# Characters illegal in Windows / Linux filenames
_ILLEGAL_CHARS: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")


def safe_filename(text: str, max_length: int = 240) -> str:
    """Clean *text* of illegal filename characters and truncate to *max_length* bytes.

    Returns ``"untitled"`` if the result would be empty.
    """
    clean = _ILLEGAL_CHARS.sub("", text)
    clean = _WHITESPACE_RUN.sub(" ", clean).strip()
    if not clean:
        return "untitled"

    encoded = clean.encode("utf-8")
    if len(encoded) > max_length:
        encoded = encoded[:max_length]
        # Don't split a multi-byte character
        while True:
            try:
                clean = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return clean.strip()


def build_path(
    novel_id: int | str,
    title: str,
    download_dir: str = "download",
) -> str:
    """Generate the filesystem path where a novel's text should be stored.

    Format: ``{download_dir}/{subdir}/{safe_title}_{novel_id}.txt``
    """
    id_int = int(novel_id)
    subdir = "0000" if id_int < 10_000_000 else str(id_int).zfill(8)[:4]
    filename = f"{safe_filename(title)}_{id_int}.txt"
    return str(Path(download_dir) / subdir / filename)


# ---------------------------------------------------------------------------
# Batch-download naming template engine
# ---------------------------------------------------------------------------

# Half-width illegal chars → full-width equivalents (preserve readability)
_FULLWIDTH_MAP: dict[int, int] = {
    0x5C: 0xFF3C,   # \ → ＼
    0x2F: 0xFF0F,   # / → ／
    0x3A: 0xFF1A,   # : → ：
    0x3F: 0xFF1F,   # ? → ？
    0x22: 0xFF02,   # " → ＂
    0x3C: 0xFF1C,   # < → ＜
    0x3E: 0xFF1E,   # > → ＞
    0x2A: 0xFF0A,   # * → ＊
    0x7C: 0xFF5C,   # | → ｜
    0x7E: 0xFF5E,   # ~ → ～
}

# Windows file-system reserved names (case-insensitive)
_WINDOWS_RESERVED: frozenset[str] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

_SEPARATORS: str = "#_- ,&"

# Invisible / control characters — stripped entirely
_CONTROL_CHARS: re.Pattern[str] = re.compile(
    "[\x00-\x1f\x7f-\x9f\xad"
    "؀-؅؜۝܏࣢᠎"
    "​-‏‪-‮⁠-⁤⁦-⁯"
    "﷐-﷯﻿￹-￻￾￿]"
)


def _replace_illegal_chars(value: str) -> str:
    """Remove invisible chars and map half-width filename chars to full-width."""
    return _CONTROL_CHARS.sub("", value).translate(_FULLWIDTH_MAP)


def _sanitize_path_segment(segment: str) -> str:
    """Sanitize one ``/``-delimited path segment.

    Handles Windows reserved names and leading/trailing dots.
    Returns ``"untitled"`` if empty after processing.
    """
    segment = segment.strip()
    if not segment:
        return "untitled"

    # Strip leading dots BEFORE the reserved-name check — ``.CON`` /
    # ``..CON`` must not slip past the Windows reserved-name guard.
    segment = segment.lstrip(".")
    if not segment:
        return "untitled"

    name, _dot, ext = segment.partition(".")
    if name.upper() in _WINDOWS_RESERVED:
        if ext:
            segment = segment.replace(".", "．")  # full-width ．
        else:
            segment = name + "[WinReserved]"

    if segment[-1] == ".":
        segment = segment[:-1] + "．"

    return segment or "untitled"


def _remove_empty_token(text: str, placeholder: str) -> str:
    """Replace *placeholder* and any adjacent separator chars with ``""``.

    All occurrences are removed (a template may repeat a token), including
    ``#{series_index}_`` wrapping an empty ``{series_index}``; ``/`` (not a
    separator) is left in place.
    """
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


def _post_process(path: str) -> str:
    """Final cleanup: sanitize each ``/`` segment, discard empties, rejoin.

    Literal characters in the template (e.g. a user-written ``:``) are
    sanitized the same way token values are, so the produced path is legal
    on Windows as well as Linux.
    """
    segments = path.split("/")
    cleaned: list[str] = []
    for seg in segments:
        seg = _replace_illegal_chars(seg.strip())
        if not seg:
            continue
        cleaned.append(_sanitize_path_segment(seg))
    return "/".join(cleaned)


class NovelNamingTemplate:
    """Resolve a token-based naming template into a ZIP arcname.

    Token names match the keys of the novel dict directly::

        {id}            — novel["id"] (REQUIRED — guarantees unique filenames)
        {title}         — novel["title"]
        {author_name}   — novel["author_name"] (falls back to "未知作者")
        {author_id}     — novel["author_id"]
        {like}          — novel["like"]
        {view}          — novel["view"]
        {text}          — novel["text"] (character count)
        {date}          — novel["create_time"] formatted as YYYY-MM-DD
        {series_name}   — novel["series_name"] (empty if no series)
        {series_index}  — novel["series_index"] (raw number, no ``#`` prefix)

    ``/`` in the template defines the directory structure inside the ZIP.

    Raises:
        ValueError: If the template does not contain ``{id}``.
    """

    def __init__(self, template: str) -> None:
        if "{id}" not in template:
            raise ValueError(
                "Template must contain '{id}' to guarantee unique filenames."
            )
        self._template = template

    # -- resolve ---------------------------------------------------------

    def resolve(self, novel: dict[str, Any]) -> str:
        """Produce a sanitized relative path (caller appends the extension)."""
        result = self._template

        # Scan the novel dict for matching tokens; {date} is the sole
        # computed token (formatted from create_time).
        for key, raw in self._iter_token_values(novel):
            placeholder = "{" + key + "}"
            val = _replace_illegal_chars(raw)

            if val:
                result = result.replace(placeholder, val)
            else:
                result = _remove_empty_token(result, placeholder)

        return _post_process(result)

    def _iter_token_values(self, novel: dict[str, Any]):
        """Yield ``(key, raw_string)`` for each token found in the template."""
        # {id} — mandatory
        yield "id", str(novel["id"])

        # {date} — computed, formatted create_time
        if "{date}" in self._template:
            yield "date", _format_date(novel.get("create_time"))

        # All other tokens are novel-dict keys directly
        for key in ("title", "author_name", "author_id", "like", "view",
                     "text", "series_name", "series_index"):
            placeholder = "{" + key + "}"
            if placeholder not in self._template:
                continue
            value = novel.get(key)
            if key == "author_name" and not value:
                yield key, "未知作者"
            elif value is None:
                yield key, ""
            else:
                yield key, str(value)


def _format_date(value: Any) -> str:
    """Format *value* as ``YYYY-MM-DD``, or return ``""`` if unavailable."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]
