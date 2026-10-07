"""Series name resolution for one series id — read-only, local first.

The editor's series field edits **``series_id``**, not ``series_name``
(``docs/NOVEL_EDIT_DESIGN.md`` §5.6): the id is the identity, the name is a
cache that gets resolved.  This module answers "what is series N called?"
without writing anything, so both the live preview
(``GET /api/novels/series/{id}``) and the ``PATCH`` path can share it.

Three sources, tried in order — cheapest and most certain first:

1. the local ``series`` row's ``series_name`` (maintained by
   ``update_summary`` from its novels' names);
2. the local ``novel`` rows that already carry that ``series_id``
   (``max(series_name)`` — exactly what ``update_summary`` would derive);
3. Pixiv's ``novel_series`` listing, whose novels carry ``series.title``.

Only step 3 touches the network, and its failure is **not** an error: a
series name nobody can resolve today is filled in later by
``update_summary`` the next time that series is scraped.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from copixiv.core.services import safe_get
from copixiv.log import logger
from copixiv.db import models
from copixiv.pixiv.client import PixivClient
from copixiv.features.novels.repo import SQLAlchemySeriesRepository


@dataclass(frozen=True)
class SeriesProfile:
    """What the editor can say about one Pixiv series id.

    ``source`` records where the name came from (the editor shows it):
    ``local`` costs nothing and works offline, ``pixiv`` means a live request
    just happened, ``unknown`` means the id maps to no name we could obtain —
    the caller may still store the id, because the mapping is the user's
    intent and the name is a cache.
    """

    series_id: int
    series_name: str | None
    source: str  # 'local' | 'pixiv' | 'unknown'


async def lookup_series_profile(
    series_id: int,
    *,
    session: Session,
    client: PixivClient | None,
) -> SeriesProfile:
    """Resolve one series id → name.  Read-only, never raises."""
    row = await SQLAlchemySeriesRepository(session).get_by_id(series_id)
    name = (row or {}).get("series_name")
    if name:
        return SeriesProfile(series_id, name, "local")

    # A series row can exist with a NULL name (placeholder) while its novels
    # already carry one — e.g. the row was created by ``ensure_exists`` right
    # before the first novel of that series was written.
    stored = session.execute(
        select(func.max(models.Novel.series_name)).where(
            models.Novel.series_id == series_id,
            models.Novel.series_name.isnot(None),
        )
    ).scalar()
    if stored:
        return SeriesProfile(series_id, stored, "local")

    name = await fetch_series_name(client, series_id)
    if name:
        return SeriesProfile(series_id, name, "pixiv")
    return SeriesProfile(series_id, None, "unknown")


async def fetch_series_name(
    client: PixivClient | None,
    series_id: int,
) -> str | None:
    """Ask Pixiv what one series is called.

    ``None`` on every failure path — no client, API error (unknown / deleted
    series, rate limit, cooling), or a payload with no title anywhere.  The
    response shape is not contractual: ``/v2/novel/series`` returns a bare
    ``{novels: [...]}`` whose items carry ``series`` — but the schema drifts,
    so the title is looked up in both places (and through ``safe_get``, which
    works on dicts *and* pydantic models alike).
    """
    if client is None:
        return None
    try:
        resp = await client.novel_series(series_id)
    except Exception as exc:  # noqa: BLE001 — any transport/auth error degrades
        logger.warning(f"Failed to fetch name for series #{series_id}: {exc}")
        return None

    name = safe_get(resp, "series.title") or safe_get(resp, "series_name")
    if name:
        return name
    for novel in safe_get(resp, "novels", []) or []:
        title = safe_get(novel, "series.title")
        if title:
            return title
    return None
