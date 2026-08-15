"""Language detection — pure functions (with optional langid I/O)."""

import logging
import re

# stdlib logging keeps this domain module free of app-layer imports; the
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
