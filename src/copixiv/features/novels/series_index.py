"""Series index resolution at ingest time — ask the roster when the guess fails.

**The problem this closes.**  ``novel.series_index`` is filled at download time
from ``guess_series_order(webview.series_navigation)`` — ``prevNovel`` /
``nextNovel``'s ``content_order`` ± 1.  That works for a work published *between*
two siblings, and fails whenever pixiv has no neighbour to anchor against: the
series' only volume, the first or last one when only one pointer survives, or a
series whose navigation the response simply omits.  Those rows were written with
``series_index = NULL`` and repaired **later** by ``fix_series_index`` — a
nightly task that re-fetches rosters for a capped number of series and has been
unable to keep up (0–34 numbers per run against 761 missing rows, still arriving
at 0.67 % of new novels).

Measured on production: **all 761** missing-index rows had a body, i.e. they all
went through ``webview_novel``.  So this is not a "wrong path" problem — the
path ran and could not produce a number.

**The fix, at the point where the information is available.**  When a webview
draft has a ``series_id`` but no index, ingest resolves it from the
``novel_series`` roster — the same authority ``fix_series_index`` uses, but
fetched **once per series per round** rather than once per novel.  Two
properties matter:

* **It fills, never overwrites.**  ``series_index`` sits outside
  ``_upsert_rows``' ``update_fields_set``, so an existing value survives every
  refresh ("filled once, never overwritten").  A number already written —
  including one the nightly repair corrected — is therefore never re-derived
  from a different source.  That is the "resolve once at ingest, then leave it
  alone" behaviour the daily repair cannot give.
* **It cannot make things worse.**  A roster fetch that fails, or one that omits
  the novel, leaves the value ``NULL`` — the same honest "unknown" the guess
  produced.  Nothing is invented.

**Why not derive it from ids.**  Tempting, since pixiv ids are roughly
chronological, and wrong: measured against 20,919 fully-indexed series, **1,320
(6.3 %)** have an id order that disagrees with their chapter order (2,429
inverted pairs out of 124,322).  Authors routinely publish chapter 8 and
backfill chapter 7, so ``id`` says 8 < 7.  Sorting by id is precisely the bug
``d01873d`` replaced with "roster position".
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from copixiv.core.draft import NovelDraft
from copixiv.core.services import safe_get
from copixiv.log import logger


def _positions_by_series(drafts: list[NovelDraft]) -> dict[int, list[int]]:
    """Group the indices of drafts that need a roster lookup, by series."""
    needs: dict[int, list[int]] = {}
    for index, draft in enumerate(drafts):
        if draft.series_id and draft.series_index is None:
            needs.setdefault(int(draft.series_id), []).append(index)
    return needs


def _authority_from_roster(roster: list) -> dict[int, int]:
    """``{novel_id: chapter_number}`` from a roster, 1-based by position.

    The roster's order *is* the author's chapter order (from oldest to newest);
    its position is therefore the chapter number.  This is the same rule
    ``fix_series_index`` applies, and the reason it exists: the order is neither
    id order nor ``create_time`` order — series 16242552's third volume
    「第2.5幕」 has a later id than the fourth, yet the author lists it third.
    """
    authority: dict[int, int] = {}
    for pos0, item in enumerate(roster):
        item_id = safe_get(item, "id")
        if item_id is None:
            continue
        authority[int(item_id)] = pos0 + 1
    return authority


async def _roster(client, series_id: int) -> list | None:
    """The series roster, or ``None`` when it cannot be used.

    Errors are swallowed on purpose: failing to resolve a chapter number must
    never fail the download.  ``fix_series_index`` still finds the row, because
    its candidate query is "series set, index NULL".
    """
    try:
        resp = await client.novel_series(series_id, fetch_all=True)
    except Exception as exc:                                # noqa: BLE001
        logger.warning(
            f"series_index: 系列 {series_id} 名册请求失败（保留 NULL）: {exc}"
        )
        return None
    novels = safe_get(resp, "novels", None)
    return list(novels) if novels else None


async def resolve_series_indices(
    drafts: list[NovelDraft], client,
) -> list[NovelDraft]:
    """Fill ``series_index`` for drafts that have a series but no index.

    Returns a new list; drafts needing nothing are passed through unchanged
    (they are frozen dataclasses, so "unchanged" means the same object).

    *client* may be ``None`` (tests, degraded assembly): nothing is fetched and
    the drafts are returned as given — this helper must never be the reason a
    download round fails.
    """
    if client is None or not drafts:
        return drafts

    needs = _positions_by_series(drafts)
    if not needs:
        return drafts

    series_ids = list(needs)
    # One roster fetch per *series*, not per novel: a round commonly contains
    # several chapters of one series (a follow feed or an author page), so
    # grouping is what keeps this from becoming N extra requests.  Concurrent
    # with each other, sequential per series — same shape as the download
    # fan-out.
    rosters = await asyncio.gather(
        *[_roster(client, sid) for sid in series_ids],
    )

    resolved = list(drafts)
    filled = 0
    for series_id, roster in zip(series_ids, rosters):
        if not roster:
            continue
        authority = _authority_from_roster(roster)
        if not authority:
            continue
        for index in needs[series_id]:
            want = authority.get(drafts[index].id)
            if want is None:
                # The roster does not list this work (restricted, or published
                # after the roster was fetched) → leave NULL, do not invent.
                continue
            resolved[index] = replace(drafts[index], series_index=want)
            filled += 1

    if filled:
        logger.info(
            f"series_index: 下载阶段按名册补号 {filled} 本"
            f"（涉及 {len(series_ids)} 个系列）"
        )
    return resolved


__all__ = ["resolve_series_indices"]
