"""NovelDraft — write-path carrier, field accessors, and factory builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from copixiv.core.models import SOURCE_PIXIV, EpubStatus
from copixiv.core.naming import build_path
from copixiv.core.text import has_image_placeholders, parse_tags


def safe_get(data: Any, path: str, default: Any = None) -> Any:
    """Read a (possibly nested) field from a Pydantic model or dict."""
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


def guess_series_order(navigation: Any) -> int | None:
    """Infer a novel's 1-based chapter order from Pixiv series navigation."""
    try:
        inferred: int | None = None
        prev = safe_get(navigation, "prevNovel")
        if prev is not None:
            order = safe_get(prev, "contentOrder")
            if order is not None:
                inferred = int(order) + 1
        if inferred is None:
            nxt = safe_get(navigation, "nextNovel")
            if nxt is not None:
                order = safe_get(nxt, "contentOrder")
                if order is not None:
                    inferred = int(order) - 1
        return inferred if inferred is not None and inferred > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class NovelDraft:
    """Frozen write-path carrier built from raw or normalized source payloads."""

    id: int
    title: str
    author_id: int | None = None
    author_name: str | None = None
    path: str = ""
    like: int = 0
    view: int = 0
    text: int = 0
    caption: str | None = None
    series_id: int | None = None
    series_name: str | None = None
    series_index: int | None = None
    create_time: str | None = None
    has_epub: EpubStatus | None = None
    tags: list[str] = field(default_factory=list)
    content: str | None = None
    rewrote_body: bool = False
    images: dict | None = None
    illusts: dict | None = None
    cover_url: str | None = None
    source: str = SOURCE_PIXIV
    source_key: str | None = None

    def __post_init__(self) -> None:
        if self.source_key is None and self.source == SOURCE_PIXIV and self.id:
            object.__setattr__(self, "source_key", str(self.id))


class UserInfoLike(Protocol):
    """Input contract for the user object attached to a Pixiv novel item."""

    id: int
    name: str


class SeriesInfoLike(Protocol):
    """Input contract for the optional series object on a Pixiv novel item."""

    id: Any
    title: Any
    index: Any


class NovelInfoLike(Protocol):
    """Input contract for Pixiv novel metadata items passed into ingest."""

    id: int
    title: str
    caption: str
    tags: list
    user: UserInfoLike
    series: SeriesInfoLike | None
    total_bookmarks: int
    total_view: int
    text_length: int
    create_date: str


def build_novel(
    *,
    id: int,
    title: str,
    author_id: int | None = None,
    author_name: str | None = None,
    path: str | None = None,
    like: int = 0,
    view: int = 0,
    text: int = 0,
    caption: str | None = None,
    series_id: int | None = None,
    series_name: str | None = None,
    series_index: int | None = None,
    create_time: str | None = None,
    has_epub: EpubStatus | None = EpubStatus.NO,
    tags: list[str | dict] | None = None,
    content: str | None = None,
    images: dict | None = None,
    illusts: dict | None = None,
    cover_url: str | None = None,
    source: str = SOURCE_PIXIV,
    source_key: str | None = None,
    download_dir: str = "download",
) -> NovelDraft:
    """Build a canonical :class:`NovelDraft` from raw source fields."""
    resolved_key = source_key if source_key is not None else str(id)
    return NovelDraft(
        id=id,
        title=title,
        author_id=author_id,
        author_name=author_name,
        path=build_path(id, title, download_dir) if path is None else path,
        like=like,
        view=view,
        text=text,
        caption=caption,
        series_id=series_id,
        series_name=series_name,
        series_index=series_index,
        create_time=create_time,
        has_epub=has_epub,
        tags=parse_tags(tags or []),
        content=content,
        images=images,
        illusts=illusts,
        cover_url=cover_url,
        source=source,
        source_key=resolved_key,
    )


def build_from_webview(data: Any, download_dir: str = "download") -> NovelDraft:
    """Build a :class:`NovelDraft` from a webview novel payload."""
    if not data:
        return NovelDraft(id=0, title="", author_id=0)

    body = data.text or ""
    return build_novel(
        id=int(data.id),
        title=data.title,
        author_id=int(data.user_id),
        author_name=None,
        like=safe_get(data, "rating.bookmark", 0),
        view=safe_get(data, "rating.view", 0),
        text=len(body),
        caption=data.caption,
        series_id=int(data.series_id) if data.series_id else None,
        series_name=data.series_title,
        series_index=guess_series_order(data.series_navigation),
        create_time=data.cdate,
        has_epub=(EpubStatus.PENDING if has_image_placeholders(body) else EpubStatus.NO_IMAGES),
        tags=data.tags,
        content=body,
        images=data.images if isinstance(data.images, dict) else None,
        illusts=data.illusts if isinstance(data.illusts, dict) else None,
        cover_url=data.cover_url,
        download_dir=download_dir,
    )


def build_from_novel_info(data: NovelInfoLike, download_dir: str = "download") -> NovelDraft:
    """Build a metadata-only :class:`NovelDraft` from a novel list item."""
    user = data.user
    series = data.series
    return build_novel(
        id=data.id,
        title=data.title,
        author_id=safe_get(user, "id"),
        author_name=safe_get(user, "name"),
        path="",
        like=data.total_bookmarks,
        view=data.total_view,
        text=data.text_length,
        caption=data.caption,
        series_id=safe_get(series, "id"),
        series_name=safe_get(series, "title"),
        series_index=safe_get(series, "index"),
        create_time=data.create_date or None,
        has_epub=None,
        tags=[safe_get(tag, "name", str(tag)) for tag in data.tags],
        download_dir=download_dir,
    )


__all__ = [
    "NovelDraft",
    "NovelInfoLike",
    "SeriesInfoLike",
    "UserInfoLike",
    "build_from_novel_info",
    "build_from_webview",
    "build_novel",
    "guess_series_order",
    "safe_get",
]
