"""Shared Pixiv HTTP constants and Retry-After parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

# Mobile iOS headers expected by Pixiv App API and image CDN.
PIXIV_IOS_USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
PIXIV_REFERER = "https://www.pixiv.net/"

PIXIV_API_HEADERS: dict[str, str] = {
    "app-os": "ios",
    "app-os-version": "14.6",
    "user-agent": PIXIV_IOS_USER_AGENT,
}

PIXIV_IMAGE_HEADERS: dict[str, str] = {
    "Referer": PIXIV_REFERER,
    "User-Agent": PIXIV_IOS_USER_AGENT,
}


def parse_retry_after(
    header_value: str | None,
    *,
    default: float,
    max_wait: float = 600.0,
) -> float:
    """Parse an HTTP ``Retry-After`` header (integer seconds or HTTP-date)."""
    if not header_value:
        return default
    raw = header_value.strip()
    if not raw:
        return default
    try:
        seconds = float(raw)
        return min(max(seconds, 0.0), max_wait)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = (dt - datetime.now(UTC)).total_seconds()
        return min(max(delta, 0.0), max_wait)
    except (TypeError, ValueError):
        return default


def pick_image_url(
    urls: Any,
    order: tuple[str, ...] = ("original", "large", "medium", "small"),
) -> str | None:
    """Pick the best available image URL by preference order.

    Tolerates ``None`` and dicts missing some sizes (the webview API
    omits sizes it does not carry).
    """
    if not isinstance(urls, dict):
        return None
    for key in order:
        url = urls.get(key)
        if url:
            return url
    return None
