#!/usr/bin/env python3
"""Test novel ingestion using the registered task system.

Runs any ``@register``-ed task against an isolated test database and
download directory, so you can verify that ingestion, search, ranking,
and maintenance tasks work correctly without touching production data.

Usage::

    # List all available tasks:
    python scripts/test_ingest.py --list

    # Run a task with parameters (key=value pairs):
    python scripts/test_ingest.py novel_fetch id=12345678
    python scripts/test_ingest.py novel_fetch id=12345678 redownload=false
    python scripts/test_ingest.py author_fetch author_id=12345 force=true
    python scripts/test_ingest.py novel_search keyword=R-18 months=1 minlike=500
    python scripts/test_ingest.py novel_follow days=3 force=true
    python scripts/test_ingest.py novel_ranking mode=daily_r18 days=2
    python scripts/test_ingest.py author_delete author_id=12345
    python scripts/test_ingest.py rebuild_fts
    python scripts/test_ingest.py check_fts
    python scripts/test_ingest.py check_epub
    python scripts/test_ingest.py sync_empty_name

    # Dry-run: show what would be executed without making API calls:
    python scripts/test_ingest.py --dry-run novel_fetch id=12345678

    # Show what's currently in the test database:
    python scripts/test_ingest.py --summary

    # Delete ALL test data so the next run starts fresh:
    python scripts/test_ingest.py --reset

    # Delete and recreate the database only (keep downloads):
    python scripts/test_ingest.py --reset-db

Test data lives in ``./test_env/``::

    test_env/
    ├── database/
    │   └── test.db          # SQLite database (empty on first run)
    └── download/             # novel text files, images, EPUBs

All other configuration (Pixiv proxy, tokens, Telegram, …) comes from
``config.yaml``.  Only ``path.database`` and ``path.download`` are
overridden via ``COPIXIV_`` environment variables so your production
data is never at risk.
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import inspect
import asyncio
from pathlib import Path
from typing import Any

# ── Resolve paths ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = PROJECT_ROOT / "test_env"
TEST_DB = TEST_DIR / "database" / "test.db"
TEST_DOWNLOAD = TEST_DIR / "download"

os.chdir(PROJECT_ROOT)

# ── Env overrides BEFORE importing copixiv ────────────────────────────────
os.environ["COPIXIV_PATH__DATABASE"] = str(TEST_DB)
os.environ["COPIXIV_PATH__DOWNLOAD"] = str(TEST_DOWNLOAD)

# Ensure pixiv_token.py exists (Container fallback for account loading)
TOKEN_PY = PROJECT_ROOT / "pixiv_token.py"
TOKEN_JSON = PROJECT_ROOT / "pixiv_token"
if not TOKEN_PY.exists() and TOKEN_JSON.exists():
    _data = json.loads(TOKEN_JSON.read_text())
    _tokens = [
        {
            "token": _info["token"],
            "username": _username,
            "premium": _info.get("premium", False),
            "valid": True,
        }
        for _username, _info in _data.items()
    ]
    TOKEN_PY.write_text(
        f"TOKENS = {json.dumps(_tokens, indent=4, ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    print(f"[setup] Created {TOKEN_PY.name} from {TOKEN_JSON.name}")

# ── Now safe to import copixiv ────────────────────────────────────────────
from copixiv.log import setup_logging
setup_logging()

from copixiv.app import _build
from copixiv.db import models
from copixiv.db.engine import create_database_engine, create_session_factory
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import DbWriteLock
from copixiv.tasks.kernel import TaskContext
from copixiv.tasks.kernel import describe_tasks, discover_tasks, get_spec

# Trigger task registration (entry points with built-in fallback)
discover_tasks()


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _parse_bool(value: str) -> bool:
    """Convert a CLI string to bool."""
    return value.lower() in ("true", "yes", "1", "t", "y")


def _coerce_value(value: str, target_type: type) -> Any:
    """Convert a CLI string to the target Python type."""
    if target_type is bool:
        return _parse_bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value  # str or unknown — keep as-is


def _parse_params(task_name: str, raw_params: list[str]) -> dict[str, Any]:
    """Parse ``key=value`` strings into a typed dict based on the task spec.

    Parameter types come from the task's Pydantic args model (docs/
    MODULARITY.md §M8).  Unknown parameters are passed through as strings.
    """
    spec = get_spec(task_name)
    if spec is None:
        raise SystemExit(f"Unknown task: {task_name!r}")

    if spec.args_model is None:
        if raw_params:
            print("[warn] Task takes no parameters — ignoring provided params")
        return {}

    known_types: dict[str, type] = {
        name: info.annotation
        for name, info in spec.args_model.model_fields.items()
    }

    params: dict[str, Any] = {}
    for raw in raw_params:
        if "=" not in raw:
            print(f"[warn] Skipping malformed param {raw!r} (expected key=value)")
            continue
        key, value = raw.split("=", 1)
        if key in known_types:
            params[key] = _coerce_value(value, known_types[key])
        else:
            # Unknown param — try int, then float, then bool, else str
            params[key] = _coerce_guess(value)

    return params


def _coerce_guess(value: str) -> Any:
    """Best-effort type coercion for unknown parameters."""
    if value.lower() in ("true", "false", "yes", "no"):
        return _parse_bool(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _injected_deps(app_singletons):
    """Return the dependency dict that tasks expect."""
    return {
        "client": app_singletons.client,
        "file_storage": app_singletons.file_storage,
        "image_downloader": app_singletons.image_downloader,
        "epub_builder": app_singletons.epub_builder,
        "config": app_singletons.config,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════

def cmd_list() -> None:
    """Print all available registered tasks with their arguments."""
    print("Available tasks:\n")
    for m in describe_tasks():
        params = []
        for a in m["arguments"]:
            if a["required"]:
                params.append(a["name"])
            else:
                params.append(f"{a['name']}={a['default']!r}")
        first_line = (m["description"] or "").splitlines()[0] if m["description"] else ""
        print(f"  {m['name']}({', '.join(params)})  — {first_line}")
    print()


def cmd_reset(drop_db_only: bool = False) -> None:
    """Delete test data."""
    if drop_db_only:
        db_file = TEST_DIR / "database" / "test.db"
        for suffix in ("", "-shm", "-wal"):
            p = Path(str(db_file) + suffix)
            if p.exists():
                p.unlink()
                print(f"[reset] Deleted {p}")
        # Also delete backups
        backups_dir = TEST_DIR / "database" / "backups"
        if backups_dir.exists():
            shutil.rmtree(backups_dir)
            print(f"[reset] Deleted {backups_dir}")
        print("[reset] Database deleted. Download files preserved.")
    else:
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR)
            print(f"[reset] Deleted {TEST_DIR}")
        else:
            print("[reset] Nothing to delete — test_env/ does not exist.")


def cmd_summary() -> None:
    """Print a summary of what's in the test database and download dir."""
    if not TEST_DB.exists():
        print("[summary] No test database found. Run a task first.")
        return

    engine = create_database_engine(str(TEST_DB))
    sf = create_session_factory(engine)

    with sf() as session:
        novel_count = session.query(models.Novel).count()
        author_count = session.query(models.Author).count()
        series_count = session.query(models.Series).count()
        tag_count = session.query(models.Tag).count()
        fav_count = session.query(models.Favourite).count()

        print(f"[summary] Database: {TEST_DB}")
        print(f"  Novels:   {novel_count}")
        print(f"  Authors:  {author_count}")
        print(f"  Series:   {series_count}")
        print(f"  Tags:     {tag_count}")
        print(f"  Favs:     {fav_count}")

        if novel_count > 0:
            print("\n  Recent novels:")
            novels = (
                session.query(models.Novel)
                .order_by(models.Novel.id.desc())
                .limit(10)
                .all()
            )
            for n in novels:
                tag_names = [t.name for t in n.tags[:5]]
                tags_str = ", ".join(tag_names)
                if len(n.tags) > 5:
                    tags_str += f" +{len(n.tags) - 5} more"
                author = n.author_name or f"#{n.author_id}"
                print(f"    #{n.id}  {n.title[:55]:55s}  by {author}  [{tags_str}]")

    engine.dispose()

    if TEST_DOWNLOAD.exists():
        txt_count = len(list(TEST_DOWNLOAD.rglob("*.txt")))
        epub_count = len(list(TEST_DOWNLOAD.rglob("*.epub")))
        print(f"\n[summary] Download dir: {TEST_DOWNLOAD}")
        print(f"  .txt files:  {txt_count}")
        print(f"  .epub files: {epub_count}")


# ═══════════════════════════════════════════════════════════════════════════
# Core: run a task
# ═══════════════════════════════════════════════════════════════════════════

def run_task(task_name: str, params: dict[str, Any], dry_run: bool = False) -> Any:
    """Build the container, look up the task spec, inject deps via
    TaskContext, and execute.

    Returns the task's return value.
    """
    spec = get_spec(task_name)
    if spec is None:
        raise SystemExit(f"Unknown task: {task_name!r}")

    # Validate required params
    if spec.args_model is not None:
        for name, info in spec.args_model.model_fields.items():
            if info.is_required() and name not in params:
                raise SystemExit(
                    f"Missing required parameter: {name}\n"
                    f"  Usage: {task_name} "
                    f"{' '.join(f'{n}=<value>' for n, i in spec.args_model.model_fields.items() if i.is_required())}"  # noqa: E501
                )

    if dry_run:
        print(f"[dry-run] Task:     {task_name}")
        print(f"[dry-run] Params:   {json.dumps(params, ensure_ascii=False)}")
        print(f"[dry-run] Test DB:  {TEST_DB}")
        print(f"[dry-run] Download: {TEST_DOWNLOAD}")
        print("\n[dry-run] Would execute task with these parameters.")
        print("[dry-run] Dry run — no API calls made, container not built.")
        return None

    # -- Prepare directories -------------------------------------------------
    TEST_DB.parent.mkdir(parents=True, exist_ok=True)
    TEST_DOWNLOAD.mkdir(parents=True, exist_ok=True)

    print(f"[setup] Test DB:      {TEST_DB}")
    print(f"[setup] Download dir: {TEST_DOWNLOAD}")
    print(f"[setup] Task:         {task_name}")
    print(f"[setup] Params:       {json.dumps(params, ensure_ascii=False)}")
    print()

    # -- Build composition root ---------------------------------------------
    print("[build] Building container (DB + migrations + accounts)...")
    singletons = _build()
    print("[build] Container ready.\n")

    # -- Run the task --------------------------------------------------------
    deps = _injected_deps(singletons)
    uow = SqlUnitOfWork(singletons.session_factory)

    ctx = TaskContext(
        uow=uow,
        session_factory=singletons.session_factory,
        client=deps.get("client"),
        file_storage=deps.get("file_storage"),
        image_downloader=deps.get("image_downloader"),
        epub_builder=deps.get("epub_builder"),
        config=deps.get("config"),
        write_lock=DbWriteLock(),
    )

    args_obj = (
        spec.args_model.model_validate(params)
        if spec.args_model is not None
        else None
    )

    print(f"[run] Executing {task_name}...")
    print(f"[run] Params: {json.dumps(params, ensure_ascii=False)}")
    print()

    if args_obj is None:
        result = asyncio.run(spec.func(ctx=ctx))
    else:
        result = asyncio.run(spec.func(args_obj, ctx=ctx))

    # -- Show result ---------------------------------------------------------
    print(f"\n[result] Return value: {_format_result(result)}")
    print()

    # -- Summary -------------------------------------------------------------
    cmd_summary()

    return result




def _format_result(result: Any) -> str:
    """Format a task return value for display."""
    if result is None:
        return "None"
    if isinstance(result, list):
        if len(result) <= 10:
            return f"list[{len(result)}]: {result}"
        return f"list[{len(result)}]: {result[:5]} ... +{len(result) - 5} more"
    if isinstance(result, int):
        return str(result)
    return str(result)[:200]


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def _usage() -> None:
    print(__doc__)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _usage()
        sys.exit(1)

    # -- Special commands (no container needed) ------------------------------
    first = sys.argv[1]

    if first in ("-h", "--help"):
        _usage()
        sys.exit(0)

    if first == "--list":
        cmd_list()
        sys.exit(0)

    if first == "--reset":
        cmd_reset(drop_db_only=False)
        sys.exit(0)

    if first == "--reset-db":
        cmd_reset(drop_db_only=True)
        sys.exit(0)

    if first == "--summary":
        cmd_summary()
        sys.exit(0)

    # -- Dry-run mode -------------------------------------------------------
    dry_run = False
    args = list(sys.argv[1:])
    if "--dry-run" in args:
        dry_run = True
        args.remove("--dry-run")

    if not args:
        _usage()
        sys.exit(1)

    task_name = args[0]
    raw_params = args[1:]

    params = _parse_params(task_name, raw_params)
    run_task(task_name, params, dry_run=dry_run)
