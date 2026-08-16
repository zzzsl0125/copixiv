"""Benchmark batch-chunk sizes on a copy of the real database.

Measures the wall time of a single chunked write transaction (op + commit)
for delete_many and add_tags_to_novels at various chunk sizes, plus the
whole-table IN-limit check.  The wall time ≈ global write-lock hold time,
which is what other writers (download tasks) have to wait behind.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

MASTER = Path("/tmp/batchbench/master.db")
WORK = Path("/tmp/batchbench/work.db")
SIZES = [1000, 2000, 5000, 10000, 20000, 50000]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlalchemy import text
from copixiv.infrastructure.database.engine import (
    create_database_engine,
    create_session_factory,
)
from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository


def make_work_copy() -> None:
    WORK.unlink(missing_ok=True)
    shutil.copy2(MASTER, WORK)


def open_repo():
    # Production-equivalent engine: WAL + temp_store=MEMORY pragmas, so the
    # numbers include the same temp B-tree behavior the live app sees.
    engine = create_database_engine(str(WORK))
    sf = create_session_factory(engine)
    session = sf()
    return session, SQLAlchemyNovelRepository(session)


def bench_delete():
    print("=== delete_many (op + commit) ===")
    for size in SIZES:
        make_work_copy()
        session, repo = open_repo()
        ids = [r[0] for r in session.execute(
            text("SELECT id FROM novel ORDER BY like DESC LIMIT :n"), {"n": size},
        )]
        t0 = time.perf_counter()
        paths = repo._delete_many_sync(ids)
        session.commit()
        dt = time.perf_counter() - t0
        session.close()
        print(f"delete  size={size:>6}  wall={dt:6.2f}s  per1k={dt/size*1000:6.1f}ms  paths={len(paths)}")


def bench_add_tags():
    print("=== add_tags_to_novels (op + commit, incl. FTS re-index) ===")
    make_work_copy()
    session, repo = open_repo()
    all_ids = [r[0] for r in session.execute(
        text("SELECT id FROM novel ORDER BY like DESC LIMIT 100000"),
    )]
    offset = 0
    for size in SIZES:
        ids = all_ids[offset:offset + size]
        offset += size
        tag = {f"bench_{size}"}
        t0 = time.perf_counter()
        affected = repo._add_tags_to_novels_sync(ids, tag)
        session.commit()
        dt = time.perf_counter() - t0
        print(f"addtag  size={size:>6}  wall={dt:6.2f}s  per1k={dt/size*1000:6.1f}ms  affected={affected}")
    session.close()


def bench_whole_table_delete():
    print("=== whole-table single delete_many (IN-limit check) ===")
    make_work_copy()
    session, repo = open_repo()
    ids = [r[0] for r in session.execute(text("SELECT id FROM novel"))]
    print(f"total novels: {len(ids)}")
    try:
        t0 = time.perf_counter()
        repo._delete_many_sync(ids)
        session.commit()
        print(f"single-shot delete OK: {time.perf_counter()-t0:.2f}s")
    except Exception as exc:
        print(f"single-shot delete FAILED: {type(exc).__name__}: {exc}")
    session.close()


def main() -> None:
    Path("/tmp/batchbench").mkdir(exist_ok=True)
    if not MASTER.exists():
        print("creating master backup…")
        subprocess.run(
            ["sqlite3", "database/database.db", f".backup {MASTER}"],
            check=True,
        )
    bench_delete()
    bench_add_tags()
    bench_whole_table_delete()


if __name__ == "__main__":
    main()
