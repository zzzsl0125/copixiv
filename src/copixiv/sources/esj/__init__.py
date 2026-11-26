"""ESJZone multi-chapter web source plugin package."""

from __future__ import annotations

from copixiv.sources.esj.client import (
    EsjChallengeError,
    EsjClient,
    EsjError,
    EsjNotFoundError,
    EsjPasswordRequiredError,
    EsjRequestError,
    normalize_esj_base_url,
    resolve_chapter_password,
    resolve_esj_base_url,
)
from copixiv.sources.esj.parser import (
    DEFAULT_ESJ_BASE_URL,
    EsjParsedChapter,
    build_esj_author_key,
    extract_jinjing_token,
    is_challenge_html,
    is_esj_host,
    is_original_genre,
    is_password_challenge_html,
    parse_book_outline,
    parse_chapter,
    parse_esj_count,
    parse_esj_datetime,
    parse_esj_ref,
)
from copixiv.sources.esj.plugin import EsjSourcePlugin

__all__ = [
    "DEFAULT_ESJ_BASE_URL",
    "EsjChallengeError",
    "EsjClient",
    "EsjError",
    "EsjNotFoundError",
    "EsjParsedChapter",
    "EsjPasswordRequiredError",
    "EsjRequestError",
    "EsjSourcePlugin",
    "build_esj_author_key",
    "extract_jinjing_token",
    "is_challenge_html",
    "is_esj_host",
    "is_original_genre",
    "is_password_challenge_html",
    "normalize_esj_base_url",
    "parse_book_outline",
    "parse_chapter",
    "parse_esj_count",
    "parse_esj_datetime",
    "parse_esj_ref",
    "resolve_chapter_password",
    "resolve_esj_base_url",
]
