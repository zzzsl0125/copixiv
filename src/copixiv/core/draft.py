"""NovelDraft — the write-path carrier, plus the factory functions that build it.

This module owns the **write path**: everything that turns raw Pixiv API
payloads into the object that repositories persist.  The read path keeps
returning the :class:`~copixiv.core.models.Novel` model (see the read
repository); :class:`NovelDraft` is deliberately a plain frozen dataclass —
no runtime validation, no serializer, no pydantic machinery — so the
repository can convert it to a row dict with ``dict(n.__dict__)`` in a
worker thread without any serializer-rebuild race (see
``features/novels/repo.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from copixiv.core.models import EpubStatus
from copixiv.core.services import (
    build_path,
    parse_tags,
    guess_series_order,
    has_image_placeholders,
    safe_get,
)


@dataclass(frozen=True)
class NovelDraft:
    """A frozen write-path carrier built from raw Pixiv API data.

    Mirrors the fields produced by :func:`build_novel`; the repository
    converts it to a row dict via ``dict(n.__dict__)`` (a frozen dataclass,
    so no runtime validation / serializer race) and persists only the
    columns in its whitelist.  ``has_epub=None`` means "don't overwrite the
    stored value" on a metadata-only refresh; ``download_dir`` records the
    root the draft was built for (only used to compute ``path``).
    """

    id: int
    title: str
    author_id: int
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
    # ``None`` = "don't overwrite the stored value" (metadata-only refresh).
    has_epub: EpubStatus | None = None
    tags: list[str] = field(default_factory=list)
    content: str | None = None
    images: dict | None = None
    illusts: dict | None = None
    cover_url: str | None = None
    download_dir: str = "download"


class UserInfoLike(Protocol):
    """The fields ``build_from_novel_info`` reads from a pixivpy3 user object."""

    id: int
    name: str


class SeriesInfoLike(Protocol):
    """The fields read from a pixivpy3 series object (may be ``None``)."""

    id: Any
    title: Any
    index: Any


class NovelInfoLike(Protocol):
    """Input contract for novelInfo-style data (a pixivpy3 Novel object).

    The ingest pipeline (``copixiv.features.novels.ingest``) feeds the same kind of
    objects into its plan phase, so this Protocol documents the shared
    input contract for both consumers.
    """

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
    has_epub: EpubStatus | None = EpubStatus.NO,
    tags: list[str | dict] | None = None,
    # Transient fields — consumed by download/asset code, never persisted
    content: str | None = None,
    images: dict | None = None,
    illusts: dict | None = None,
    cover_url: str | None = None,
    download_dir: str = "download",
) -> NovelDraft:
    """Build a canonical :class:`NovelDraft` from raw Pixiv API fields.

    The returned draft contains everything needed for both DB persistence
    and asset download.  Transient fields (``content``, ``images``,
    ``illusts``, ``cover_url``) are excluded from persistence by the
    repository's column whitelist.
    """
    return NovelDraft(
        id=id,
        title=title,
        author_id=author_id,
        author_name=author_name,
        path=build_path(id, title, download_dir),
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
        download_dir=download_dir,
    )


# ---- Higher-level builders that use the factory ----

def build_from_webview(data: Any, download_dir: str = "download") -> NovelDraft:
    """Build a :class:`NovelDraft` from a ``webview_novel`` API response.

    Tolerates missing ``text`` (deleted/restricted novels may return None):
    ``text`` falls back to 0 and ``has_epub`` to NO.
    """
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
        has_epub=EpubStatus.PENDING if has_image_placeholders(body) else EpubStatus.NO,
        tags=data.tags,
        content=body,
        # The webview API returns ``images``/``illusts`` as empty *lists*
        # (not dicts) for novels without images — normalise to None.
        images=data.images if isinstance(data.images, dict) else None,
        illusts=data.illusts if isinstance(data.illusts, dict) else None,
        cover_url=data.cover_url,
        download_dir=download_dir,
    )


def build_from_novel_info(
    data: NovelInfoLike, download_dir: str = "download"
) -> NovelDraft:
    """Build a :class:`NovelDraft` from a ``novelInfo`` API response (metadata only).

    The body text is not available, so the image-placeholder state (and
    thus ``has_epub``) cannot be computed here: ``has_epub`` is ``None``,
    which the repository interprets as "leave the stored value untouched" —
    otherwise every metadata refresh would overwrite PENDING/DONE with NO.
    """
    user = data.user
    series = data.series
    return build_novel(
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
        create_time=(data.create_date or "")[:10],
        has_epub=None,
        tags=[safe_get(tag, "name", str(tag)) for tag in data.tags],
        download_dir=download_dir,
    )


__all__ = [
    "NovelDraft",
    "UserInfoLike",
    "SeriesInfoLike",
    "NovelInfoLike",
    "build_novel",
    "build_from_novel_info",
    "build_from_webview",
]
