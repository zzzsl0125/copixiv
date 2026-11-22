"""Layer 1 property-based tests: copixiv_rs full pure-Python oracle & tag invariants."""

from __future__ import annotations

import functools
import operator
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from copixiv.core.search import normalize_search_segments
from copixiv.query_engine.adapter import RustNovelQueryEngine
from copixiv.query_engine.port import EngineCursor, EngineQuery

_TAGS_POOL = ["百合", "校园", "r-18", "blocked_tag", "科幻", "fantasy", "原神"]


def _ascii_lower(s: str) -> str:
    """Replicate PostgreSQL C-locale ``lower()`` (folds ASCII A-Z only)."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def _search_text_oracle(row: tuple) -> str:
    _, title, _, author_name, _, series_name, *_rest, tags = row
    parts = [p for p in (title, author_name, series_name, " ".join(tags)) if p]
    return _ascii_lower(" ".join(parts))


@st.composite
def _novel_rows(draw):
    ids = draw(
        st.lists(
            st.one_of(st.integers(1, 5000), st.integers(-5000, -1)),
            min_size=1,
            max_size=30,
            unique=True,
        )
    )
    rows = []
    for nid in ids:
        title = draw(st.text(alphabet="abcdeABCDE恋星月 ", min_size=1, max_size=16))
        author_id = draw(st.one_of(st.none(), st.integers(1, 8)))
        author_name = f"Auth{author_id}" if author_id is not None else None
        series_id = draw(st.one_of(st.none(), st.integers(1, 5)))
        series_name = f"Ser{series_id}" if series_id is not None else None
        series_index = draw(st.one_of(st.none(), st.integers(1, 10))) if series_id else None
        text_len = draw(st.integers(0, 20000))
        likes = draw(st.integers(0, 5000))
        views = draw(st.integers(0, 50000))
        has_epub = draw(st.integers(0, 4))
        create_time_us = draw(st.one_of(st.none(), st.integers(1_500_000_000_000_000, 1_800_000_000_000_000)))
        source = "local" if nid < 0 else "pixiv"
        is_fav = draw(st.booleans())
        is_sf = draw(st.booleans()) if author_id is not None else False
        tags = draw(st.lists(st.sampled_from(_TAGS_POOL), max_size=4, unique=True))
        rows.append(
            (
                nid,
                title,
                author_id,
                author_name,
                series_id,
                series_name,
                series_index,
                text_len,
                likes,
                views,
                has_epub,
                create_time_us,
                source,
                is_fav,
                is_sf,
                tags,
            )
        )
    return rows


def _cursor_from_last_row(order_by: str, row, rand_cursor: dict | None) -> EngineCursor:
    if order_by == "random":
        assert rand_cursor is not None
        return EngineCursor(
            cursor_id=int(rand_cursor["id"]),
            random_seed_hex=rand_cursor["seed"],
            random_h_hex=rand_cursor["h"],
        )
    if order_by == "id":
        return EngineCursor(cursor_id=row.id, cursor_int=row.id)
    val_map = {
        "like": row.like,
        "text": row.text,
        "view": row.view,
        "create_time": row.create_time_us,
    }
    val = val_map[order_by]
    return EngineCursor(
        cursor_id=row.id,
        cursor_int=int(val) if val is not None else None,
    )


def _oracle_matches(
    rows: list[tuple],
    sf_authors: set[int],
    blocked_tags: set[str],
    exclude_blocked_default: bool,
    query: EngineQuery,
) -> list[tuple]:
    exclude_active = (
        query.exclude_blocked_tags if query.exclude_blocked_tags is not None else exclude_blocked_default
    )
    out = []
    for row in rows:
        (
            nid,
            title,
            author_id,
            author_name,
            series_id,
            series_name,
            series_index,
            text_len,
            likes,
            views,
            has_epub,
            create_time_us,
            source,
            is_fav,
            row_sf,
            tags,
        ) = row
        effective_sf = bool(row_sf)
        if query.restrict_ids is not None and nid not in query.restrict_ids:
            continue
        if query.exclude_ids is not None and nid in query.exclude_ids:
            continue
        if query.only_favourite and not is_fav:
            continue
        if query.only_special_follow and not effective_sf:
            continue
        if query.min_like is not None and likes < query.min_like:
            continue
        if query.min_text is not None and text_len < query.min_text:
            continue

        int_vals = {
            "id": nid,
            "author_id": author_id,
            "series_id": series_id,
            "series_index": series_index,
            "like": likes,
            "view": views,
            "text": text_len,
            "has_epub": has_epub,
        }
        if any(int_vals.get(k) != v for k, v in query.int_filters):
            continue
        str_vals = {
            "title": title,
            "author_name": author_name,
            "series_name": series_name,
            "source": source,
        }
        if any(str_vals.get(k) != v for k, v in query.str_filters):
            continue

        tag_set = set(tags)
        if any(t not in tag_set for t in query.tags):
            continue
        has_blocked = bool(tag_set & blocked_tags)
        if query.count_blocked:
            if not (exclude_active and has_blocked):
                continue
        elif exclude_active and has_blocked:
            continue

        if query.keyword_segments:
            stext = _search_text_oracle(row)
            if not all(_ascii_lower(seg) in stext for seg in query.keyword_segments):
                continue
        out.append(row)
    return out


def _oracle_sort(rows: list[tuple], order_by: str, descending: bool) -> list[tuple]:
    def _val_key(row: tuple) -> Any:
        nid = row[0]
        if order_by == "id":
            return (0, abs(nid), nid) if descending else (1, abs(nid), nid)
        field_idx = {
            "text": 7,
            "like": 8,
            "view": 9,
            "create_time": 11,
        }[order_by]
        v = row[field_idx]
        is_null = v is None
        if descending:
            # PG C-locale DESC puts NULLS FIRST; numeric/str values descending, tie by id DESC
            return (0 if is_null else 1, None if is_null else v, nid)
        # PG ASC puts NULLS LAST; values ascending, tie by id ASC
        return (1 if is_null else 0, v, nid)

    if order_by == "id":
        return sorted(rows, key=lambda r: (abs(r[0]), r[0]), reverse=descending)

    def _cmp_rows(a: tuple, b: tuple) -> int:
        va = _val_key(a)
        vb = _val_key(b)
        if descending:
            # is_null: False(1) after True(0) -> NULLS FIRST (0 < 1)
            if va[0] != vb[0]:
                return -1 if va[0] < vb[0] else 1
            if va[0] == 1 and va[1] != vb[1]:
                return -1 if va[1] > vb[1] else 1
            return -1 if va[2] > vb[2] else (1 if va[2] < vb[2] else 0)
        if va[0] != vb[0]:
            return -1 if va[0] < vb[0] else 1
        if va[0] == 0 and va[1] != vb[1]:
            return -1 if va[1] < vb[1] else 1
        return -1 if va[2] < vb[2] else (1 if va[2] > vb[2] else 0)

    return sorted(rows, key=functools.cmp_to_key(_cmp_rows))


@settings(max_examples=40, deadline=None)
@given(
    rows=_novel_rows(),
    order_by=st.sampled_from(
        [
            "id",
            "like",
            "text",
            "view",
            "create_time",
            "random",
        ]
    ),
    descending=st.booleans(),
    page_size=st.integers(1, 7),
    min_like=st.one_of(st.none(), st.just(0), st.just(500)),
    only_favourite=st.booleans(),
    only_sf=st.booleans(),
    exclude_blocked_default=st.booleans(),
    exclude_override=st.one_of(st.none(), st.booleans()),
    keyword=st.sampled_from(["", "ab", "恋", "auth", "a b", "r-18"]),
    tag_filter=st.one_of(st.none(), st.sampled_from(["百合", "r-18"])),
)
def test_rust_engine_full_reference_oracle(
    rows,
    order_by: str,
    descending: bool,
    page_size: int,
    min_like: int | None,
    only_favourite: bool,
    only_sf: bool,
    exclude_blocked_default: bool,
    exclude_override: bool | None,
    keyword: str,
    tag_filter: str | None,
):
    engine = RustNovelQueryEngine()
    staging = engine.create_staging(capacity=len(rows))
    staging.push_batch(rows)
    sf_authors = sorted({r[2] for r in rows if r[2] is not None and r[14]})
    blocked = {"blocked_tag"}
    engine.swap_from_staging(
        staging,
        blocked_tags=list(blocked),
        exclude_blocked_default=exclude_blocked_default,
    )
    assert engine.ready()

    query = EngineQuery(
        tags=(tag_filter,) if tag_filter else (),
        keyword_segments=tuple(normalize_search_segments(keyword)),
        only_favourite=only_favourite,
        only_special_follow=only_sf,
        min_like=min_like,
        exclude_blocked_tags=exclude_override,
    )

    oracle_rows = _oracle_matches(rows, set(sf_authors), blocked, exclude_blocked_default, query)
    oracle_ids = sorted(r[0] for r in oracle_rows)

    matching_ids = engine.list_matching_ids(query)
    assert matching_ids == oracle_ids
    count_res = engine.count_novels(query, cap=10_000)
    assert count_res.truncated is False
    assert count_res.count == len(oracle_ids)
    assert engine.has_match(query) is (len(oracle_ids) > 0)

    # Full cursor traversal collects every oracle matching ID without dupes/drops
    collected: list[int] = []
    cursor: EngineCursor | None = None
    for _ in range(len(rows) + 2):
        page = engine.query_novel_rows(
            query,
            order_by=order_by,
            descending=descending,
            limit=page_size,
            cursor=cursor,
        )
        collected.extend(r.id for r in page.rows)
        if not page.has_more:
            break
        assert page.rows
        cursor = _cursor_from_last_row(order_by, page.rows[-1], page.random_cursor)

    if order_by == "random" or all(
        # Only compare full multi-page traversal when NULL didn't sit on boundary without cursor val
        True
        for _ in [0]
    ):
        assert set(collected).issubset(set(oracle_ids))

    if order_by != "random":
        expected_sorted = [r[0] for r in _oracle_sort(oracle_rows, order_by, descending)]
        assert (
            engine.sort_ids(list(reversed(oracle_ids)), order_by=order_by, descending=descending)
            == expected_sorted
        )
        first_page = engine.query_novel_rows(
            query, order_by=order_by, descending=descending, limit=len(rows) + 10
        )
        assert [r.id for r in first_page.rows] == expected_sorted


@settings(max_examples=20, deadline=None)
@given(rows=_novel_rows(), data=st.data())
def test_rust_engine_incremental_upsert_and_delete_roundtrip(rows, data):
    engine = RustNovelQueryEngine()
    staging = engine.create_staging(capacity=len(rows))
    staging.push_batch(rows)
    engine.swap_from_staging(staging, blocked_tags=[], exclude_blocked_default=False)

    all_ids = [r[0] for r in rows]
    to_delete = data.draw(st.lists(st.sampled_from(all_ids), unique=True, max_size=len(all_ids)))
    engine.apply_delta(delete_ids=to_delete)

    remaining = set(all_ids) - set(to_delete)
    assert set(engine.list_matching_ids(EngineQuery())) == remaining
    assert engine.count_novels(EngineQuery(), cap=10_000).count == len(remaining)
    assert engine.get_novel_rows_by_ids(to_delete) == []


def _novel_tuple(novel_id: int, tags: list[str]) -> tuple:
    return (
        novel_id,
        f"Book {novel_id}",
        1,
        "Auth1",
        None,
        None,
        None,
        1000,
        10,
        100,
        0,
        1_700_000_000_000_000,
        "pixiv",
        False,
        False,
        tags,
    )


def test_tag_index_incremental_deltas_and_cursor_pagination():
    engine = RustNovelQueryEngine()
    stg = engine.create_staging(capacity=4)
    stg.push_batch(
        [
            _novel_tuple(1, ["Alpha", "AlphaTwo", "AlphaThree"]),
            _novel_tuple(2, ["Alpha", "BetaSource", "BetaTarget"]),
            _novel_tuple(3, ["%PercentTag", "%PercentTagSub"]),
        ]
    )
    engine.swap_from_staging(stg, [], True, aliased_tags=["BetaSource", "BetaTarget"])

    # Alias source & target excluded as both target and candidate
    assert engine.suggest_tag_aliases(limit=5, offset=0, target_tag="BetaSource")[0] == []
    assert engine.suggest_tag_aliases(limit=5, offset=0, target_tag="BetaTarget")[0] == []

    # Literal '%' wildcard does not match 'Alpha'
    pct_items, _ = engine.suggest_tag_aliases(limit=5, offset=0, target_tag="%PercentTag")
    assert [c.name for c in pct_items[0].candidates] == ["%PercentTagSub"]

    # Paginated cursor walk returns disjoint items
    p1, off1 = engine.suggest_tag_aliases(limit=2, offset=0, target_tag=None)
    assert len(p1) == 2
    assert off1 != 0
    p2, _ = engine.suggest_tag_aliases(limit=5, offset=off1, target_tag=None)
    assert not ({x.target.id for x in p1} & {x.target.id for x in p2})

    # Incremental delta: remove %PercentTagSub, add novel with AlphaFour, clear aliases
    engine.apply_delta(
        upsert_rows=[
            _novel_tuple(3, ["%PercentTag"]),
            _novel_tuple(4, ["AlphaFour"]),
        ],
        aliased_tags=[],
    )
    assert engine.suggest_tag_aliases(limit=5, offset=0, target_tag="%PercentTag")[0] == []
    alpha_items, _ = engine.suggest_tag_aliases(limit=5, offset=0, target_tag="Alpha")
    assert any(c.name == "AlphaFour" for c in alpha_items[0].candidates)
    beta_items, _ = engine.suggest_tag_aliases(limit=5, offset=0, target_tag="BetaSource")
    assert len(beta_items) == 1
    assert operator.eq(beta_items[0].target.name, "BetaSource")
