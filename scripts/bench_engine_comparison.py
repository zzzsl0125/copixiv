"""Benchmark Rust in-memory query engine startup, RSS footprint, and query latency.

Read-only benchmark against a live database: loads the snapshot via
:func:`build_engine_from_store`, reports build time, process RSS delta,
``memory_breakdown()``, and per-workload p50 / p95 / mean latencies for novel
and tag-suggestion workloads.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import os
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text

from copixiv.config import load_config
from copixiv.core.search import QuerySpec
from copixiv.db import models
from copixiv.db.engine import create_database_engine, create_session_factory
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.query_engine.sync import build_engine_from_store


def _malloc_trim() -> None:
    with contextlib.suppress(OSError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)


def _read_rss_mb() -> float:
    page_size = os.sysconf("SC_PAGE_SIZE")
    with open("/proc/self/statm", encoding="utf-8") as fh:
        parts = fh.read().split()
    return int(parts[1]) * page_size / (1024 * 1024)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _measure(
    fn: Callable[[], Any],
    *,
    warmup: int,
    iters: int,
) -> tuple[float, float, float, Any]:
    last: Any = None
    for _ in range(warmup):
        last = fn()
    samples: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - t0) * 1_000.0)
    samples.sort()
    return (
        _percentile(samples, 50.0),
        _percentile(samples, 95.0),
        statistics.mean(samples),
        last,
    )


def _pick_probe_inputs(session_factory) -> dict[str, Any]:
    with session_factory() as s:
        total = s.execute(text("SELECT count(*) FROM novel")).scalar_one()
        tags = s.execute(
            text(
                """
                SELECT tag_name, COUNT(*) AS cnt
                FROM novel, unnest(tags) AS tag_name
                GROUP BY tag_name
                HAVING COUNT(*) >= 5
                ORDER BY cnt DESC
                LIMIT 200
                """
            )
        ).all()
        common_tag = tags[0][0] if tags else "R-18"
        med_tag = tags[len(tags) // 2][0] if len(tags) > 2 else common_tag
        rare_tag = tags[-1][0] if tags else common_tag
        author_id = s.execute(
            select(models.Novel.author_id).where(models.Novel.author_id.isnot(None)).limit(1)
        ).scalar_one_or_none()
        return {
            "total": int(total),
            "common_tag": common_tag,
            "med_tag": med_tag,
            "rare_tag": rare_tag,
            "author_id": author_id,
        }


@dataclass(frozen=True)
class BenchCase:
    name: str
    run: Callable[[SQLAlchemyNovelRepository, SQLAlchemyTagRepository], Any]
    summary: Callable[[Any], str]


def _build_cases(probe: dict[str, Any]) -> list[BenchCase]:
    def _novel_summary(res: dict) -> str:
        return f"rows={len(res['novels'])} has_cur={res['cursor'] is not None}"

    cases = [
        BenchCase(
            name="Hot track: like DESC limit=30",
            run=lambda nr, _tr: nr.get_novels(QuerySpec(order_by="like", per_page=30)),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Hot track: abs_id DESC min_like=500 min_text=3000",
            run=lambda nr, _tr: nr.get_novels(
                QuerySpec(order_by="id", min_like=500, min_text=3000, per_page=30)
            ),
            summary=_novel_summary,
        ),
        BenchCase(
            name=f"Tag filter: {probe['med_tag']!r} order=like",
            run=lambda nr, _tr: nr.get_novels(
                QuerySpec(conditions=[("tags", probe["med_tag"])], order_by="like", per_page=30)
            ),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Keyword CJK: '百合' order=like",
            run=lambda nr, _tr: nr.get_novels(
                QuerySpec(conditions=[("keyword", "百合")], order_by="like", per_page=30)
            ),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Keyword multi-seg: '百合 日常' order=like",
            run=lambda nr, _tr: nr.get_novels(
                QuerySpec(conditions=[("keyword", "百合 日常")], order_by="like", per_page=30)
            ),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Keyword ASCII-CI + space: 'r-18 百合'",
            run=lambda nr, _tr: nr.get_novels(
                QuerySpec(conditions=[("keyword", "r-18 百合")], order_by="create_time", per_page=30)
            ),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Keyword single char: '猫' order=like",
            run=lambda nr, _tr: nr.get_novels(
                QuerySpec(conditions=[("keyword", "猫")], order_by="like", per_page=30)
            ),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Count bounded: min_like=500 min_text=3000",
            run=lambda nr, _tr: nr.count_novels(QuerySpec(min_like=500, min_text=3000)),
            summary=lambda c: f"value={c.value} trunc={c.truncated}",
        ),
        BenchCase(
            name="Random page: unfiltered limit=30",
            run=lambda nr, _tr: nr.get_novels(QuerySpec(order_by="random", per_page=30)),
            summary=_novel_summary,
        ),
        BenchCase(
            name="Tag aliases suggest page limit=10",
            run=lambda _nr, tr: tr.suggest_aliases(limit=10, offset=0),
            summary=lambda d: f"items={len(d['items'])} next={d['next_cursor']}",
        ),
        BenchCase(
            name=f"Tag aliases suggest target={probe['med_tag']!r}",
            run=lambda _nr, tr: tr.suggest_aliases(limit=10, offset=0, target_tag=probe["med_tag"]),
            summary=lambda d: f"cands={len(d['items'][0]['candidates'])}" if d["items"] else "empty",
        ),
    ]
    if probe["author_id"] is not None:
        aid = str(probe["author_id"])
        cases.append(
            BenchCase(
                name=f"Author filter: author_id={aid}",
                run=lambda nr, _tr: nr.get_novels(
                    QuerySpec(conditions=[("author_id", aid)], order_by="create_time", per_page=30)
                ),
                summary=_novel_summary,
            )
        )
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Rust single query engine")
    parser.add_argument("--iters", type=int, default=25)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--max-rss-mib", type=float, default=150.0)
    args = parser.parse_args()

    cfg = load_config()
    db_engine = create_database_engine(cfg.database_url)
    session_factory = create_session_factory(db_engine)
    probe = _pick_probe_inputs(session_factory)
    print(
        f"Live DB: novels={probe['total']:,} common_tag={probe['common_tag']!r} "
        f"med_tag={probe['med_tag']!r} rare_tag={probe['rare_tag']!r}"
    )

    gc.collect()
    _malloc_trim()
    rss_before = _read_rss_mb()

    t0 = time.perf_counter()
    store = PgSnapshotStore(session_factory)
    rust_engine = build_engine_from_store(store)
    build_ms = (time.perf_counter() - t0) * 1_000.0

    gc.collect()
    _malloc_trim()
    rss_after = _read_rss_mb()
    delta_mib = rss_after - rss_before

    print(
        f"Rust startup snapshot build: {build_ms:.1f} ms | "
        f"RSS {rss_before:.1f} MiB -> {rss_after:.1f} MiB (+{delta_mib:.1f} MiB)"
    )
    print("Memory breakdown:")
    total_b = 0
    for name, cnt, size_b in rust_engine.memory_breakdown():
        total_b += size_b
        print(f"  {name:20s}: count={cnt:10d} size={size_b / (1024 * 1024):6.2f} MiB")
    print(f"  {'SUM':20s}: {'':16s} size={total_b / (1024 * 1024):6.2f} MiB")

    cases = _build_cases(probe)
    header = f"{'Workload':<50} | {'p50':>8} | {'p95':>8} | {'mean':>8} | Output"
    print()
    print(header)
    print("-" * len(header))

    with session_factory() as session:
        novel_repo = SQLAlchemyNovelRepository(session, query_engine=rust_engine)
        tag_repo = SQLAlchemyTagRepository(session, query_engine=rust_engine)
        for case in cases:
            p50, p95, mean, out = _measure(
                lambda c=case: c.run(novel_repo, tag_repo),
                warmup=args.warmup,
                iters=args.iters,
            )
            print(f"{case.name:<50} | {p50:>6.2f}ms | {p95:>6.2f}ms | {mean:>6.2f}ms | {case.summary(out)}")

    if args.max_rss_mib > 0 and delta_mib > args.max_rss_mib:
        raise SystemExit(f"RSS delta +{delta_mib:.1f} MiB exceeded limit +{args.max_rss_mib:.1f} MiB!")
    print(f"\nRSS check PASSED: +{delta_mib:.1f} MiB <= +{args.max_rss_mib:.1f} MiB")


if __name__ == "__main__":
    main()
