#!/usr/bin/env python3
"""Consolidated query-performance verification for copixiv-v2.

Run from the project root:

    source .venv/bin/activate
    python scripts/benchmark_query_performance.py

The script uses the real production-sized database (database/database.db)
and measures, for each previously identified bottleneck, the current
implementation vs a representative proposed implementation.

Sections:
  A. Current list-query matrix
  B. Current count-query matrix
  C. Per-change before/after verification
     C1. P0: selective tag/keyword list: EXISTS (current) vs IN (proposed)
     C2. P1: special-follow count: EXISTS (current) vs IN/JOIN (proposed)
     C3. P2: popular-tag count: IN subquery (current) vs JOIN (proposed)
     C4. P3: temp_store default vs MEMORY for large IN subqueries
     C5. P4: author/series + min_like=0: current vs no-threshold
     C6. P5: tag-suggest current cost and an experimental prefix-only variant
"""

from __future__ import annotations

import asyncio
import signal
import statistics
import sys
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copixiv.db.engine import create_database_engine  # noqa: E402
from copixiv.db import models  # noqa: E402
from copixiv.features.novels.repo import SQLAlchemyNovelRepository  # noqa: E402
from copixiv.features.novels.repo import NovelQueryBuilder  # noqa: E402

DB_PATH = str(ROOT / "database" / "database.db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Timeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise Timeout("timed out")


def run_with_timeout(fn, seconds: float):
    """Run fn in the current thread; raise Timeout after `seconds`."""
    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def make_session(temp_store: str | None = None) -> Session:
    engine = create_database_engine(DB_PATH)
    s = Session(bind=engine)
    if temp_store is not None:
        s.execute(text(f"PRAGMA temp_store={temp_store}"))
    return s


def bench(fn, n: int = 3):
    """Return (min_ms, median_ms, last_result)."""
    samples = []
    result = None
    for _ in range(n):
        t = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t) * 1000)
    return min(samples), statistics.median(samples), result


def fmt(ms: float | None) -> str:
    if ms is None:
        return "N/A"
    return f"{ms:8.1f}"


def print_table(title: str, rows: list[tuple[str, float | None, float | None]]):
    print(f"\n{title}")
    print(f"{'场景':<34}{'min(ms)':>10}{'median(ms)':>12}")
    for name, mn, md in rows:
        print(f"{name:<34}{fmt(mn):>10}{fmt(md):>12}")


def build_list_query(repo, queries, order_by="like", direction="DESC",
                     limit=31, min_like=0, min_text=0, mode="adaptive"):
    """Build a list query with a specific tag/FTS filter strategy.

    mode:
      - "adaptive": current implementation (IN for rare, EXISTS for popular)
      - "exists":   legacy forced-EXISTS behaviour
      - "in":       forced IN-subquery behaviour
    """
    params = dict(
        queries=queries,
        order_by=order_by,
        order_direction=direction,
        cursor=None,
        per_page=limit,
        min_like=min_like,
        min_text=min_text,
    )
    b = NovelQueryBuilder(repo, **params)
    tags, keywords, field_filters = b._categorize(queries)
    main = b._base_select(
        skip_favourite_join="is_favourite" in field_filters,
        skip_special_follow_join="is_special_follow" in field_filters,
    )
    main = b._join_field_filter_tables(main, field_filters)
    if mode == "in":
        main = b._where_tag_filter(main, tags, use_exists=False)
        main = b._where_fts_filter(main, keywords, use_exists=False)
    elif mode == "exists":
        main = b._where_tag_filter(main, tags, use_exists=True, adaptive=False)
        main = b._where_fts_filter(main, keywords, use_exists=True, adaptive=False)
    else:  # adaptive
        main = b._where_tag_filter(main, tags, use_exists=True)
        main = b._where_fts_filter(main, keywords, use_exists=True)
    main = b._where_field_filters(main, field_filters)
    main = b._where_thresholds(main)
    main = b._apply_ordering(main, order_by, direction)
    main = b._apply_limit(main, limit)
    return main


# ---------------------------------------------------------------------------
# Data / session setup
# ---------------------------------------------------------------------------

print("=" * 78)
print("copixiv-v2 查询性能验证（真实数据库）")
print(f"数据库: {DB_PATH}")
print("=" * 78)

s = make_session("MEMORY")
repo = SQLAlchemyNovelRepository(s)

# warm up shared caches (SQLite pages, FTS, gram)
repo._get_novels_sync(order_by="random", per_page=30, min_like=500, min_text=3000)
repo._get_novels_sync(queries={"恋": "keyword"}, order_by="like",
                      order_direction="DESC", per_page=30, min_like=0, min_text=0)
repo._count_novels_sync(queries={"R-18": "tags"}, min_like=0, min_text=0)
repo._count_novels_sync(queries={"1": "is_special_follow"}, min_like=0, min_text=0)

# ---------------------------------------------------------------------------
# A. Current list-query matrix
# ---------------------------------------------------------------------------

print_table("A. 当前列表查询（warm）", [
    ("列表-随机默认(500/3000)",
     *bench(lambda: repo._get_novels_sync(order_by="random", per_page=30,
                                          min_like=500, min_text=3000))[:2]),
    ("列表-点赞排序默认",
     *bench(lambda: repo._get_novels_sync(order_by="like", order_direction="DESC",
                                          per_page=30, min_like=500, min_text=3000))[:2]),
    ("列表-ID排序",
     *bench(lambda: repo._get_novels_sync(order_by="id", order_direction="DESC",
                                          per_page=30, min_like=None, min_text=None))[:2]),
    ("列表-标签NTR+点赞",
     *bench(lambda: repo._get_novels_sync(queries={"NTR": "tags"}, order_by="like",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-标签R-18+点赞",
     *bench(lambda: repo._get_novels_sync(queries={"R-18": "tags"}, order_by="like",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-稀有标签+点赞",
     *bench(lambda: repo._get_novels_sync(queries={"伊理戸結女": "tags"}, order_by="like",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-关键词恋+点赞",
     *bench(lambda: repo._get_novels_sync(queries={"恋": "keyword"}, order_by="like",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-关键词R-18+点赞",
     *bench(lambda: repo._get_novels_sync(queries={"R-18": "keyword"}, order_by="like",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-作者+ID",
     *bench(lambda: repo._get_novels_sync(queries={"100770156": "author_id"}, order_by="id",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-系列+ID",
     *bench(lambda: repo._get_novels_sync(queries={"11250666": "series_id"}, order_by="id",
                                          order_direction="ASC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-收藏+ID",
     *bench(lambda: repo._get_novels_sync(queries={"1": "is_favourite"}, order_by="id",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
    ("列表-特别关注+ID",
     *bench(lambda: repo._get_novels_sync(queries={"1": "is_special_follow"}, order_by="id",
                                          order_direction="DESC", per_page=30,
                                          min_like=0, min_text=0))[:2]),
])

# ---------------------------------------------------------------------------
# B. Current count-query matrix
# ---------------------------------------------------------------------------

print_table("B. 当前计数查询（warm）", [
    ("计数-默认阈值",
     *bench(lambda: repo._count_novels_sync(min_like=500, min_text=3000))[:2]),
    ("计数-标签NTR",
     *bench(lambda: repo._count_novels_sync(queries={"NTR": "tags"}, min_like=0,
                                            min_text=0))[:2]),
    ("计数-标签R-18",
     *bench(lambda: repo._count_novels_sync(queries={"R-18": "tags"}, min_like=0,
                                            min_text=0))[:2]),
    ("计数-关键词恋",
     *bench(lambda: repo._count_novels_sync(queries={"恋": "keyword"}, min_like=0,
                                            min_text=0))[:2]),
    ("计数-特别关注",
     *bench(lambda: repo._count_novels_sync(queries={"1": "is_special_follow"},
                                            min_like=0, min_text=0))[:2]),
])

# ---------------------------------------------------------------------------
# C. Per-change verification
# ---------------------------------------------------------------------------

print("\n" + "=" * 78)
print("C. 待改动项逐一验证")
print("=" * 78)

# --- C1: P0 selective list EXISTS vs adaptive vs IN ------------------------

print("\nC1. P0 低频标签/关键词列表：旧EXISTS vs 新自适应 vs 纯IN")
print("    场景均为 ORDER BY like DESC LIMIT 30")

c1_cases = [
    ("稀有标签", {"伊理戸結女": "tags"}),
    ("关键词恋(少量命中)", {"恋": "keyword"}),
    ("关键词R-18(大量命中)", {"R-18": "keyword"}),
    ("不存在关键词(零命中)", {"不存在词xyz": "keyword"}),
]

for label, queries in c1_cases:
    results = {}
    for mode in ("exists", "adaptive", "in"):
        try:
            stmt = build_list_query(repo, queries, mode=mode)
            if mode == "exists" and label == "不存在关键词(零命中)":
                mn, _, _ = bench(
                    lambda: run_with_timeout(
                        lambda: s.execute(stmt).fetchall(), 8.0,
                    ),
                    n=1,
                )
            else:
                mn, _, _ = bench(lambda: s.execute(stmt).fetchall(), n=3)
            results[mode] = mn
        except Timeout:
            results[mode] = None
        except Exception as e:
            results[mode] = None
            print(f"    [{mode}] 错误: {e.__class__.__name__}: {e}")

    def fmt_ms(v):
        return f"{v:8.1f}" if v is not None else f"{'超时':>8}"

    print(f"  {label:<24} 旧EXISTS={fmt_ms(results.get('exists'))}ms  "
          f"自适应={fmt_ms(results.get('adaptive'))}ms  "
          f"纯IN={fmt_ms(results.get('in'))}ms")

# --- C2: P1 special-follow count ------------------------------------------

print("\nC2. P1 特别关注计数：旧EXISTS vs 当前实现（IN） vs 原始 IN/JOIN")

sql_sf_exists = """
SELECT count(*) FROM novel
WHERE EXISTS (SELECT 1 FROM special_follow
              WHERE special_follow.author_id = novel.author_id)
  AND novel.like >= 0 AND novel.text >= 0
"""
sql_sf_in = """
SELECT count(*) FROM novel
WHERE author_id IN (SELECT author_id FROM special_follow)
  AND novel.like >= 0 AND novel.text >= 0
"""
sql_sf_join = """
SELECT count(*) FROM novel
JOIN special_follow ON novel.author_id = special_follow.author_id
WHERE novel.like >= 0 AND novel.text >= 0
"""

c2_old_exists, c2_old_exists_med, _ = bench(
    lambda: s.execute(text(sql_sf_exists)).scalar())
c2_current, c2_current_med, _ = bench(lambda: repo._count_novels_sync(
    queries={"1": "is_special_follow"}, min_like=0, min_text=0))
c2_in, c2_in_med, _ = bench(lambda: s.execute(text(sql_sf_in)).scalar())
c2_join, c2_join_med, _ = bench(lambda: s.execute(text(sql_sf_join)).scalar())
print(f"  旧 EXISTS      : min={c2_old_exists:8.1f}ms median={c2_old_exists_med:8.1f}ms")
print(f"  当前实现(IN)    : min={c2_current:8.1f}ms median={c2_current_med:8.1f}ms")
print(f"  原始 IN        : min={c2_in:8.1f}ms median={c2_in_med:8.1f}ms")
print(f"  原始 JOIN      : min={c2_join:8.1f}ms median={c2_join_med:8.1f}ms")

# --- C3: P2 popular-tag count ---------------------------------------------

print("\nC3. P2 大标签计数：旧IN子查询 vs 当前实现（JOIN） vs 原始 JOIN")

sql_tag_in = """
SELECT count(*) FROM novel
WHERE novel.id IN (
  SELECT novel_tag.novel_id
  FROM novel_tag JOIN tag ON novel_tag.tag_id = tag.id
  WHERE tag.name = 'R-18'
) AND novel.like >= 0 AND novel.text >= 0
"""
sql_tag_join = """
SELECT count(*) FROM novel
JOIN novel_tag ON novel.id = novel_tag.novel_id
JOIN tag ON novel_tag.tag_id = tag.id
WHERE tag.name = 'R-18'
  AND novel.like >= 0 AND novel.text >= 0
"""

c3_old_in, c3_old_in_med, _ = bench(
    lambda: s.execute(text(sql_tag_in)).scalar())
c3_current, c3_current_med, _ = bench(lambda: repo._count_novels_sync(
    queries={"R-18": "tags"}, min_like=0, min_text=0))
c3_join, c3_join_med, _ = bench(lambda: s.execute(text(sql_tag_join)).scalar())
print(f"  旧 IN        : min={c3_old_in:8.1f}ms median={c3_old_in_med:8.1f}ms")
print(f"  当前实现(JOIN): min={c3_current:8.1f}ms median={c3_current_med:8.1f}ms")
print(f"  原始 JOIN    : min={c3_join:8.1f}ms median={c3_join_med:8.1f}ms")

# --- C4: P3 temp_store default vs MEMORY -----------------------------------

print("\nC4. P3 temp_store：默认 vs MEMORY（大 IN 子查询）")
print("     验证查询：SELECT count(*) ... id IN (SELECT novel_id FROM novel_tag WHERE tag_id=R-18)")

s_default = make_session("0")   # 0 = SQLite 编译默认（当前引擎未设置）
s_memory = make_session("MEMORY")

sql_big_in = """
SELECT count(*) FROM novel
WHERE novel.id IN (
  SELECT novel_id FROM novel_tag WHERE tag_id = 8
) AND novel.like >= 0 AND novel.text >= 0
"""

# warm both
try:
    s_default.execute(text(sql_big_in)).scalar()
except Exception:
    pass
s_memory.execute(text(sql_big_in)).scalar()

def run_default():
    return s_default.execute(text(sql_big_in)).scalar()

def run_memory():
    return s_memory.execute(text(sql_big_in)).scalar()

try:
    c4_default, c4_default_med, _ = bench(run_default, n=1)
    print(f"  默认 temp_store : min={c4_default:8.1f}ms（成功）")
except Exception as e:
    print(f"  默认 temp_store : 失败/错误（{e.__class__.__name__}: {e}）")

c4_memory, c4_memory_med, _ = bench(run_memory, n=3)
print(f"  MEMORY temp_store: min={c4_memory:8.1f}ms median={c4_memory_med:8.1f}ms（成功）")

# --- C5: P4 author/series + min_like=0 -------------------------------------

print("\nC5. P4 作者/系列排序：旧SQL(>=0) vs 当前实现(0视为无阈值)")

c5_cases = [
    ("作者+ID降序",
     "SELECT id FROM novel WHERE author_id = 100770156 AND like >= 0 AND text >= 0 ORDER BY id DESC LIMIT 30",
     "SELECT id FROM novel WHERE author_id = 100770156 ORDER BY id DESC LIMIT 30"),
    ("系列+ID升序",
     "SELECT id FROM novel WHERE series_id = 11250666 AND like >= 0 AND text >= 0 ORDER BY id ASC LIMIT 30",
     "SELECT id FROM novel WHERE series_id = 11250666 ORDER BY id ASC LIMIT 30"),
]
for label, old_sql, new_sql in c5_cases:
    old_ms, old_med, _ = bench(lambda: s.execute(text(old_sql)).fetchall())
    new_ms, new_med, _ = bench(lambda: s.execute(text(new_sql)).fetchall())
    print(f"  {label:<24} 旧(>=0) min={old_ms:7.1f}ms  当前(无阈值) min={new_ms:7.1f}ms")

# --- C6: P5 tag suggest -----------------------------------------------------

print("\nC6. P5 标签建议：当前实现 vs 实验性前缀-only")
from copixiv.features.tags.repo import SQLAlchemyTagRepository  # noqa: E402
tag_repo = SQLAlchemyTagRepository(s)

def _suggest_aliases():
    return asyncio.run(tag_repo.suggest_aliases(limit=5, offset=0))


c6_current, c6_current_med, _ = bench(_suggest_aliases, n=3)

# 实验性前缀-only：仅测 SQL 形状，不代表最终实现
sql_prefix_only = """
SELECT id, name, reference_count
FROM tag
WHERE name LIKE 'R%'
  AND id != 8
ORDER BY reference_count DESC
LIMIT 50
"""
c6_prefix, c6_prefix_med, _ = bench(lambda: s.execute(text(sql_prefix_only)).fetchall(), n=3)
print(f"  当前 suggest_aliases(limit=5) : min={c6_current:7.1f}ms median={c6_current_med:7.1f}ms")
print(f"  实验性前缀-only LIKE 'R%'     : min={c6_prefix:7.1f}ms median={c6_prefix_med:7.1f}ms")
print("  （前缀-only 会改变语义，仅用于量化全表 %..% 扫描的成本）")

# ---------------------------------------------------------------------------
s.close()
s_default.close()
s_memory.close()
print("\n完成。")
