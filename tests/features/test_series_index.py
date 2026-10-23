"""``resolve_series_indices`` — chapter numbers resolved at ingest time.

Why this exists (production measurements, 2026-10-24):

* 761 rows had ``series_id`` but no ``series_index``, and **all 761 had a body**
  — so every one of them went through ``webview_novel`` and the navigation
  simply could not yield a number.  The "two metadata paths" split is real but
  is not the cause: ``build_from_novel_info`` reads ``series.index``, a field
  pixivpy3's ``Series`` model does not even have (it carries only ``id`` and
  ``title``), so that path can never produce a number by construction.
* New novels still arrive missing the index at ~0.67 %, while the nightly
  repair fills 0–34 per run.  Resolving at ingest is what stops the inflow.

The rule is the roster's 1-based position, not the id order: measured against
20,919 fully-indexed series, 1,320 (6.3 %) disagree with their chapter order.
"""

from __future__ import annotations

from copixiv.core.draft import NovelDraft
from copixiv.features.novels.series_index import resolve_series_indices


def _draft(novel_id: int, series_id=None, series_index=None) -> NovelDraft:
    return NovelDraft(
        id=novel_id, title=f"n{novel_id}", author_id=1,
        series_id=series_id, series_index=series_index,
    )


def _info(novel_id: int) -> dict:
    """A roster entry; the resolver only reads ``id``."""
    return {"id": novel_id}


class _FakeClient:
    def __init__(self, rosters=None, fail=()):
        self.rosters = rosters or {}
        self.fail = set(fail)
        self.calls: list[int] = []

    async def novel_series(self, series_id: int, fetch_all: bool = False):
        self.calls.append(series_id)
        if series_id in self.fail:
            raise RuntimeError("pixiv unavailable")
        return {"novels": list(self.rosters.get(series_id, [])), "next_url": None}


class TestResolveSeriesIndices:
    async def test_fills_position_from_the_roster(self):
        """Roster position (1-based) is the chapter number."""
        client = _FakeClient({900: [_info(11), _info(12), _info(13)]})
        drafts = [_draft(12, series_id=900), _draft(11, series_id=900)]

        out = await resolve_series_indices(drafts, client)

        assert [d.series_index for d in out] == [2, 1]

    async def test_does_not_overwrite_an_existing_index(self):
        """Fill-once: the resolver must not re-derive a number already stored.

        ``series_index`` is outside ``_upsert_rows``' overwrite whitelist, so a
        written value survives every refresh — including a correction the nightly
        repair made.  Re-resolving it here would let a second source win.
        """
        client = _FakeClient({901: [_info(21), _info(22)]})
        drafts = [_draft(21, series_id=901, series_index=7)]

        out = await resolve_series_indices(drafts, client)

        assert out[0].series_index == 7, "已有值不可被名册覆盖"
        assert client.calls == [], "无需补号时不该发请求"

    async def test_one_roster_fetch_per_series(self):
        """Several chapters of one series cost ONE request, not one each."""
        client = _FakeClient({902: [_info(31), _info(32), _info(33)]})
        drafts = [
            _draft(31, series_id=902), _draft(32, series_id=902),
            _draft(33, series_id=902),
        ]

        out = await resolve_series_indices(drafts, client)

        assert client.calls == [902], "同系列只拉一次名册"
        assert [d.series_index for d in out] == [1, 2, 3]

    async def test_separate_series_each_get_their_own_roster(self):
        client = _FakeClient({
            903: [_info(41), _info(42)],
            904: [_info(51)],
        })
        drafts = [_draft(42, series_id=903), _draft(51, series_id=904)]

        out = await resolve_series_indices(drafts, client)

        assert sorted(client.calls) == [903, 904]
        assert [d.series_index for d in out] == [2, 1]

    async def test_roster_failure_leaves_null(self):
        """A failed fetch must never fail the download, nor invent a number."""
        client = _FakeClient({905: [_info(61)]}, fail={905})
        drafts = [_draft(61, series_id=905)]

        out = await resolve_series_indices(drafts, client)

        assert out[0].series_index is None
        assert out[0].id == 61, "draft 本身不该被丢掉"

    async def test_novel_absent_from_roster_stays_null(self):
        """Restricted/omitted works: no number is better than a false one."""
        client = _FakeClient({906: [_info(71)]})
        drafts = [_draft(72, series_id=906)]

        out = await resolve_series_indices(drafts, client)

        assert out[0].series_index is None

    async def test_drafts_without_series_are_untouched(self):
        client = _FakeClient({907: [_info(81)]})
        drafts = [_draft(80), _draft(81, series_id=907)]

        out = await resolve_series_indices(drafts, client)

        assert out[0].series_index is None
        assert out[0] is drafts[0], "无关 draft 应原样返回（同一对象）"
        assert out[1].series_index == 1

    async def test_no_client_is_a_no_op(self):
        drafts = [_draft(91, series_id=908)]
        out = await resolve_series_indices(drafts, None)
        assert out[0].series_index is None

    async def test_empty_roster_is_a_no_op(self):
        client = _FakeClient({909: []})
        drafts = [_draft(92, series_id=909)]

        out = await resolve_series_indices(drafts, client)

        assert out[0].series_index is None

    async def test_id_order_is_not_used(self):
        """Regression guard for the 6.3 % bug.

        The roster lists 102 (chapter 1) before 101 (chapter 2) — an author who
        published chapter 2 first and backfilled chapter 1.  Sorting by id would
        invert these; the roster's order must win.
        """
        client = _FakeClient({910: [_info(102), _info(101)]})
        drafts = [_draft(101, series_id=910), _draft(102, series_id=910)]

        out = await resolve_series_indices(drafts, client)

        by_id = {d.id: d.series_index for d in out}
        assert by_id == {102: 1, 101: 2}, "名册顺序优先，不按 id 排"
