"""Compare PostgreSQL vs Rust in-memory query engine performance on a live DB.

Read-only benchmark: executes non-mutating queries through
``SQLAlchemyNovelRepository`` with ``query_engine=None`` (PG) and with a
populated ``RustNovelQueryEngine`` (Rust), reporting startup snapshot time,
process RSS delta, and per-workload p50 / p95 / mean latencies.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select, text

from copixiv.config import load_config
from copixiv.core.services import QuerySpec
from copixiv.db import models
from copixiv.db.engine import create_database_engine, create_session_factory
from copixiv.db.novel_snapshot import PgSnapshotStore
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.query_engine.sync import build_engine_from_store


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
        statistics.fmean(samples),
        last,
    )


@dataclass(frozen=True)
class BenchCase:
    name: str
    run: Callable[[SQLAlchemyNovelRepository], Any]
    summary: Callable[[Any], str]
    deterministic: bool = True
    unordered: bool = False


def _pick_probe_inputs(session_factory) -> dict[str, Any]:
    with session_factory() as s:
        total = s.execute(text("SELECT count(*) FROM novel")).scalar_one()
        common_tag = s.execute(
            select(models.Tag.name)
            .where(models.Tag.reference_count > 10_000)
            .order_by(desc(models.Tag.reference_count))
            .limit(1)
        ).scalar_one_or_none() or "中文"
        med_tag = s.execute(
            select(models.Tag.name)
            .where(models.Tag.reference_count.between(800, 3_000))
            .order_by(desc(models.Tag.reference_count))
            .limit(1)
        ).scalar_one_or_none() or "日常"
        rare_tag = s.execute(
            select(models.Tag.name)
            .where(models.Tag.reference_count.between(15, 50))
            .order_by(desc(models.Tag.reference_count))
            .limit(1)
        ).scalar_one_or_none() or "日常"
        top_author_id = s.execute(
            select(models.Author.author_id)
            .order_by(desc(models.Author.novel_count).nulls_last())
            .limit(1)
        ).scalar_one_or_none() or 1
        sample_5k_ids = list(
            s.execute(
                select(models.Novel.id).order_by(desc(models.Novel.id)).limit(5_000)
            ).scalars()
        )
        # Walk 20 pages down in like DESC order to get a realistic deep cursor.
        deep_cursor: dict[str, Any] | None = None
        repo = SQLAlchemyNovelRepository(s)
        spec_walk = QuerySpec(order_by="like", order_direction="DESC", per_page=20)
        for _ in range(25):
            page = repo.get_novels(
                spec_walk.model_copy(update={"cursor": deep_cursor}),
            )
            deep_cursor = page["cursor"]
            if deep_cursor is None:
                break

    return {
        "total": total,
        "common_tag": common_tag,
        "med_tag": med_tag,
        "rare_tag": rare_tag,
        "top_author_id": int(top_author_id),
        "sample_5k_ids": sample_5k_ids,
        "deep_like_cursor": deep_cursor,
    }


def _build_cases(probe: dict[str, Any]) -> list[BenchCase]:
    common_tag = probe["common_tag"]
    med_tag = probe["med_tag"]
    rare_tag = probe["rare_tag"]
    top_author_id = probe["top_author_id"]
    sample_5k_ids = probe["sample_5k_ids"]
    deep_like_cursor = probe["deep_like_cursor"]

    def _ids(res: dict[str, Any]) -> list[int]:
        return [n.id for n in res["novels"]]

    def _random_3pages(repo: SQLAlchemyNovelRepository) -> tuple[int, int]:
        seen: list[int] = []
        cursor = None
        for _ in range(3):
            res = repo.get_novels(
                QuerySpec(order_by="random", per_page=20, cursor=cursor),
            )
            seen.extend(n.id for n in res["novels"])
            cursor = res["cursor"]
            if cursor is None:
                break
        return (len(seen), len(set(seen)))

    return [
        BenchCase(
            name="get_novels(random, all, p1)",
            run=lambda r: _ids(r.get_novels(QuerySpec(order_by="random", per_page=20))),
            summary=lambda out: f"n={len(out)}",
            deterministic=False,
        ),
        BenchCase(
            name="get_novels(random, 3 pages cursor)",
            run=_random_3pages,
            summary=lambda out: f"rows={out[0]},uniq={out[1]}",
            deterministic=False,
        ),
        BenchCase(
            name=f"get_novels(random, tag={med_tag!r})",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        conditions=[("tags", med_tag)],
                        order_by="random",
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)}",
            deterministic=False,
        ),
        BenchCase(
            name="get_novels(random, min_like=500, min_text=20000)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        order_by="random",
                        min_like=500,
                        min_text=20_000,
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)}",
            deterministic=False,
        ),
        BenchCase(
            name="get_novels(like DESC, all, p1)",
            run=lambda r: _ids(
                r.get_novels(QuerySpec(order_by="like", order_direction="DESC", per_page=20)),
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="get_novels(like DESC, depth~500 cursor)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        order_by="like",
                        order_direction="DESC",
                        per_page=20,
                        cursor=deep_like_cursor,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="get_novels(create_time DESC, all, p1)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(order_by="create_time", order_direction="DESC", per_page=20),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="get_novels(id DESC, keyword='原' CJK 1ch)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        conditions=[("keyword", "原")],
                        order_by="id",
                        order_direction="DESC",
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="get_novels(id DESC, keyword='日常' CJK 2ch)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        conditions=[("keyword", "日常")],
                        order_by="id",
                        order_direction="DESC",
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name=f"get_novels(like DESC, tag={common_tag!r}+keyword='NTR')",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        conditions=[("tags", common_tag), ("keyword", "NTR")],
                        order_by="like",
                        order_direction="DESC",
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="get_novels(title ASC, all, p1)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(order_by="title", order_direction="ASC", per_page=20),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="get_novels(like DESC, keyword='a' 1ch ASCII)",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        conditions=[("keyword", "a")],
                        order_by="like",
                        order_direction="DESC",
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name=f"get_novels(id DESC, author_id={top_author_id})",
            run=lambda r: _ids(
                r.get_novels(
                    QuerySpec(
                        conditions=[("author_id", str(top_author_id))],
                        order_by="id",
                        order_direction="DESC",
                        per_page=20,
                    ),
                )
            ),
            summary=lambda out: f"n={len(out)},head={out[:2]}",
        ),
        BenchCase(
            name="count_novels(all)",
            run=lambda r: r.count_novels(QuerySpec()),
            summary=lambda out: f"{out.value}{'+' if out.truncated else ''}",
        ),
        BenchCase(
            name="count_novels(keyword='原')",
            run=lambda r: r.count_novels(QuerySpec(conditions=[("keyword", "原")])),
            summary=lambda out: f"{out.value}{'+' if out.truncated else ''}",
        ),
        BenchCase(
            name="count_novels(keyword='后宫')",
            run=lambda r: r.count_novels(QuerySpec(conditions=[("keyword", "后宫")])),
            summary=lambda out: f"{out.value}{'+' if out.truncated else ''}",
        ),
        BenchCase(
            name=f"count_novels(tag={med_tag!r}) + excluded",
            run=lambda r: (
                r.count_novels(QuerySpec(conditions=[("tags", med_tag)])),
                r.count_excluded_novels(QuerySpec(conditions=[("tags", med_tag)])),
            ),
            summary=lambda out: f"vis={out[0].value},excl={out[1].value}",
        ),
        BenchCase(
            name=f"list_matching_ids(tag={rare_tag!r})",
            run=lambda r: r.list_matching_ids(QuerySpec(conditions=[("tags", rare_tag)])),
            summary=lambda out: f"len={len(out)}",
        ),
        BenchCase(
            name="list_blocked_ids()",
            run=lambda r: r.list_blocked_ids(),
            summary=lambda out: f"len={len(out)}",
            unordered=True,
        ),
        BenchCase(
            name="filter_ids_in_scope(5000 ids, min_like=100)",
            run=lambda r: r.filter_ids_in_scope(
                sample_5k_ids,
                QuerySpec(min_like=100),
            ),
            summary=lambda out: f"len={len(out)}",
        ),
        BenchCase(
            name="sort_novel_ids(5000 ids, like DESC)",
            run=lambda r: r.sort_novel_ids(
                sample_5k_ids,
                order_by="like",
                order_direction="DESC",
            ),
            summary=lambda out: f"len={len(out)},head={out[:2]}",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark PG vs Rust query engine")
    parser.add_argument("--pg-iters", type=int, default=5)
    parser.add_argument("--pg-warmup", type=int, default=1)
    parser.add_argument("--rust-iters", type=int, default=25)
    parser.add_argument("--rust-warmup", type=int, default=5)
    args = parser.parse_args()

    cfg = load_config()
    db_engine = create_database_engine(cfg.database_url)
    session_factory = create_session_factory(db_engine)
    probe = _pick_probe_inputs(session_factory)
    print(
        f"Live DB: novels={probe['total']:,} common_tag={probe['common_tag']!r} "
        f"med_tag={probe['med_tag']!r} rare_tag={probe['rare_tag']!r}"
    )

    rss_before = _read_rss_mb()
    t0 = time.perf_counter()
    store = PgSnapshotStore(session_factory)
    rust_engine = build_engine_from_store(store, mode="rust")
    build_ms = (time.perf_counter() - t0) * 1_000.0
    rss_after = _read_rss_mb()
    print(
        f"Rust startup snapshot build: {build_ms:.1f} ms | "
        f"RSS {rss_before:.1f} MiB -> {rss_after:.1f} MiB (+{rss_after - rss_before:.1f} MiB)"
    )

    cases = _build_cases(probe)
    header = (
        f"{'Workload':<50} | {'PG p50':>9} | {'PG p95':>9} | "
        f"{'Rust p50':>9} | {'Rust p95':>9} | {'Speedup':>8} | Parity"
    )
    print()
    print(header)
    print("-" * len(header))

    with session_factory() as pg_session, session_factory() as rs_session:
        pg_repo = SQLAlchemyNovelRepository(pg_session)
        rs_repo = SQLAlchemyNovelRepository(rs_session, query_engine=rust_engine)

        mismatches = 0
        for case in cases:
            pg_p50, pg_p95, _pg_mean, pg_out = _measure(
                lambda: case.run(pg_repo),
                warmup=args.pg_warmup,
                iters=args.pg_iters,
            )
            rs_p50, rs_p95, _rs_mean, rs_out = _measure(
                lambda: case.run(rs_repo),
                warmup=args.rust_warmup,
                iters=args.rust_iters,
            )
            speedup = pg_p50 / rs_p50 if rs_p50 > 0 else float("inf")
            if case.deterministic:
                if case.unordered:
                    ok = set(pg_out) == set(rs_out) and len(pg_out) == len(rs_out)
                else:
                    ok = pg_out == rs_out
                if not ok:
                    mismatches += 1
                parity_str = f"EXACT ({case.summary(rs_out)})" if ok else (
                    f"MISMATCH pg={case.summary(pg_out)} rs={case.summary(rs_out)}"
                )
            else:
                parity_str = f"OK ({case.summary(rs_out)})"

            print(
                f"{case.name:<50} | {pg_p50:>7.2f}ms | {pg_p95:>7.2f}ms | "
                f"{rs_p50:>7.2f}ms | {rs_p95:>7.2f}ms | {speedup:>7.1f}x | {parity_str}"
            )

    if mismatches:
        raise SystemExit(f"Benchmark detected {mismatches} deterministic parity mismatch(es)!")


if __name__ == "__main__":
    main()
