#!/usr/bin/env python3
"""One-shot: restore missing authors via the Pixiv API.

Background: the SQLite→PG migration turned novels whose ``author_id``
referenced a row missing from the ``author`` table into ``author_id
IS NULL`` (see migrate_sqlite_to_pg.py).  Such rows pass the Pydantic
validation of the read path with a NULL author_id … but the domain
``Novel.author_id`` is a required ``int``, so those rows crash the
random-browse list with a 500 (about 15% of random pages, ~1200 rows).

This script asks Pixiv for each missing author (``user_detail``) and,
when the author still exists, inserts the ``author`` row and re-links
``novel.author_id``.  Authors that no longer exist (deleted/blocked)
are left NULL — the domain model tolerates NULL after the companion
``Novel.author_id -> int | None`` change.

Usage (from the project root)::

    source .venv/bin/activate
    python scripts/restore_missing_authors.py            # dry-run: probe + report
    python scripts/restore_missing_authors.py --commit   # probe + write to PG

The write step is idempotent (skips authors already present; ``UPDATE
novel ... AND author_id IS NULL``).  Run ``pg_dump`` first if in doubt.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SQLITE_DB = str(ROOT / "database" / "database.db")


# ---------------------------------------------------------------------------
# Step 1 — collect (novel_id, author_id) pairs orphaned in author
# ---------------------------------------------------------------------------

def collect_orphans() -> dict[int, list[int]]:
    """Return {author_id: [novel_id, ...]} for novels whose author is missing."""
    conn = sqlite3.connect(SQLITE_DB)
    try:
        rows = conn.execute(
            """
            SELECT n.author_id, n.id
            FROM novel n
            WHERE n.author_id NOT IN (SELECT author_id FROM author)
            ORDER BY n.author_id, n.id
            """
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, list[int]] = {}
    for author_id, novel_id in rows:
        out.setdefault(author_id, []).append(novel_id)
    return out


# ---------------------------------------------------------------------------
# Step 2 — probe Pixiv
# ---------------------------------------------------------------------------

def _user_name(result) -> str | None:
    user = result.get("user") if isinstance(result, dict) else getattr(result, "user", None)
    if user is None:
        return None
    if isinstance(user, dict):
        return user.get("name")
    return getattr(user, "name", None)


async def probe_authors(author_ids: list[int]) -> dict[int, str | None]:
    """Return {author_id: name} for authors that still exist; errors -> omitted."""
    from copixiv.pixiv.patch import apply as apply_patch
    apply_patch()

    from copixiv.config import load_config
    from copixiv.pixiv.accounts import AccountPool
    from copixiv.pixiv.client import PixivClient
    from copixiv.db.engine import create_database_engine, create_session_factory
    from copixiv.app import _load_accounts

    cfg = load_config(str(ROOT / "config.yaml"))
    sf = create_session_factory(create_database_engine(cfg.database_url))
    pool = AccountPool()
    _load_accounts(sf, pool, cfg)

    client = PixivClient(pool, max_concurrency=3)
    found: dict[int, str | None] = {}
    for author_id in author_ids:
        try:
            result = await client.user_detail(author_id)
            found[author_id] = _user_name(result)
        except Exception as e:  # noqa: BLE001 — one bad author must not stop the run
            print(f"  author {author_id}: REMOTE ERROR {type(e).__name__}: {str(e)[:100]}")
    return found


# ---------------------------------------------------------------------------
# Step 3 — write to PG
# ---------------------------------------------------------------------------

def write_to_pg(orphans: dict[int, list[int]], found: dict[int, str | None]) -> None:
    from sqlalchemy import create_engine, text
    from copixiv.config import load_config

    cfg = load_config(str(ROOT / "config.yaml"))
    engine = create_engine(cfg.database_url)
    conn = engine.connect()
    trans = conn.begin()
    cur = conn.connection.cursor()  # raw psycopg2 cursor (same transaction)

    def summary_for(author_id: int, novel_ids: list[int]):
        cur.execute(
            """
            SELECT count(*), coalesce(sum("like"),0), coalesce(sum("view"),0),
                   coalesce(sum("text"),0), max(create_time)
            FROM novel WHERE id = ANY(%s)
            """,
            (novel_ids,),
        )
        return cur.fetchone()

    restored_novels = 0
    try:
        for author_id, novel_ids in sorted(orphans.items()):
            # Skip authors that exist already (idempotent re-runs).
            cur.execute("SELECT 1 FROM author WHERE author_id = %s", (author_id,))
            if cur.fetchone():
                continue
            if author_id not in found:
                continue
            cnt, likes, views, texts, last_update = summary_for(author_id, novel_ids)
            cur.execute(
                """
                INSERT INTO author
                    (author_id, author_name, novel_count, "like", "view", "text",
                     last_update, is_special_follow)
                VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (author_id) DO UPDATE SET author_name = EXCLUDED.author_name
                """,
                (author_id, found[author_id], cnt, likes, views, texts, last_update),
            )
            cur.execute(
                "UPDATE novel SET author_id = %s WHERE id = ANY(%s) AND author_id IS NULL",
                (author_id, novel_ids),
            )
            restored_novels += cur.rowcount
            print(
                f"  author {author_id}: restored name={found[author_id]!r} "
                f"novels={cnt} (+{cur.rowcount} linked)"
            )
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()
    print(f"linked novels: {restored_novels}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="write to PostgreSQL")
    args = parser.parse_args()

    orphans = collect_orphans()
    print(f"orphaned authors: {len(orphans)} (novels: {sum(len(v) for v in orphans.values())})")

    found = asyncio.run(probe_authors(sorted(orphans)))
    print(f"still exists on Pixiv: {len(found)} / {len(orphans)}")
    missing = [aid for aid in orphans if aid not in found]
    if missing:
        print(f"gone from Pixiv (left NULL): {missing}")

    if not args.commit:
        print("\nDRY-RUN — no writes performed. Re-run with --commit to write.")
        return

    write_to_pg(orphans, found)
    print("done.")


if __name__ == "__main__":
    main()
