"""Novel dict factory — builds the canonical novel dict from raw API data."""

from typing import Any

from .filename import build_path
from .tags import parse_tags
from .parsing import safe_get, guess_series_order
from .language import has_image_placeholders


def build_novel_dict(
    *,
    id: int,
    title: str,
    author_id: int,
    author_name: str | None = None,
    like: int = 0,
    view: int = 0,
    text: int = 0,
    caption: str | None = None,
    series_id: int | None = None,
    series_name: str | None = None,
    series_index: int | None = None,
    create_time: str | None = None,
    has_epub: int = 0,
    tags: list[str | dict] | None = None,
    # Transient fields (popped before DB upsert)
    content: str | None = None,
    images: dict | None = None,
    illusts: dict | None = None,
    cover_url: str | None = None,
    download_dir: str = "download",
) -> dict[str, Any]:
    """Build a canonical novel dictionary from raw Pixiv API fields.

    The returned dict contains everything needed for both DB persistence
    and asset download.  Transient fields (``content``, ``images``,
    ``illusts``, ``cover_url``) must be popped before the DB upsert.
    """
    return {
        "id": id,
        "title": title,
        "author_id": author_id,
        "author_name": author_name,
        "path": build_path(id, title, download_dir),
        "like": like,
        "view": view,
        "text": text,
        "caption": caption,
        "series_id": series_id,
        "series_name": series_name,
        "series_index": series_index,
        "create_time": create_time,
        "has_epub": has_epub,
        "tag": parse_tags(tags or []),
        # Transient — popped before DB insert
        "content": content,
        "images": images,
        "illusts": illusts,
        "cover_url": cover_url,
    }


# ---- Higher-level builders that use the factory ----

def build_from_webview(data: Any, download_dir: str = "download") -> dict[str, Any]:
    """Build a novel dict from a ``webview_novel`` API response."""
    if not data:
        return {}

    return build_novel_dict(
        id=int(data.id),
        title=data.title,
        author_id=int(data.user_id),
        author_name=None,
        like=safe_get(data, "rating.bookmark", 0),
        view=safe_get(data, "rating.view", 0),
        text=len(data.text),
        caption=data.caption,
        series_id=int(data.series_id) if data.series_id else None,
        series_name=data.series_title,
        series_index=guess_series_order(data.series_navigation),
        create_time=data.cdate,
        has_epub=1 if has_image_placeholders(data.text) else 0,
        tags=data.tags,
        content=data.text,
        images=data.images,
        illusts=data.illusts,
        cover_url=data.cover_url,
        download_dir=download_dir,
    )


def build_from_novel_info(data: Any, download_dir: str = "download") -> dict[str, Any]:
    """Build a novel dict from a ``novelInfo`` API response (metadata only)."""
    user = data.user
    series = data.series
    return build_novel_dict(
        id=data.id,
        title=data.title,
        author_id=safe_get(user, "id"),
        author_name=safe_get(user, "name"),
        like=data.total_bookmarks,
        view=data.total_view,
        text=data.text_length,
        caption=data.caption,
        series_id=safe_get(series, "id"),
        series_name=safe_get(series, "title"),
        series_index=safe_get(series, "index"),
        create_time=data.create_date[:10],
        has_epub=0,
        tags=[safe_get(tag, "name", str(tag)) for tag in data.tags],
        download_dir=download_dir,
    )
