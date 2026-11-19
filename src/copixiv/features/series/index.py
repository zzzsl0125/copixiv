"""Series index resolution at ingest time — ask the roster when the guess fails."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from copixiv.core.draft import NovelDraft, safe_get
from copixiv.log import logger


def _positions_by_series(drafts: list[NovelDraft]) -> dict[int, list[int]]:
    needs: dict[int, list[int]] = {}
    for index, draft in enumerate(drafts):
        if draft.series_id and draft.series_index is None:
            needs.setdefault(int(draft.series_id), []).append(index)
    return needs


def _authority_from_roster(roster: list) -> dict[int, int]:
    """``{novel_id: chapter_number}`` from a roster, 1-based by position."""
    authority: dict[int, int] = {}
    for pos0, item in enumerate(roster):
        item_id = safe_get(item, "id")
        if item_id is None:
            continue
        authority[int(item_id)] = pos0 + 1
    return authority


async def _roster(client, series_id: int) -> list | None:
    try:
        resp = await client.novel_series(series_id, fetch_all=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"series_index: 系列 {series_id} 名册请求失败（保留 NULL）: {exc}")
        return None
    novels = safe_get(resp, "novels", None)
    return list(novels) if novels else None


async def resolve_series_indices(drafts: list[NovelDraft], client) -> list[NovelDraft]:
    """Fill ``series_index`` for drafts that have a series but no index."""
    if client is None or not drafts:
        return drafts

    needs = _positions_by_series(drafts)
    if not needs:
        return drafts

    series_ids = list(needs)
    rosters = await asyncio.gather(
        *[_roster(client, sid) for sid in series_ids],
    )

    resolved = list(drafts)
    filled = 0
    for series_id, roster in zip(series_ids, rosters, strict=True):
        if not roster:
            continue
        authority = _authority_from_roster(roster)
        if not authority:
            continue
        for index in needs[series_id]:
            want = authority.get(drafts[index].id)
            if want is None:
                continue
            resolved[index] = replace(drafts[index], series_index=want)
            filled += 1

    if filled:
        logger.info(f"series_index: 下载阶段按名册补号 {filled} 本（涉及 {len(series_ids)} 个系列）")
    return resolved


__all__ = ["resolve_series_indices"]
