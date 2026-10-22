"""Pure domain services — no I/O, no side effects.

Merged from ``domain/services/*`` (8 files + ``__init__.py`` re-exports).

Filename/naming-template rules live in :mod:`copixiv.core.naming` and the
batch ZIP builder in :mod:`copixiv.core.zip`; both are re-exported here so
existing ``from copixiv.core.services import ...`` call sites keep working.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Timestamp normalisation (postgres-migration) — pure functions.
# ---------------------------------------------------------------------------

# Pixiv API timestamps are either full ISO strings with a TZ offset (``cdate``)
# or bare dates (``create_date[:10]``).  Naive values are interpreted as
# Asia/Tokyo (JST) — the source-scale timezone — before being stored as UTC.
_JST = ZoneInfo("Asia/Tokyo")


def parse_pixiv_time(value: str | None) -> datetime | None:
    """Normalise a Pixiv source timestamp to an aware UTC ``datetime``.

    Accepts:
      * an ISO timestamp with an offset (``+09:00`` etc.) — parsed via
        ``dateutil``/``fromisoformat`` and converted to UTC;
      * a bare date ``YYYY-MM-DD`` — assumed ``Asia/Tokyo`` midnight;
      * an existing ``datetime`` (returned as-is, only converted to UTC).

    Returns ``None`` for empty / unparseable input.  PostgreSQL stores
    ``timestamptz`` internally as UTC, so the write path calls this before
    binding so ordering/range queries behave correctly.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            dt = datetime.strptime(s, "%Y-%m-%d")
        else:
            try:
                from dateutil.parser import isoparse
                dt = isoparse(s)
            except Exception:
                try:
                    dt = datetime.fromisoformat(s)
                except Exception:
                    return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_JST)
    return dt.astimezone(timezone.utc)


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
# Image placeholders — text and byte entry points share one pattern.
# ---------------------------------------------------------------------------

# Pattern for embedded images in novel text.  This is the **only** definition
# of the marker syntax in the codebase: text callers use
# ``has_image_placeholders`` / ``IMAGE_PLACEHOLDER_PATTERN`` (the builder needs
# the compiled pattern's capture groups to substitute), and byte callers use
# ``has_image_placeholders_bytes``.  Do not re-spell this regex anywhere else.
IMAGE_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(
    r"\[(uploadedimage|pixivimage):([\d\-]+)\]"
)
_HAS_IMAGE_PATTERN_BYTES: re.Pattern[bytes] = re.compile(
    IMAGE_PLACEHOLDER_PATTERN.pattern.encode()
)

# Bytes a chunked byte scan must carry between reads so a marker straddling a
# chunk boundary is still matched.  A marker is ``[uploadedimage:`` (15) or
# ``[pixivimage:`` (12), then digits/hyphens, then ``]`` — real ids are well
# under 20 digits, so 64 bytes covers the whole token with room to spare and
# costs nothing on a 1 MiB read.
MARKER_OVERLAP = 64


def has_image_placeholders(content: str | None) -> bool:
    """Check whether the novel text contains image placeholders.

    Placeholders look like ``[uploadedimage:12345]`` or ``[pixivimage:67890]``.
    ``None`` / empty content → ``False`` (no images to convert).
    """
    if not content:
        return False
    return bool(IMAGE_PLACEHOLDER_PATTERN.search(content))


def has_image_placeholders_bytes(content: bytes | None) -> bool:
    """Byte-level variant of :func:`has_image_placeholders`.

    Exists so file sweeps can look for a marker **without decoding** the
    whole document (``check_epub`` walks 238 k EPUB members; ``maintenance``
    reads 1 MiB chunks of novel text).  Same syntax, same owner — this is not
    a second regex, just the bytes view of the one pattern.
    """
    if not content:
        return False
    return bool(_HAS_IMAGE_PATTERN_BYTES.search(content))


# ---------------------------------------------------------------------------
# Content fingerprinting — pure functions (2026-09 正文变更检测).
# ---------------------------------------------------------------------------
#
# Two callers, two *different* inputs — the distinction is deliberate and
# must not be "unified" by accident:
#
# * local uploads (``features/library/ingest.py::body_fingerprint``) hash the
#   **normalised** body; the value is a dedup key (``novel.source_key``,
#   partial unique index).
# * the refresh path (``features/novels/download_novel.py``) hashes the body
#   **exactly as it would be written to disk**, to answer "is the file on disk
#   still the body pixiv serves?" — a change detector, never persisted.


def sha256_text(text: str) -> str:
    """sha256 hex digest of *text* (UTF-8) — the single hashing primitive."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_unchanged(local_text: str | None, incoming: str | None) -> bool:
    """Is the freshly fetched body identical to the local copy?

    ``None`` / empty on either side → ``False`` ("no usable local body, so
    nothing can be considered unchanged").  Comparison is over the **raw**
    strings: no newline normalisation, no stripping — the byte-for-byte text
    that ``FileStorage.save_novel_text`` writes is what gets hashed.
    """
    if not local_text or not incoming:
        return False
    return sha256_text(local_text) == sha256_text(incoming)


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
# Backward-compatible re-exports — the naming/ZIP code moved to sibling
# modules (``core/naming.py`` / ``core/zip.py``), but these names stay
# importable from here so no call site has to change.
# ---------------------------------------------------------------------------
from copixiv.core.naming import (  # noqa: E402  re-exported: call sites still import from services
    NovelNamingTemplate,
    _sanitize_path_segment,
    build_path,
    safe_filename,
)
from copixiv.core.zip import build_batch_zip  # noqa: E402  re-exported

__all__ = [
    "parse_tags",
    "normalize_tag",
    "IMAGE_PLACEHOLDER_PATTERN",
    "has_image_placeholders",
    "has_image_placeholders_bytes",
    "MARKER_OVERLAP",
    "sha256_text",
    "body_unchanged",
    "safe_filename",
    "build_path",
    "NovelNamingTemplate",
    "_sanitize_path_segment",
    "safe_get",
    "safe_set",
    "guess_series_order",
    "parse_search_keyword",
    "build_batch_zip",
    "parse_pixiv_time",
]
