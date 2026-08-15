"""Novel factory — builds canonical :class:`Novel` domain models from raw API data.

v2 contract (M9 unification): the factory returns ``Novel`` instances, not
plain dicts.  Use cases and repositories consume the model directly; dicts
only appear at the SQLAlchemy row boundary and the HTTP wire.
"""

from typing import Any, Protocol

from copixiv.domain.models.novel import EpubStatus, Novel
from .filename import build_path
from .tags import parse_tags
from .parsing import safe_get, guess_series_order
from .language import has_image_placeholders


# ---------------------------------------------------------------------------
# Input contract for novelInfo-style data
# ---------------------------------------------------------------------------


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

    The batch pipeline (``tasks/pipeline.py``) feeds the same kind of
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
) -> Novel:
    """Build a canonical ``Novel`` domain model from raw Pixiv API fields.

    The returned model contains everything needed for both DB persistence
    and asset download.  Transient fields (``content``, ``images``,
    ``illusts``, ``cover_url``) are excluded from persistence by the
    repository's column whitelist.
    """
    return Novel(
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
    )


# ---- Higher-level builders that use the factory ----

def build_from_webview(data: Any, download_dir: str = "download") -> Novel:
    """Build a ``Novel`` from a ``webview_novel`` API response.

    Tolerates missing ``text`` (deleted/restricted novels may return None):
    ``text`` falls back to 0 and ``has_epub`` to NO.
    """
    if not data:
        return Novel(id=0, title="", author_id=0)

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
        images=data.images,
        illusts=data.illusts,
        cover_url=data.cover_url,
        download_dir=download_dir,
    )


def build_from_novel_info(
    data: NovelInfoLike, download_dir: str = "download"
) -> Novel:
    """Build a ``Novel`` from a ``novelInfo`` API response (metadata only).

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
