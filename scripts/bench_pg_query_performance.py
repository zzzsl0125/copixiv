#!/usr/bin/env python3
"""PostgreSQL query-performance verification for copixiv v2 (post-migration).

Successor to the SQLite-era ``benchmark_query_performance.py`` / ``bench_fts_cost.py``
— those are dead after the SQLite → PG migration (new schema ``novel.tags text[]``,
``novel_search`` derived table, ``QuerySpec``-based builder).  This script
measures what the *user* actually pays for on the migrated stack:

  * ``check``  — environment/data sanity (row counts, indexes, ANALYZE state,
                 PG runtime settings, blocked-tag config).  Run this first.
  * ``bench``  — warm query matrix: list / count / keyword / pagination, via the
                 real repository methods (``_get_novels_sync`` /
                 ``_count_novels_sync``) on the production database.
  * ``cold``   — cold-start measurement: stops/starts the local PG instance
                 (``scripts/pg_dev.py``), then times the *first* query of each
                 scenario from a brand-new engine (new process pool), plus
                 EXPLAIN (ANALYZE, BUFFERS) shared-read evidence before/after.
  * ``explain``— EXPLAIN (ANALYZE, BUFFERS, COSTS) for representative queries.

Usage (from the project root, with the venv active)::

    python scripts/bench_pg_query_performance.py check
    python scripts/bench_pg_query_performance.py bench [--n 5] [--url URL]
    python scripts/bench_pg_query_performance.py cold [--n 5] [--url URL]
    python scripts/bench_pg_query_performance.py explain [--url URL]

``--url`` defaults to ``postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv``
(the app default).  ``bench`` is read-only (SELECT/EXPLAIN only); ``cold``
executes ``scripts/pg_dev.py stop`` + ``start`` (data is preserved).

The ``cold`` command restarts the database, so do not point it at a server
other processes depend on without warning.
"""

from __future__ import annotations

import argparse
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select, func, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from copixiv.core.services import QuerySpec, parse_search_keyword  # noqa: E402
from copixiv.db import models  # noqa: E402
from copixiv.db.engine import create_database_engine  # noqa: E402
from copixiv.features.novels import repo as novels_repo_module  # noqa: E402
from copixiv.features.novels.repo import (  # noqa: E402
    SQLAlchemyNovelReadRepository,
    SQLAlchemyNovelRepository,
)

DEFAULT_URL = "postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv"
PG_DEV = ROOT / "scripts" / "pg_dev.py"

# Scenario constants matching the old SQLite benchmark / the front-end UX.
PER_PAGE = 30
MIN_LIKE_DEFAULT = 500
MIN_TEXT_DEFAULT = 3000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Timeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise Timeout("timed out")


def run_with_timeout(fn, seconds: float = 20.0):
    """Run fn in the current thread; raise Timeout after *seconds*."""
    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def make_session(url: str) -> tuple[object, Session]:
    engine = create_database_engine(url)
    return engine, Session(bind=engine)


def bench(fn, n: int = 5, timeout: float = 30.0, session=None):
    """Return (min_ms, median_ms, samples_ms).

    Clears the count cache before each sample so every count scenario pays
    full SQL cost (the in-process epoch cache would otherwise turn repeats
    into O(1) dict hits and the benchmark would measure nothing).

    A timed-out sample records ``None``; if *session* is given the aborted
    transaction is rolled back so subsequent samples on the same session stay
    usable (a DB error from a dead transaction would otherwise cascade).
    """
    samples: list[float | None] = []
    for _ in range(n):
        novels_repo_module._count_cache.clear()
        t = time.perf_counter()
        try:
            run_with_timeout(fn, timeout)
            samples.append((time.perf_counter() - t) * 1000)
        except Timeout:
            samples.append(None)
            if session is not None:
                session.rollback()
        except Exception as exc:
            samples.append(None)
            if session is not None:
                session.rollback()
            print(f"  [bench] 样本错误: {exc.__class__.__name__}: {exc}")
    valid = [s for s in samples if s is not None]
    if not valid:
        return None, None, samples
    return min(valid), statistics.median(valid), samples


def fmt(ms: float | None, width: int = 11) -> str:
    if ms is None:
        return f"{'timeout':>{width}}"
    return f"{ms:>{width}.1f}"


def print_table(title: str, rows: list[tuple[str, float | None, float | None]],
                extra_header: str = "") -> None:
    print(f"\n{title}")
    print(f"{'场景':<40}{'min(ms)':>11}{'median(ms)':>12}  {extra_header}")
    for name, mn, md in rows:
        print(f"{name:<40}{fmt(mn):>11}{fmt(md):>12}")


# ---------------------------------------------------------------------------
# QuerySpec helpers
# ---------------------------------------------------------------------------

def spec(
    queries: str = "",
    *,
    order_by: str = "like",
    order_direction: str = "DESC",
    per_page: int = PER_PAGE,
    min_like: int | None = MIN_LIKE_DEFAULT,
    min_text: int | None = MIN_TEXT_DEFAULT,
    cursor: dict | None = None,
    exclude_blocked_tags: bool | None = None,
) -> QuerySpec:
    conditions = parse_search_keyword(queries) if queries else []
    return QuerySpec(
        conditions=conditions,
        order_by=order_by,
        order_direction=order_direction,
        cursor=cursor,
        per_page=per_page,
        min_like=min_like,
        min_text=min_text,
        exclude_blocked_tags=exclude_blocked_tags,
    )


# ---------------------------------------------------------------------------
# check — environment & data sanity
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    engine, session = make_session(args.url)
    q = session.execute
    print("=" * 78)
    print("copixiv PG 环境核查")
    print(f"URL: {args.url}")
    print("=" * 78)

    try:
        for table in ("novel", "novel_search", "author", "series", "tag",
                      "tag_preference", "setting"):
            row = q(text(
                f"SELECT count(*) FROM {table}")).scalar()
            print(f"  rows {table:<16} {row}")
    except Exception as exc:
        print(f"  !! 计数失败: {exc.__class__.__name__}: {exc}")

    print("\n  -- PG 运行时设置 --")
    for setting in ("shared_buffers", "work_mem", "effective_cache_size",
                    "maintenance_work_mem", "max_connections",
                    "default_statistics_target", "max_parallel_workers_per_gather"):
        try:
            val = q(text(f"SHOW {setting}")).scalar()
            print(f"  {setting:<34} {val}")
        except Exception as exc:
            print(f"  {setting:<34} ERROR {exc}")

    print("\n  -- 索引清单（关键表） --")
    idx_rows = q(text(
        """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename IN ('novel', 'novel_search', 'tag', 'author')
        ORDER BY tablename, indexname
        """
    )).fetchall()
    for table, name, definition in idx_rows:
        print(f"  {table:<14} {name:<28} {definition}")

    print("\n  -- 统计信息/ANALYZE 状态 --")
    stat_rows = q(text(
        """
        SELECT relname, n_live_tup, last_analyze, last_autoanalyze,
               last_vacuum, last_autovacuum
        FROM pg_stat_user_tables
        WHERE relname IN ('novel', 'novel_search', 'tag', 'novel_tag')
        ORDER BY relname
        """
    )).fetchall()
    for relname, live, last_an, last_aan, last_v, last_av in stat_rows:
        print(f"  {relname:<14} live_tup={live!s:<10} analyze={last_an} "
              f"autoanalyze={last_aan} vacuum={last_v} autovacuum={last_av}")

    print("\n  -- blocked tags / runtime settings（影响默认查询形态） --")
    blocked = q(text(
        "SELECT tag FROM tag_preference WHERE preference = 'blocked'"
    )).scalars().all()
    print(f"  blocked tags: {len(blocked)} {sorted(blocked)[:10]}")
    setting_rows = q(text("SELECT key, value FROM setting")).fetchall()
    print(f"  settings: {dict(setting_rows) if setting_rows else '(empty)'}")

    fav = q(text("SELECT count(*) FROM novel WHERE is_favourite")).scalar()
    sfol = q(text(
        "SELECT count(*) FROM author WHERE is_special_follow")).scalar()
    tags_array = q(text(
        "SELECT count(*) FROM novel WHERE tags <> '{}'")).scalar()
    print(f"  is_favourite novels: {fav}   special-follow authors: {sfol}   "
          f"novels with tags: {tags_array}")

    gin = q(text(
        """
        SELECT indexrelname, idx_scan, idx_tup_read
        FROM pg_stat_user_indexes
        WHERE indexrelname IN ('ix_novel_tags_gin', 'novel_search_gin')
        """
    )).fetchall()
    print("  -- GIN 索引使用情况（累计扫描计数，零 = 从未被使用过） --")
    for name, scans, reads in gin:
        print(f"  {name:<20} scans={scans} tup_read={reads}")

    session.close()
    engine.dispose()
    print("\ncheck 完成。")
    return 0


# ---------------------------------------------------------------------------
# bench — warm query matrix
# ---------------------------------------------------------------------------

LIST_CASES = [
    ("列表-默认(500/3000)+like", dict(queries="", order_by="like")),
    ("列表-默认+关闭blocked排除", dict(queries="", order_by="like",
                                    exclude_blocked_tags=False)),
    ("列表-随机浏览(500/3000)", dict(queries="", order_by="random")),
    ("列表-ID排序(无阈值)", dict(queries="", order_by="id",
                              min_like=None, min_text=None)),
    ("列表-标签NTR+like", dict(queries="tags:NTR", order_by="like")),
    ("列表-标签R-18+like", dict(queries="tags:R-18", order_by="like")),
    ("列表-稀有标签+like", dict(queries="tags:伊理戸結女", order_by="like")),
    ("列表-关键词恋+like", dict(queries="keyword:恋", order_by="like")),
    ("列表-关键词R-18+like", dict(queries="keyword:R-18", order_by="like")),
    ("列表-关键词哈利波特+like", dict(queries="keyword:哈利波特", order_by="like")),
    ("列表-作者100770156+id", dict(queries="author_id:100770156",
                                  order_by="id")),
    ("列表-系列11250666+id", dict(queries="series_id:11250666", order_by="id",
                                 order_direction="ASC")),
    ("列表-收藏+id", dict(queries="is_favourite:1", order_by="id")),
    ("列表-特别关注+id", dict(queries="is_special_follow:1", order_by="id")),
    ("列表-组合 tag NTR+关键词恋", dict(queries="tags:NTR;keyword:恋",
                                    order_by="like")),
    ("列表-组合 tag NTR+关键词R-18", dict(queries="tags:NTR;keyword:R-18",
                                      order_by="like")),
]

COUNT_CASES = [
    ("计数-默认阈值(500/3000)", dict(queries="")),
    ("计数-标签NTR", dict(queries="tags:NTR")),
    ("计数-标签R-18", dict(queries="tags:R-18")),
    ("计数-关键词恋", dict(queries="keyword:恋")),
    ("计数-关键词R-18", dict(queries="keyword:R-18")),
    ("计数-关键词催眠", dict(queries="keyword:催眠")),
    ("计数-关键词哈利波特", dict(queries="keyword:哈利波特")),
    ("计数-特别关注", dict(queries="is_special_follow:1")),
    ("计数-组合 tag NTR+关键词恋", dict(queries="tags:NTR;keyword:恋")),
]

PAGE_DEPTHS = [1, 10, 50, 100, 500, 1000]


def _build_read_repo(session: Session):
    """Use the repository facade so we measure the real request path."""
    return SQLAlchemyNovelRepository(session)


def bench_lists(session: Session, n: int) -> list[tuple[str, float, float]]:
    repo = _build_read_repo(session)
    rows: list[tuple[str, float, float]] = []
    for label, kwargs in LIST_CASES:
        s = spec(**kwargs)
        mn, md, _ = bench(lambda: repo._get_novels_sync(s), n=n, session=session)
        rows.append((label, mn, md))
    return rows


def bench_counts(session: Session, n: int) -> list[tuple[str, float, float]]:
    repo = _build_read_repo(session)
    rows: list[tuple[str, float, float]] = []
    for label, kwargs in COUNT_CASES:
        s = spec(**kwargs, min_like=0, min_text=0)
        mn, md, _ = bench(lambda: repo._count_novels_sync(s), n=n, session=session)
        rows.append((label, mn, md))
    # excluded count (blocked-tag novels) — only meaningful when blocked exist
    try:
        mn, md, _ = bench(lambda: repo._count_excluded_novels_sync(
            spec(min_like=0, min_text=0)), n=n, session=session)
        rows.append(("计数-被排除(blocked)", mn, md))
    except Exception as exc:
        print(f"  [计数-被排除] 错误: {exc.__class__.__name__}: {exc}")
    return rows


def _paginate(repo, s: QuerySpec, depth: int, order_by: str):
    """Walk *depth* keyset pages; return (per_page_ms, cursor, pages_reached).

    The walk stops early when the last page is reached (cursor is None) —
    callers can compare *pages_reached* against *depth* to interpret the
    numbers (a dataset shorter than depth×per_page simply ends sooner).
    """
    curs = None
    per_times: list[float] = []
    reached = 0
    for _ in range(depth):
        page_spec = s.model_copy(update={"cursor": curs})
        t = time.perf_counter()
        result = repo._get_novels_sync(page_spec)
        per_times.append((time.perf_counter() - t) * 1000)
        curs = result["cursor"]
        reached += 1
        if curs is None:
            break
    return per_times, curs, reached


def bench_pagination(session: Session, n: int) -> list[tuple[str, float, float]]:
    repo = _build_read_repo(session)
    rows: list[tuple[str, float, float]] = []
    reach_map: dict[int, int] = {}
    base = spec(order_by="like")  # defaults: 500/3000 DESC
    for depth in PAGE_DEPTHS:
        # single-depth sweep, n times for min/median of per-page latency
        times: list[float] = []
        for _ in range(n):
            per_times, _curs, reached = _paginate(repo, base, depth, "like")
            times.append(min(per_times) if per_times else 0.0)
            reach_map[depth] = reached
        rows.append((f"翻页-like 第{depth}页(实达{reach_map[depth]})",
                     min(times), statistics.median(times)))

    # random browsing pages (shuffle cursor)
    rand_base = spec(order_by="random", per_page=30)
    for depth in (1, 10, 50):
        times: list[float] = []
        for _ in range(n):
            per_times, _curs, _reached = _paginate(repo, rand_base, depth, "random")
            times.append(min(per_times) if per_times else 0.0)
        rows.append((f"翻页-random 第{depth}页", min(times), statistics.median(times)))
    return rows


def cmd_bench(args: argparse.Namespace) -> int:
    engine, session = make_session(args.url)
    n = args.n
    print("=" * 78)
    print("copixiv PG 查询性能基准（warm，真实数据库 + 真实 repo 路径）")
    print(f"URL: {args.url}   样本数: n={n}")
    print("=" * 78)

    # warm shared buffers / plan cache
    repo = _build_read_repo(session)
    repo._get_novels_sync(spec())
    repo._get_novels_sync(spec(queries="keyword:恋"))
    repo._count_novels_sync(spec(queries="keyword:恋", min_like=0, min_text=0))

    list_rows = bench_lists(session, n)
    print_table("A. 当前列表查询（warm）", list_rows)

    count_rows = bench_counts(session, n)
    print_table("B. 当前计数查询（warm，每样本绕过进程内 count 缓存）", count_rows)

    # count-cache hit cost (what repeat requests pay after the first)
    cached_rows: list[tuple[str, float, float]] = []
    s = spec(queries="keyword:恋", min_like=0, min_text=0)
    repo._count_novels_sync(s)  # populate cache
    for _ in range(n):
        t = time.perf_counter()
        repo._count_novels_sync(s)
        cached_rows.append(((time.perf_counter() - t) * 1000, 0, 0))
    print_table("B8. 计数-关键词恋（缓存命中对照）",
                [("计数-关键词恋 cache-hit",
                  min(c[0] for c in cached_rows),
                  statistics.median(c[0] for c in cached_rows))])

    # tag suggestion (the search-box autocomplete path, old bench C6)
    import asyncio

    from copixiv.features.tags.repo import SQLAlchemyTagRepository

    tag_repo = SQLAlchemyTagRepository(session)
    mn, md, _ = bench(
        lambda: asyncio.run(tag_repo.suggest_aliases(limit=5, offset=0)),
        n=n, session=session)
    print_table("D. 标签建议 suggest_aliases（warm）",
                [("标签建议(limit=5)", mn, md)])

    page_rows = bench_pagination(session, n)
    print_table("C. 翻页（keyset，warm）", page_rows)

    session.close()
    engine.dispose()
    print("\nbench 完成。")
    return 0


# ---------------------------------------------------------------------------
# explain — representative EXPLAIN (ANALYZE, BUFFERS, COSTS)
# ---------------------------------------------------------------------------

EXPLAIN_CASES = [
    ("默认列表(500/3000)+like", dict(queries="", order_by="like")),
    ("标签R-18列表", dict(queries="tags:R-18", order_by="like",
                         min_like=0, min_text=0)),
    ("标签R-18计数", dict(queries="tags:R-18", min_like=0, min_text=0,
                        count=True)),
    ("关键词恋列表", dict(queries="keyword:恋", order_by="like",
                        min_like=0, min_text=0)),
    ("关键词恋列表(带阈值500/3000)", dict(queries="keyword:恋",
                                     order_by="like")),
    ("关键词R-18列表", dict(queries="keyword:R-18", order_by="like",
                          min_like=0, min_text=0)),
    ("关键词R-18计数", dict(queries="keyword:R-18", min_like=0, min_text=0,
                          count=True)),
    ("关键词催眠计数", dict(queries="keyword:催眠", min_like=0, min_text=0,
                         count=True)),
    ("组合 tagNTR+关键词恋列表", dict(queries="tags:NTR;keyword:恋",
                                 order_by="like", min_like=0, min_text=0)),
    ("翻页-like 第500页", dict(queries="", order_by="like", depth=500)),
]


def _stmt_to_sql(session: Session, s: QuerySpec, depth: int | None = None,
                 count: bool = False) -> str:
    """Render the repo's real query SQL with literal binds.

    * ``count=True`` renders the COUNT(*) form (``build_count``).
    * The blocked-tag exclusion is read from the DB (same as the repo request
      path) so the plan matches what ``bench`` actually measures.
    * If *depth* is given, first fast-forward a keyset cursor to that page so
      the EXPLAIN shows the deep-page query shape.
    """
    from copixiv.features.novels.repo import NovelQueryBuilder

    read_repo = SQLAlchemyNovelReadRepository(session)
    blocked_names = (
        read_repo._blocked_tag_names()
        if read_repo._exclusion_active(s.exclude_blocked_tags)
        else frozenset()
    )
    if depth is not None and depth > 1:
        curs = None
        for _ in range(depth - 1):
            page_spec = s.model_copy(update={"cursor": curs})
            result = read_repo._get_novels_sync(page_spec)
            curs = result["cursor"]
            if curs is None:
                break
        s = s.model_copy(update={"cursor": curs})
    b = NovelQueryBuilder(read_repo, s, blocked_tag_names=blocked_names)
    if count:
        stmt = b.build_count()
        if stmt is None:
            stmt = select(func.count()).select_from(models.Novel)
    else:
        stmt, _ = b.build()
    return str(stmt.compile(
        dialect=session.get_bind().dialect,
        compile_kwargs={"literal_binds": True},
    ))


def cmd_explain(args: argparse.Namespace) -> int:
    engine, session = make_session(args.url)
    print("=" * 78)
    print("copixiv PG EXPLAIN (ANALYZE, BUFFERS, COSTS)")
    print(f"URL: {args.url}")
    print("=" * 78)
    for label, kwargs in EXPLAIN_CASES:
        depth = kwargs.pop("depth", None)
        count = kwargs.pop("count", False)
        s = spec(**kwargs)
        print(f"\n--- {label} ---")
        try:
            sql = _stmt_to_sql(session, s, depth, count=count)
            rows = session.execute(
                text("EXPLAIN (ANALYZE, BUFFERS, COSTS) " + sql)
            ).fetchall()
            for r in rows:
                print("  " + r[0])
        except Exception as exc:
            print(f"  !! EXPLAIN 失败: {exc.__class__.__name__}: {exc}")
    session.close()
    engine.dispose()
    return 0


# ---------------------------------------------------------------------------
# cold — PG restart + first-query measurement
# ---------------------------------------------------------------------------

COLD_FIRST_CASES = [
    ("冷-首查默认列表(500/3000)", dict(queries="", order_by="like")),
    ("冷-首查关键词恋列表", dict(queries="keyword:恋", order_by="like",
                             min_like=0, min_text=0)),
    ("冷-首查关键词R-18列表", dict(queries="keyword:R-18", order_by="like",
                               min_like=0, min_text=0)),
    ("冷-首查标签R-18列表", dict(queries="tags:R-18", order_by="like",
                             min_like=0, min_text=0)),
    ("冷-首查计数默认阈值", dict(queries="")),
    ("冷-首查计数关键词R-18", dict(queries="keyword:R-18",
                               min_like=0, min_text=0)),
    ("冷-首查翻页第2页", dict(queries="", order_by="like", depth=2)),
]


def _run_pg_dev(action: str) -> None:
    subprocess.run([sys.executable, str(PG_DEV), action], check=True)


def cmd_cold(args: argparse.Namespace) -> int:
    print("=" * 78)
    print("copixiv PG 冷启动实测")
    print(f"URL: {args.url}")
    print("=" * 78)

    # 1) warm baseline on the running instance
    engine, session = make_session(args.url)
    repo = _build_read_repo(session)
    warm_rows: list[tuple[str, float | None, float | None]] = []
    for label, kwargs in COLD_FIRST_CASES:
        depth = kwargs.get("depth", 1)
        kwargs = {k: v for k, v in kwargs.items() if k != "depth"}
        s = spec(**kwargs)
        if label.startswith("冷-首查计数"):
            mn, md, _ = bench(
                lambda: repo._count_novels_sync(s), n=args.n, session=session)
        elif depth > 1:
            per_times, _curs, _reached = _paginate(
                repo, s, depth, kwargs.get("order_by", "like"))
            mn, md = min(per_times), statistics.median(per_times)
        else:
            mn, md, _ = bench(
                lambda: repo._get_novels_sync(s), n=args.n, session=session)
        warm_rows.append((label, mn, md))
    session.close()
    engine.dispose()
    print_table("0) warm 基线（重启前）", warm_rows)

    # 2) restart PG
    print("\n-- 重启本地 PG（pg_dev stop → start） --")
    t0 = time.perf_counter()
    _run_pg_dev("stop")
    stop_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    _run_pg_dev("start")
    start_ms = (time.perf_counter() - t0) * 1000
    print(f"stop={stop_ms:.0f}ms  start(到就绪)={start_ms:.0f}ms")

    # 3) brand-new engine (≈ fresh app process pool) → first queries
    engine_cold, session_cold = make_session(args.url)
    repo_cold = _build_read_repo(session_cold)
    cold_first_rows: list[tuple[str, float | None, float | None]] = []
    explain_evidence: list[tuple[str, str]] = []
    for label, kwargs in COLD_FIRST_CASES:
        depth = kwargs.get("depth", 1)
        kwargs = {k: v for k, v in kwargs.items() if k != "depth"}
        s = spec(**kwargs)
        is_count = label.startswith("冷-首查计数")
        novels_repo_module._count_cache.clear()
        t = time.perf_counter()
        try:
            if is_count:
                run_with_timeout(lambda: repo_cold._count_novels_sync(s), 60.0)
            elif depth > 1:
                _paginate(repo_cold, s, depth, kwargs.get("order_by", "like"))
            else:
                run_with_timeout(lambda: repo_cold._get_novels_sync(s), 60.0)
            first_ms = (time.perf_counter() - t) * 1000
        except Timeout:
            first_ms = None
            session_cold.rollback()
        except Exception as exc:
            first_ms = None
            session_cold.rollback()
            print(f"  [cold] {label} 错误: {exc.__class__.__name__}: {exc}")
        cold_first_rows.append((label, first_ms, None))

        # warm repeat (3 samples) on the same session for contrast
        if first_ms is not None:
            if is_count:
                m2, m3, _ = bench(
                    lambda: repo_cold._count_novels_sync(s), n=3,
                    session=session_cold)
            elif depth > 1:
                m2, m3 = first_ms, first_ms
            else:
                m2, m3, _ = bench(
                    lambda: repo_cold._get_novels_sync(s), n=3,
                    session=session_cold)
            cold_first_rows[-1] = (label, first_ms, m3)

        # BUFFERS evidence (warm now: the first query warmed the relation)
        if first_ms is not None:
            try:
                sql = _stmt_to_sql(session_cold, s)
                rows = session_cold.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS) " + sql)
                ).fetchall()
                plan = "\n".join(r[0] for r in rows)
                buff = [ln for ln in plan.splitlines()
                        if "buffers:" in ln or "shared" in ln]
                explain_evidence.append((label, "\n".join(buff[:3])))
            except Exception:
                pass

    session_cold.close()
    engine_cold.dispose()

    print_table("1) 冷启动首查（PG 重启后首个全新连接池）",
                cold_first_rows,
                extra_header="warm-median(重启后)")
    print("\n-- 重启后 BUFFERS 证据（shared hit / read，此时已 warm） --")
    for label, ev in explain_evidence:
        print(f"  {label:<32} {ev.replace(chr(10), ' | ')}")

    print("\n-- warm 基线 vs 冷启动首查（倍率） --")
    warm_map = {w[0]: w[1] for w in warm_rows}
    for label, first, _med in cold_first_rows:
        base = warm_map.get(label)
        if first is None:
            print(f"  {label:<36} 冷={'超时':>10}  warm={fmt(base)}")
        elif base:
            ratio = first / base
            print(f"  {label:<36} 冷={first:>10.1f}ms  warm={fmt(base)}  "
                  f"慢 {ratio:>6.1f}×")
        else:
            print(f"  {label:<36} 冷={first:>10.1f}ms  warm=?")

    print("\ncold 完成（PG 已重新启动并保持运行）。")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("check", "bench", "cold", "explain"):
        sp = sub.add_parser(name, help=f"run `{name}`")
        sp.add_argument("--url", default=DEFAULT_URL,
                        help=f"SQLAlchemy URL (default: {DEFAULT_URL})")
        sp.add_argument("--n", type=int, default=5,
                        help="samples per scenario (default 5)")
        sp.set_defaults(fn=globals()[f"cmd_{name}"])

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Timeout:
        print("!! 基准超时（>30s），已中止。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("!! 中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))