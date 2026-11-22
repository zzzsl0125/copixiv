"""Pure text rules: tags, image placeholders, body hashes, and language detection."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from copixiv.core.exceptions import ValidationError

MAX_TAG_LENGTH = 100

_CLEAN_PATTERN: re.Pattern[str] = re.compile(r"[()]")
_SPLIT_PATTERN: re.Pattern[str] = re.compile(r"[/|#、\\]")


def normalize_tag(tag: str) -> str:
    """Normalise a single tag by trimming whitespace and lowercasing."""
    return tag.strip().lower()


def parse_tags(tags: Sequence[str | dict[str, Any]]) -> list[str]:
    """Parse Pixiv API tag entries into a deduplicated, cleaned list."""
    result: dict[str, None] = {}
    for tag in tags:
        if tag is None:
            continue
        tag_text: str = tag.get("name", "") if isinstance(tag, dict) else str(tag)
        if not tag_text:
            continue
        cleaned = _CLEAN_PATTERN.sub("", tag_text)
        for part in _SPLIT_PATTERN.split(cleaned):
            part = normalize_tag(part)
            if part:
                result.setdefault(part)
    return list(result)


def validate_tags(
    tags: Sequence[str | dict[str, Any]],
    *,
    max_count: int | None = None,
    max_length: int = MAX_TAG_LENGTH,
    require_non_empty: bool = False,
) -> list[str]:
    """Clean tags and validate count/length invariants, raising ``ValidationError``."""
    cleaned = parse_tags(tags)
    if require_non_empty and not cleaned:
        raise ValidationError("请至少输入一个标签")
    if max_count is not None and len(cleaned) > max_count:
        raise ValidationError(f"一次最多操作 {max_count} 个标签（当前 {len(cleaned)} 个）")
    for tag in cleaned:
        if len(tag) > max_length:
            raise ValidationError(f"标签过长（单个最多 {max_length} 字）：{tag[:20]}…")
    return cleaned


IMAGE_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"\[(uploadedimage|pixivimage):([\d\-]+)\]")
_HAS_IMAGE_PATTERN_BYTES: re.Pattern[bytes] = re.compile(IMAGE_PLACEHOLDER_PATTERN.pattern.encode())
MARKER_OVERLAP = 64


def has_image_placeholders(content: str | None) -> bool:
    """Check whether novel text contains ``[uploadedimage:...]`` / ``[pixivimage:...]``."""
    if not content:
        return False
    return bool(IMAGE_PLACEHOLDER_PATTERN.search(content))


def has_image_placeholders_bytes(content: bytes | None) -> bool:
    """Byte-level variant of :func:`has_image_placeholders`."""
    if not content:
        return False
    return bool(_HAS_IMAGE_PATTERN_BYTES.search(content))


def sha256_text(text: str) -> str:
    """Return the UTF-8 SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_unchanged(local_text: str | None, incoming: str | None) -> bool:
    """True when both bodies are non-empty and identical."""
    if not local_text or not incoming:
        return False
    return local_text == incoming


# Match Japanese hiragana and katakana letters/iteration marks while excluding
# punctuation symbols shared by Chinese titles/translations (e.g. '・' U+30FB,
# 'ー' U+30FC, '゠' U+30A0).
_JAPANESE_REGEX: re.Pattern[str] = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fd-\u30ff]")

_CHINESE_TAG_KEYWORDS: frozenset[str] = frozenset(
    {
        "中文",
        "中文作品",
        "中文注意",
        "简中",
        "简体中文",
        "繁体",
        "繁體",
        "中文中国语",
        "中国文",
        "中文語",
        "中文语",
        "中国语",
        "中国語",
        "中阈语",
        "中國语",
        "中國語",
        "中国語注意",
        "中國語注意",
        "中国语注意",
        "chinese",
        "中國",
        "中国",
    }
)

_HAN_RE: re.Pattern[str] = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE: re.Pattern[str] = re.compile(r"[A-Za-z]")
_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]*>")
_SCHEME_RE: re.Pattern[str] = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)

_MIN_HAN = 1
_MIN_HAN_RATIO = 0.1


def _strip_noise(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    return _SCHEME_RE.sub(" ", text)


def _han_ratio(text: str) -> tuple[int, int]:
    stripped = _strip_noise(text)
    return len(_HAN_RE.findall(stripped)), len(_LATIN_RE.findall(stripped))


def is_chinese(
    title: str = "",
    caption: str = "",
    tags: list[str] | None = None,
) -> bool:
    """Whether a Pixiv work should be treated as Chinese-language."""
    if tags and any(t.lower() in _CHINESE_TAG_KEYWORDS for t in tags):
        return True

    sample = (title or "") + (caption or "")
    if not sample.strip():
        return False

    if _JAPANESE_REGEX.search(sample):
        return False

    han, latin = _han_ratio(sample)
    if han < _MIN_HAN:
        return False
    return han / max(han + latin, 1) >= _MIN_HAN_RATIO


def filter_chinese(novels: list[Any]) -> list[Any]:
    """Filter Pixiv novel payload items down to Chinese works."""
    from copixiv.core.draft import safe_get

    return [
        n
        for n in novels
        if is_chinese(
            title=safe_get(n, "title", ""),
            caption=safe_get(n, "caption", ""),
            tags=[safe_get(t, "name", str(t)) for t in safe_get(n, "tags", None) or []],
        )
    ]


__all__ = [
    "IMAGE_PLACEHOLDER_PATTERN",
    "MARKER_OVERLAP",
    "MAX_TAG_LENGTH",
    "body_unchanged",
    "filter_chinese",
    "has_image_placeholders",
    "has_image_placeholders_bytes",
    "is_chinese",
    "normalize_tag",
    "parse_tags",
    "sha256_text",
    "validate_tags",
]
