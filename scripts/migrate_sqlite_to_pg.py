#!/usr/bin/env python3
"""One-time SQLite → PostgreSQL data migration (postgres-migration).

Moves the SQLite ``database/database.db`` into an *already Alembic-migrated*
PostgreSQL target database (the greenfield schema from ``db_greenfield_design.md``
§4).  Handles the structural transformation:

- ``novel_tag`` join table  → ``novel.tags text[]`` (per-novel unique set).
- ``favourite`` / ``special_follow`` join tables → ``novel.is_favourite`` /
  ``author.is_special_follow`` booleans.
- ``novel_fts`` char-gram source → ``novel_search.search_text`` (computed via
  ``gram_tokenize`` over title + author_name + series_name + tags).
- Pluraled SQLite table names → singular PG names
  (``scheduled_tasks`` → ``scheduled_task``, ``settings`` → ``setting``,
  ``tag_aliases`` → ``tag_alias``, ``tag_preferences`` → ``tag_preference``,
  ``tokens`` → ``token``).
- varchar timestamps → ``timestamptz`` (naive treated as Asia/Tokyo, per §3.4).
- ``task_history.result/progress`` and ``scheduled_task.params`` → ``JSONB``.
- ``tag.reference_count`` is recomputed from the migrated ``novel.tags``.

Usage::

    python scripts/migrate_sqlite_to_pg.py --limit 2000 --db copixiv_greenfield
    python scripts/migrate_sqlite_to_pg.py                     # full migration
    python scripts/migrate_sqlite_to_pg.py --url postgresql+psycopg2://... --limit 0

``--limit N`` selects the first N novels ordered by ``novel.shuffle`` (a
precomputed random key → a dispersed sample); ``0`` means the full corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import sqlite3
from psycopg2.extras import Json
from sqlalchemy.engine import make_url

# Make copixiv importable for gram_tokenize (independent of how alembic configures src).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from copixiv.features.novels.fts import gram_tokenize

JST = ZoneInfo("Asia/Tokyo")

DEFAULT_SQLITE = str(PROJECT_ROOT / "database" / "database.db")
DEFAULT_PG_PORT = 5433

# The selected novels are materialized into a SQLite temp table ``_selected`` so
# every downstream query filters with ``id IN (SELECT id FROM _selected)`` — this
# avoids SQLite's bind-variable cap for large limits and keeps the selection
# consistent across all tables.
_SEL = "(SELECT id FROM _selected)"


def _convert_dt(value):
    """Normalize a source timestamp string to an aware UTC datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            dt = datetime.strptime(s, "%Y-%m-%d")
        else:
            try:
                from dateutil.parser import isoparse
                dt = isoparse(s)
            except Exception:
                try:
                    dt = datetime.fromisoformat(s)
                except Exception:
                    return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(timezone.utc)


def _to_jsonb(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return Json(value)
    s = str(value)
    try:
        parsed = json.loads(s)
    except Exception:
        # Not valid JSON — store the raw string as a JSON string value.
        return Json(value)
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except Exception:
            pass
    return Json(parsed)


def _b(value) -> bool:
    """SQLite bool-ish (0/1/int) → Python bool."""
    return bool(value)


def _connect_pg(url_str: str):
    url = make_url(url_str)
    host = url.host
    if host is None and "host" in url.query:
        host = url.query["host"]
    return psycopg2.connect(
        host=host or "127.0.0.1",
        port=url.port or url.query.get("port") or DEFAULT_PG_PORT,
        user=url.username or "postgres",
        password=url.password,
        dbname=url.database,
    )


def _open_sqlite(path: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


# ---------------------------------------------------------------------------
# Table migrators
# ---------------------------------------------------------------------------

def migrate_author(src, cur) -> tuple[int, set[int]]:
    q = f"""
        SELECT a.*, (SELECT 1 FROM special_follow sf WHERE sf.author_id = a.author_id) AS sf
        FROM author a
        WHERE a.author_id IN (SELECT DISTINCT author_id FROM novel WHERE id IN {_SEL})
    """
    rows = [dict(r) for r in src.execute(q)]
    rows = [r for r in rows if r["author_id"] is not None]
    cur.executemany(
        """
        INSERT INTO author (author_id, author_name, novel_count, "like", "view",
                            "text", last_update, is_special_follow)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                r["author_id"], r["author_name"], r["novel_count"] or 0,
                r["like"] or 0, r["view"] or 0, r["text"] or 0,
                _convert_dt(r["last_update"]), _b(r["sf"]),
            )
            for r in rows
        ],
    )
    return len(rows), {r["author_id"] for r in rows}


def migrate_series(src, cur, author_ids: set[int]) -> tuple[int, set[int]]:
    q = f"""
        SELECT s.*
        FROM series s
        WHERE s.series_id IN (SELECT DISTINCT series_id FROM novel WHERE id IN {_SEL})
    """
    rows = [dict(r) for r in src.execute(q)]
    rows = [r for r in rows if r["series_id"] is not None]
    cur.executemany(
        """
        INSERT INTO series (series_id, series_name, novel_count, author_id,
                            "like", "view", "text")
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                r["series_id"], r["series_name"], r["novel_count"] or 0,
                r["author_id"] if r["author_id"] in author_ids else None,
                r["like"] or 0, r["view"] or 0, r["text"] or 0,
            )
            for r in rows
        ],
    )
    return len(rows), {r["series_id"] for r in rows}


def migrate_tag(src, cur) -> int:
    rows = [dict(r) for r in src.execute("SELECT id, name FROM tag")]
    cur.executemany(
        "INSERT INTO tag (id, name, reference_count) VALUES (%s, %s, 0)",
        [(r["id"], r["name"]) for r in rows],
    )
    return len(rows)


def _load_tags_by_novel(src) -> dict[int, list[str]]:
    """Build {novel_id: [unique tag names]} for the selected novels."""
    q = f"""
        SELECT nt.novel_id, t.name
        FROM novel_tag nt JOIN tag t ON t.id = nt.tag_id
        WHERE nt.novel_id IN {_SEL}
        ORDER BY nt.novel_id, t.name
    """
    tags_by: dict[int, list[str]] = {}
    for r in src.execute(q):
        tags_by.setdefault(r["novel_id"], []).append(r["name"])
    # 每本唯一化: the novel.tags invariant requires unique elements.
    for nid in tags_by:
        seen = set()
        uniq = []
        for name in tags_by[nid]:
            if name not in seen:
                seen.add(name)
                uniq.append(name)
        tags_by[nid] = uniq
    return tags_by


def migrate_novel_and_search(src, cur, author_ids: set[int], series_ids: set[int]) -> tuple[int, dict[int, str]]:
    tags_by = _load_tags_by_novel(src)
    q = f"""
        SELECT n.*,
               (SELECT 1 FROM favourite f WHERE f.novel_id = n.id) AS fav
        FROM novel n
        WHERE n.id IN {_SEL}
        ORDER BY n.id
    """
    rows = [dict(r) for r in src.execute(q)]
    novel_rows = []
    search_map: dict[int, str] = {}
    for r in rows:
        nid = r["id"]
        tags = sorted(tags_by.get(nid, []))
        # Null out orphan FKs that the source tolerated (foreign_keys pragma was off).
        author_id = r["author_id"] if r["author_id"] in author_ids else None
        series_id = r["series_id"] if r["series_id"] in series_ids else None
        novel_rows.append((
            nid, r["title"], author_id, r["author_name"], r["path"],
            r["like"] or 0, r["view"] or 0, r["text"] or 0, r["caption"],
            series_id, r["series_name"], r["series_index"],
            _convert_dt(r["create_time"]), r["has_epub"] or 0, r["shuffle"] or 0,
            tags, _b(r["fav"]),
        ))
        search_src = " ".join([
            r["title"] or "",
            r["author_name"] or "",
            r["series_name"] or "",
            " ".join(tags),
        ])
        search_map[nid] = gram_tokenize(search_src)

    cur.executemany(
        """
        INSERT INTO novel (id, title, author_id, author_name, path, "like", "view",
                           "text", caption, series_id, series_name, series_index,
                           create_time, has_epub, shuffle, tags, is_favourite)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        novel_rows,
    )
    return len(novel_rows), search_map


def migrate_novel_search(cur, search_map: dict[int, str]) -> int:
    cur.executemany(
        "INSERT INTO novel_search (novel_id, search_text) VALUES (%s, %s)",
        [(nid, txt) for nid, txt in search_map.items()],
    )
    return len(search_map)


def migrate_failed_novel(src, cur) -> int:
    rows = [dict(r) for r in src.execute(
        f"SELECT novel_id, failure_type, error_message, failed_times, title, "
        f"last_failed_at FROM failed_novel WHERE novel_id IN {_SEL}"
    )]
    cur.executemany(
        """
        INSERT INTO failed_novel (novel_id, failure_type, error_message,
                                  failed_times, title, last_failed_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            (
                r["novel_id"], r["failure_type"], r["error_message"],
                r["failed_times"] or 1, r["title"], _convert_dt(r["last_failed_at"]),
            )
            for r in rows
        ],
    )
    return len(rows)


def migrate_task_history(src, cur) -> int:
    rows = [dict(r) for r in src.execute(
        "SELECT name, task_func, arguments, status, start_time, end_time, "
        "duration, result, progress FROM task_history"
    )]
    cur.executemany(
        """
        INSERT INTO task_history (name, task_func, arguments, status, start_time,
                                  end_time, duration, result, progress)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                r["name"], r["task_func"], r["arguments"], r["status"],
                _convert_dt(r["start_time"]), _convert_dt(r["end_time"]),
                r["duration"], _to_jsonb(r["result"]), _to_jsonb(r["progress"]),
            )
            for r in rows
        ],
    )
    return len(rows)


def migrate_scheduled_task(src, cur) -> int:
    rows = [dict(r) for r in src.execute(
        "SELECT name, task, cron, params, is_enabled, config, sort_index "
        "FROM scheduled_tasks"
    )]
    cur.executemany(
        """
        INSERT INTO scheduled_task (name, task, cron, params, is_enabled,
                                    config, sort_index)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                r["name"], r["task"], r["cron"], _to_jsonb(r["params"]),
                _b(r["is_enabled"]), r["config"], r["sort_index"] or 0,
            )
            for r in rows
        ],
    )
    return len(rows)


def migrate_token(src, cur) -> int:
    rows = [dict(r) for r in src.execute(
        "SELECT name, token, premium, valid, sort_index, is_follow FROM tokens"
    )]
    cur.executemany(
        """
        INSERT INTO token (name, token, premium, valid, sort_index, is_follow)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            (
                r["name"], r["token"], _b(r["premium"]), _b(r["valid"]),
                r["sort_index"] or 0, _b(r["is_follow"]),
            )
            for r in rows
        ],
    )
    return len(rows)


def migrate_tag_preference(src, cur) -> int:
    rows = [dict(r) for r in src.execute(
        "SELECT tag, preference, sort_index FROM tag_preferences"
    )]
    cur.executemany(
        "INSERT INTO tag_preference (tag, preference, sort_index) VALUES (%s, %s, %s)",
        [(r["tag"], r["preference"], r["sort_index"] or 0) for r in rows],
    )
    return len(rows)


def migrate_tag_alias(src, cur) -> int:
    rows = [dict(r) for r in src.execute("SELECT source, target FROM tag_aliases")]
    cur.executemany(
        "INSERT INTO tag_alias (source, target) VALUES (%s, %s)",
        [(r["source"], r["target"]) for r in rows],
    )
    return len(rows)


def migrate_search_history(src, cur) -> int:
    rows = [dict(r) for r in src.execute(
        "SELECT type, value, display_value, timestamp FROM search_history"
    )]
    cur.executemany(
        """
        INSERT INTO search_history (type, value, display_value, timestamp)
        VALUES (%s, %s, %s, %s)
        """,
        [
            (r["type"], r["value"], r["display_value"], _convert_dt(r["timestamp"]))
            for r in rows
        ],
    )
    return len(rows)


def migrate_setting(src, cur) -> int:
    rows = [dict(r) for r in src.execute("SELECT key, value FROM settings")]
    cur.executemany(
        "INSERT INTO setting (key, value) VALUES (%s, %s)",
        [(r["key"], r["value"]) for r in rows],
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _select_into_temp(src, limit: int) -> int:
    """Materialize the selected novel ids into the ``_selected`` temp table."""
    src.execute("CREATE TEMP TABLE _selected (id INTEGER PRIMARY KEY)")
    if limit and limit > 0:
        rows = src.execute(
            "SELECT id FROM novel ORDER BY shuffle, id LIMIT ?", (limit,)
        )
    else:
        rows = src.execute("SELECT id FROM novel")
    src.executemany("INSERT INTO _selected (id) VALUES (?)", [(r["id"],) for r in rows])
    return src.execute("SELECT count(*) FROM _selected").fetchone()[0]


def _reset_target(cur) -> None:
    cur.execute(
        """
        TRUNCATE novel, author, series, tag, tag_alias, tag_preference,
                 failed_novel, novel_search, scheduled_task, task_history,
                 token, setting, search_history
        RESTART IDENTITY CASCADE
        """
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Migrate SQLite → PostgreSQL.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Migrate only the first N novels ordered by shuffle (0 = all).")
    ap.add_argument("--url", default=None,
                    help="PostgreSQL SQLAlchemy URL (default: derived from --db).")
    ap.add_argument("--sqlite", default=DEFAULT_SQLITE, help="Path to the SQLite source DB.")
    ap.add_argument("--db", default="copixiv_greenfield", help="Target PostgreSQL database name.")
    ap.add_argument("--reset", action="store_true",
                    help="TRUNCATE the target tables first (re-run support).")
    args = ap.parse_args(argv)

    url = args.url or f"postgresql+psycopg2://postgres@127.0.0.1:{DEFAULT_PG_PORT}/{args.db}"

    src = _open_sqlite(args.sqlite)
    pg = _connect_pg(url)
    cur = pg.cursor()
    try:
        cur.execute("SELECT count(*) FROM novel")
        nonempty = cur.fetchone()[0]
        if nonempty and not args.reset:
            print(f"Target novel table already has {nonempty} rows; use --reset to overwrite.", file=sys.stderr)
            return 1
        if args.reset:
            _reset_target(cur)

        selected_count = _select_into_temp(src, args.limit)
        print(f"Selected {selected_count} novels (limit={args.limit}).")

        counts = {}
        n_author, author_ids = migrate_author(src, cur)
        counts["author"] = n_author
        n_series, series_ids = migrate_series(src, cur, author_ids)
        counts["series"] = n_series
        counts["tag"] = migrate_tag(src, cur)
        n_novel, search_map = migrate_novel_and_search(src, cur, author_ids, series_ids)
        counts["novel"] = n_novel
        counts["novel_search"] = migrate_novel_search(cur, search_map)
        counts["failed_novel"] = migrate_failed_novel(src, cur)
        counts["scheduled_task"] = migrate_scheduled_task(src, cur)
        counts["task_history"] = migrate_task_history(src, cur)
        counts["token"] = migrate_token(src, cur)
        counts["tag_preference"] = migrate_tag_preference(src, cur)
        counts["tag_alias"] = migrate_tag_alias(src, cur)
        counts["search_history"] = migrate_search_history(src, cur)
        counts["setting"] = migrate_setting(src, cur)

        # 兜底: recompute reference_count from the migrated novel.tags (set-based,
        # one pass over novel; per-novel dedup keeps each tag counted once).
        cur.execute(
            """
            UPDATE tag t
            SET reference_count = COALESCE(c.cnt, 0)
            FROM (
                SELECT tn.name AS name, count(DISTINCT n.id) AS cnt
                FROM novel n, unnest(n.tags) AS tn(name)
                GROUP BY tn.name
            ) c
            WHERE t.name = c.name
            """
        )

        pg.commit()
    except Exception:
        pg.rollback()
        raise
    finally:
        cur.close()
        pg.close()
        src.close()

    for name in ("author", "series", "novel", "novel_search", "failed_novel",
                 "tag", "tag_alias", "tag_preference", "scheduled_task",
                 "task_history", "token", "setting", "search_history"):
        print(f"  {name:<16} {counts.get(name, 0):>8} rows")
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
