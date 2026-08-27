"""Diagnose the ~8s fixed per-transaction cost: attribute it to FTS5.

Hypothesis: any write transaction touching novel_fts (232k docs) pays a
fixed FTS5 segment-maintenance cost regardless of the row count.
"""

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

MASTER = Path("/tmp/batchbench/master.db")
WORK = Path("/tmp/batchbench/work.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlalchemy import text
from copixiv.db.engine import (
    create_database_engine,
    create_session_factory,
)
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.novels.fts import FTSManager


def make_work_copy() -> None:
    """WAL-safe copy via the SQLite backup API (shutil.copy misses -wal)."""
    WORK.unlink(missing_ok=True)
    src = sqlite3.connect(MASTER)
    dst = sqlite3.connect(WORK)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def open_repo():
    engine = create_database_engine(str(WORK))
    sf = create_session_factory(engine)
    session = sf()
    return engine, session, SQLAlchemyNovelRepository(session)


def timed(label, fn) -> float:
    t0 = time.perf_counter()
    fn()
    dt = time.perf_counter() - t0
    print(f"{label:45s} {dt:7.2f}s")
    return dt


def bench_one(size: int = 5000):
    make_work_copy()
    engine, session, repo = open_repo()
    ids = [r[0] for r in session.execute(
        text("SELECT id FROM novel ORDER BY like DESC LIMIT :n"), {"n": size},
    )]
    fts = FTSManager(session)

    # 1. FTS-only: DELETE rowids + commit
    timed(f"FTS-only delete {size} rowids + commit", lambda: (
        fts.delete_novel_fts_many(ids),
        session.commit(),
    ))

    # 2. delete_many with FTS skipped
    def no_fts_delete():
        fts.delete_novel_fts_many = lambda _ids: None
        repo._delete_many_sync(ids)
        session.commit()
    timed(f"delete_many({size}) WITHOUT FTS", no_fts_delete)

    # 3. fresh copy — full delete_many (baseline from previous bench)
    session.close()
    engine.dispose()
    make_work_copy()
    engine, session, repo = open_repo()
    ids = [r[0] for r in session.execute(
        text("SELECT id FROM novel ORDER BY like DESC LIMIT :n"), {"n": size},
    )]
    timed(f"delete_many({size}) FULL", lambda: (
        repo._delete_many_sync(ids),
        session.commit(),
    ))

    # 4. FTS re-index alone (the add_tags FTS cost)
    ids2 = [r[0] for r in session.execute(
        text("SELECT id FROM novel ORDER BY like DESC LIMIT :n OFFSET :m"),
        {"n": size, "m": size},
    )]
    timed(f"update_novel_fts_index({size}) alone", lambda: (
        FTSManager(session).update_novel_fts_index(ids2),
        session.commit(),
    ))
    session.close()
    engine.dispose()


def bench_whole_table():
    make_work_copy()
    engine, session, repo = open_repo()
    ids = [r[0] for r in session.execute(text("SELECT id FROM novel"))]
    print(f"whole-table ids: {len(ids)}")
    try:
        t0 = time.perf_counter()
        repo._delete_many_sync(ids)
        session.commit()
        print(f"whole-table single-shot delete OK: {time.perf_counter()-t0:.2f}s")
    except Exception as exc:
        print(f"whole-table single-shot FAILED: {type(exc).__name__}: {exc}")
    session.close()
    engine.dispose()


def main() -> None:
    Path("/tmp/batchbench").mkdir(exist_ok=True)
    if not MASTER.exists():
        subprocess.run(
            ["sqlite3", "database/database.db", f".backup {MASTER}"],
            check=True,
        )
    bench_one(5000)
    bench_whole_table()


if __name__ == "__main__":
    main()
