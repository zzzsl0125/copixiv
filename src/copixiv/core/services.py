"""Pure domain services — no I/O, no side effects.

Merged from ``domain/services/*`` (8 files + ``__init__.py`` re-exports).
"""

from __future__ import annotations

import logging
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from pydantic import BaseModel, Field

from copixiv.core.models import EpubStatus


# ---------------------------------------------------------------------------
# Tag parsing utilities — pure functions.
# ---------------------------------------------------------------------------

# Pattern to clean parenthesised sub-tags
_CLEAN_PATTERN: re.Pattern[str] = re.compile(r"[()]")
# Pattern to split combined tags on delimiters
_SPLIT_PATTERN: re.Pattern[str] = re.compile(r"[/|#、\\]")


def normalize_tag(tag: str) -> str:
    """Normalise a single tag: lowercase, strip delimiters."""
    return tag.strip().lower()


def parse_tags(tags: list[str | dict]) -> list[str]:
    """Parse Pixiv API tag data into a deduplicated, cleaned list.

    Handles both raw strings and Pixiv dict tags (``{"name": "..."}``).
    Splits combined tags on ``/|#、\\`` and removes parenthesised clutter.
    """
    # dict keys deduplicate like a set but preserve first-seen order,
    # so the output is deterministic (set iteration order is not).
    result: dict[str, None] = {}
    for tag in tags:
        if tag is None:
            continue  # junk entry — never str(None) → "none"
        tag_text: str = (
            tag.get("name", "") if isinstance(tag, dict) else str(tag)
        )
        if not tag_text:
            continue
        cleaned = _CLEAN_PATTERN.sub("", tag_text)
        for part in _SPLIT_PATTERN.split(cleaned):
            part = normalize_tag(part)
            if part:
                result.setdefault(part)
    return list(result)


# ---------------------------------------------------------------------------
# Language detection — pure functions (with optional langid I/O).
# ---------------------------------------------------------------------------

# stdlib logging keeps this core module free of app-layer imports; the
# app's loguru bridge forwards these records.
logger = logging.getLogger(__name__)

# Regex matching any Japanese kana character
_JAPANESE_REGEX: re.Pattern[str] = re.compile(r"[぀-ゟ゠-ヿ]")

# Tags that explicitly mark a work as Chinese
_CHINESE_TAG_KEYWORDS: frozenset[str] = frozenset({
    "中文", "中文作品", "中文注意", "简中", "简体中文", "繁体", "繁體", "中文中国语",
    "中国文", "中文語", "中文语", "中国语", "中国語", "中阈语", "中國语", "中國語",
    "中国語注意", "中國語注意", "中国语注意", "Chinese", "chinese", "中國", "中国",
})

# Pattern for embedded images in novel text
_HAS_IMAGE_PATTERN: re.Pattern[str] = re.compile(
    r"\[(uploadedimage|pixivimage):([\d\-]+)\]"
)


def is_chinese(
    title: str = "",
    caption: str = "",
    tags: list[str] | None = None,
) -> bool:
    """Determine whether a Pixiv work is Chinese-language.

    Checks tags first (fast, no I/O), then falls back to langid on title+caption.
    """
    if tags and any(t in _CHINESE_TAG_KEYWORDS for t in tags):
        return True

    sample = (title or "") + (caption or "")
    if not sample.strip():
        return False

    if _JAPANESE_REGEX.search(sample):
        return False

    # langid is I/O — imported lazily to keep the pure-function surface clean
    try:
        import langid
        return langid.classify(sample)[0] == "zh"
    except ModuleNotFoundError:
        # langid missing means every non-tagged novel silently reads as
        # "not Chinese" — surface that degradation once instead of hiding it.
        if not is_chinese._langid_warning_emitted:  # type: ignore[attr-defined]
            logger.warning(
                "langid is not installed — language detection degrades to "
                "tag-keyword + kana heuristics only."
            )
            is_chinese._langid_warning_emitted = True  # type: ignore[attr-defined]
        return False
    except Exception:
        return False


def has_image_placeholders(content: str | None) -> bool:
    """Check whether the novel text contains image placeholders.

    Placeholders look like ``[uploadedimage:12345]`` or ``[pixivimage:67890]``.
    ``None`` / empty content → ``False`` (no images to convert).
    """
    if not content:
        return False
    return bool(re.search(_HAS_IMAGE_PATTERN, content))


# ---------------------------------------------------------------------------
# Safe filename and path generation — pure functions.
# ---------------------------------------------------------------------------

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

    The title is truncated so the *complete* basename always fits within
    ``NAME_MAX`` (255 bytes) — including the ``_{novel_id}.txt`` suffix
    and the ``.tmp`` suffix that atomic writes (``save_novel_text`` /
    the EPUB builder) append transiently.  One extra byte of headroom is
    reserved so no component ever sits exactly at the limit.  Without
    that reservation a long title can still overflow the limit on the
    ``.tmp`` write and fail the whole download with
    ``OSError: [Errno 36] File name too long``.
    """
    id_int = int(novel_id)
    subdir = "0000" if id_int < 10_000_000 else str(id_int).zfill(8)[:4]
    # Worst-case basename suffix is "_<id>.epub.tmp" (one byte longer
    # than "_<id>.txt.tmp"); reserve it so both text and EPUB temp
    # writes stay within NAME_MAX, with 1 byte of margin.
    reserved = len(f"_{id_int}.epub.tmp") + 1
    filename = (
        f"{safe_filename(title, max_length=254 - reserved)}_{id_int}.txt"
    )
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

    def _iter_token_values(self, novel):
        """Yield ``(key, raw_string)`` for each token found in the template.

        *novel* is a domain :class:`~copixiv.core.models.Novel`
        (or any object exposing the token attributes).
        """
        # {id} — mandatory
        yield "id", str(novel.id)

        # {date} — computed, formatted create_time
        if "{date}" in self._template:
            yield "date", _format_date(novel.create_time)

        # All other tokens are novel attributes directly
        for key in ("title", "author_name", "author_id", "like", "view",
                     "text", "series_name", "series_index"):
            placeholder = "{" + key + "}"
            if placeholder not in self._template:
                continue
            value = getattr(novel, key, None)
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


# ---------------------------------------------------------------------------
# General parsing utilities — pure functions.
# ---------------------------------------------------------------------------


def safe_get(data: Any, path: str, default: Any = None) -> Any:
    """Read a (possibly nested) field from a Pydantic model or dict.

    Supports dotted paths: ``safe_get(novel, "user.name")`` is equivalent to
    ``safe_get(safe_get(novel, "user"), "name")``.

    Returns *default* if *data* is ``None``, any intermediate key is missing,
    or the final key is missing.  A terminal ``None`` value is NOT treated as
    missing — if the final field exists and is ``None``, ``None`` is returned.
    """
    if data is None:
        return default
    keys = path.split(".")
    for i, key in enumerate(keys):
        if data is None:
            return default
        is_last = i == len(keys) - 1
        if isinstance(data, dict):
            if is_last:
                return data.get(key, default)
            data = data.get(key)
        else:
            if is_last:
                return getattr(data, key, default)
            data = getattr(data, key, None)
    return default


def safe_set(data: Any, path: str, value: Any) -> None:
    """Set a (possibly nested) field on a Pydantic model or dict.

    Dotted paths traverse through intermediate keys, creating missing dicts
    along the way.  For Pydantic model attributes, intermediate attributes
    must already exist.
    """
    if data is None:
        raise ValueError("Cannot set on None")
    keys = path.split(".")
    for key in keys[:-1]:
        if isinstance(data, dict):
            if key not in data:
                data[key] = {}
            data = data[key]
        else:
            data = getattr(data, key)
    last = keys[-1]
    if isinstance(data, dict):
        data[last] = value
    else:
        setattr(data, last, value)


def guess_series_order(navigation: Any) -> int | None:
    """Infer a novel's position in its series from Pixiv navigation data.

    Looks at ``prevNovel.contentOrder`` first (current = prev + 1), and
    falls back to ``nextNovel.contentOrder`` (current = next - 1) when the
    previous pointer exists but carries no order.
    """
    try:
        prev = safe_get(navigation, "prevNovel")
        if prev is not None:
            order = safe_get(prev, "contentOrder")
            if order is not None:
                return int(order) + 1
        nxt = safe_get(navigation, "nextNovel")
        if nxt is not None:
            order = safe_get(nxt, "contentOrder")
            if order is not None:
                return int(order) - 1
        return None
    except (TypeError, ValueError):
        return None


# An ordered search condition: (type, value).  The canonical wire format
# is the raw keyword string ``"type:value;type:value"`` — this parser is
# its single, authoritative translation into typed conditions.
SearchConditions = list[tuple[str, str]]

# Bare digit-only tokens of 7+ characters are treated as novel IDs (the
# smallest ID in the dataset is 1,285,180 — shorter numbers can never be
# a valid ID; use the explicit ``id:123`` form to force ID lookup).
_BARE_ID_RE = re.compile(r"^\d{7,}$")


def parse_search_keyword(keyword: str) -> SearchConditions:
    """Parse a search keyword string into an ordered list of conditions.

    Input format: ``"type:value;type:value;"`` (semicolon-delimited,
    Chinese full-width ``；`` accepted).  Each condition becomes a
    ``(type, value)`` pair, e.g. ``"keyword:R-18;author_id:12345"`` →
    ``[("keyword", "R-18"), ("author_id", "12345")]``.

    Semantics (the contract the whole search path relies on):

    - ALL conditions combine with AND — every additional condition
      narrows the result set.  Multi-valued facets (``tags``, ``keyword``)
      each contribute one AND-branch per value, which is satisfiable
      because a novel holds many tags / an FTS row matches many tokens.
    - Single-valued columns (``author_id`` / ``series_id`` / ``id`` /
      ``is_favourite`` / ``is_special_follow``) keep the LAST value when a
      type repeats — under AND semantics two different values for the
      same column are contradictory, and order is preserved so the winner
      is deterministic.
    - Unknown types are kept in the list and rejected downstream with a
      400 (``Invalid query field``) — a typo must be loud, not a silent
      empty result set.
    - The list preserves duplicates and order, unlike the old lossy
      ``{value: type}`` dict (which dropped conditions whose values
      collided).
    """
    conditions: SearchConditions = []
    if not keyword or not keyword.strip():
        return conditions

    for cond in keyword.replace("；", ";").split(";"):
        cond = cond.strip()
        if not cond:
            continue
        colon_idx = cond.find(":")
        if colon_idx > 0:
            qtype = cond[:colon_idx].strip()
            qvalue = cond[colon_idx + 1 :].strip()
            if qvalue:
                conditions.append((qtype, qvalue))
        else:
            qtype = "id" if _BARE_ID_RE.match(cond) else "keyword"
            conditions.append((qtype, cond))
    return conditions


# ---------------------------------------------------------------------------
# QuerySpec — the domain value object for novel list/count/scope queries
# ---------------------------------------------------------------------------

# Replaces the bare ``(conditions, order_by, order_direction, cursor,
# per_page, min_like, min_text, exclude_ids, exclude_blocked_tags)``
# parameter soup that used to travel from the web layer through the
# repository port into the SQL query builder (docs/MODULARITY.md §M3).
#
# The repository layer supplies the SQL-only inputs (blocked tag names,
# restricted ID sets) separately when constructing the builder — they are
# infrastructure concerns, not part of the user-facing query.


class QuerySpec(BaseModel):
    """A complete read-query specification for novels.

    Fields:
        conditions: Parsed search conditions (``parse_search_keyword``).
        order_by / order_direction: List ordering; ``random`` uses the
            precomputed shuffle column.
        cursor: Opaque keyset-pagination cursor (``{"id": ..., <order_by>: ...}``
            or ``{"shuffle": ..., "id": ...}`` for random browsing).
        per_page: Page size (+1 internally to detect more pages).
        min_like / min_text: Display thresholds (0 disables).
        exclude_ids: Novel IDs to drop from results (scope complements).
        exclude_blocked_tags: Override for the blocked-tag exclusion
            policy (``None`` → global runtime setting, see
            ``core/services.py``).
    """

    conditions: SearchConditions = Field(default_factory=list)
    order_by: str = "like"
    order_direction: str = "DESC"
    cursor: dict[str, Any] | None = None
    per_page: int = 50
    min_like: int | None = None
    min_text: int | None = None
    exclude_ids: list[int] = Field(default_factory=list)
    exclude_blocked_tags: bool | None = None


# ---------------------------------------------------------------------------
# Blocked-tag exclusion policy — pure domain rules (docs/MODULARITY.md §M4).
#
# Novels carrying user-blocked (厌恶) tags are hidden from browsing, search
# and counts (global toggle, default ON).  The infrastructure layer supplies
# the data (the global setting value, the blocked tag names); the *decision*
# lives here so repositories never embed business rules.
#
# The runtime-setting key is owned by this module too — the system endpoint
# and the read repository both resolve it through here (previously the
# string was duplicated across layers).
# ---------------------------------------------------------------------------

# Runtime-setting key for the global "exclude blocked-tag novels" toggle.
EXCLUDE_BLOCKED_SETTING_KEY = "exclude_blocked_tag_novels"

# Exclusion is ON when the settings row is missing.
DEFAULT_ACTIVE = True

_TRUTHY = ("1", "true", "yes", "on")


def resolve_active(
    explicit: bool | None,
    global_setting_value: str | None,
) -> bool:
    """Decide whether blocked-tag exclusion applies to a query.

    Args:
        explicit: The API ``exclude_blocked`` override; wins when given.
        global_setting_value: Raw value of the runtime setting row
            (``None`` when the row is missing → default ON).

    Reads are tiny single-row lookups with no caching, so toggles take
    effect immediately — this function must stay pure and cheap.
    """
    if explicit is not None:
        return explicit
    if global_setting_value is None:
        return DEFAULT_ACTIVE
    return global_setting_value.strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Batch ZIP archive builder — pure function.
# ---------------------------------------------------------------------------

# Buffers up to 8 MB stay in memory; larger batches (e.g. 200 EPUBs)
# spill to a disk-backed temp file instead of exhausting RAM.
_SPOOL_MAX = 8 * 1024 * 1024

# Progress callback fires every N processed files (used by the background
# batch_export task to report live progress).
_PROGRESS_EVERY = 500


def build_batch_zip(
    novels: list[dict[str, Any]],
    format_mode: str = "txt",
    naming_template: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[BinaryIO, list[str], list[str]]:
    """Build a ZIP of novel files matching the given criteria.

    Args:
        novels: List of novel dicts, each containing at least ``id``, ``path``,
                ``title``, ``author_name``, ``series_id``, ``series_name``,
                ``series_index``, and ``has_epub``.
        format_mode: ``'txt'`` or ``'prefer_epub'`` (prefers EPUB when available).
        naming_template: Token-based naming template for ZIP arcnames.
                Defaults to ``{author_name}/{series_name}/#{series_index}_{title}_{id}``.
        progress: Optional callback ``(processed, total)`` invoked every
                ~500 files — lets long-running exports report progress.

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
            for index, novel in enumerate(novels, start=1):
                novel_id = novel.id
                novel_path_str = novel.path
                title = novel.title or str(novel_id)

                actual_fmt = (
                    "epub"
                    if (format_mode == "prefer_epub"
                        and novel.has_epub == EpubStatus.DONE)
                    else "txt"
                )

                if not novel_path_str:
                    missing_ids.append(str(novel_id))
                else:
                    file_path = Path(novel_path_str).with_suffix("." + actual_fmt)
                    if not file_path.is_file():
                        missing_ids.append(str(novel_id))
                    else:
                        arcname = template.resolve(novel) + "." + actual_fmt
                        zf.write(str(file_path), arcname)
                        added_titles.append(title)

                if progress is not None and index % _PROGRESS_EVERY == 0:
                    progress(index, len(novels))
    except Exception:
        # Never leak the spooled file on partial builds.
        zip_buffer.close()
        raise

    return zip_buffer, added_titles, missing_ids


__all__ = [
    "parse_tags",
    "normalize_tag",
    "is_chinese",
    "has_image_placeholders",
    "safe_filename",
    "build_path",
    "safe_get",
    "safe_set",
    "guess_series_order",
    "parse_search_keyword",
    "build_batch_zip",
]
