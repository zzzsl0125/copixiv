"""Series name resolution for one series id — read-only, local first."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from copixiv.core.draft import safe_get
from copixiv.db.tx import read_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient


@dataclass(frozen=True)
class SeriesProfile:
    """Resolved series metadata alongside its lookup origin."""

    series_id: int
    series_name: str | None
    source: str  # 'local' | 'pixiv' | 'unknown'


def _lookup_series_local(session: Session, series_id: int) -> str | None:
    return SQLAlchemySeriesRepository(session).get_name_fallback(series_id)


async def lookup_series_profile(
    series_id: int,
    *,
    uow: SqlUnitOfWork | None = None,
    session: Session | None = None,
    client: PixivClient | None,
) -> SeriesProfile:
    """Resolve one series id → name. Read-only, never raises."""
    if uow is not None:
        local_name = await read_transaction(uow, lambda tx: _lookup_series_local(tx.session, series_id))
    elif session is not None:
        local_name = _lookup_series_local(session, series_id)
    else:
        raise ValueError("lookup_series_profile requires uow or session")
    if local_name:
        return SeriesProfile(series_id, local_name, "local")

    name = await fetch_series_name(client, series_id)
    if name:
        return SeriesProfile(series_id, name, "pixiv")
    return SeriesProfile(series_id, None, "unknown")


async def fetch_series_name(
    client: PixivClient | None,
    series_id: int,
) -> str | None:
    """Ask Pixiv what one series is called, degrading to ``None`` on error."""
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
