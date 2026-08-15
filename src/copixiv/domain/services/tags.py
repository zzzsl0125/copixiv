"""Tag parsing utilities — pure functions."""

import re

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
