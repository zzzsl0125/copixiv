"""Startup snapshot builder and serialized post-commit delta synchronization."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from copixiv.log import logger
from copixiv.query_engine.adapter import RustNovelQueryEngine
from copixiv.query_engine.port import (
    EngineMode,
    PendingDeltaProtocol,
    SnapshotStoreProtocol,
)


def build_engine_from_store(
    store: SnapshotStoreProtocol,
    *,
    mode: EngineMode = "rust",
    engine: RustNovelQueryEngine | None = None,
) -> RustNovelQueryEngine:
    """Populate *engine* (or a new one) via a staged atomic swap."""
    t0 = time.perf_counter()
    target = engine or RustNovelQueryEngine(mode=mode)
    staging_holder: list[Any] = []
    loaded = 0

    def _on_total(total: int) -> None:
        staging_holder.append(target.create_staging(capacity=total))

    def _on_batch(batch) -> None:
        nonlocal loaded
        staging_holder[0].push_batch(batch)
        loaded += len(batch)

    blocked_tags, exclude_default, sf_authors = store.stream_snapshot(
        _on_total, _on_batch, batch_size=25_000,
    )
    target.swap_from_staging(staging_holder[0], blocked_tags, exclude_default, sf_authors)
    elapsed_ms = (time.perf_counter() - t0) * 1_000
    logger.info(
        "Rust query engine ready: mode={} docs={} blocked_tags={} exclude_default={} ({:.1f} ms)",
        target.mode,
        loaded,
        len(blocked_tags),
        exclude_default,
        elapsed_ms,
    )
    return target


def rebuild_engine(
    store: SnapshotStoreProtocol,
    engine: RustNovelQueryEngine,
    *,
    sync_lock: threading.Lock | None = None,
) -> None:
    """Perform a full atomic rebuild of *engine* from *store*."""
    if sync_lock is not None:
        with sync_lock:
            build_engine_from_store(store, mode=engine.mode, engine=engine)
    else:
        build_engine_from_store(store, mode=engine.mode, engine=engine)


def make_post_commit_hook(
    store: SnapshotStoreProtocol,
    engine: RustNovelQueryEngine,
    sync_lock: threading.Lock | None = None,
) -> Callable[[PendingDeltaProtocol], None]:
    """Return a thread-safe post-commit hook reading latest committed rows."""
    lock = sync_lock or threading.Lock()

    def _on_commit(delta: PendingDeltaProtocol) -> None:
        if delta.is_empty():
            return
        with lock:
            if delta.deleted_novel_ids:
                engine.delete_ids(list(delta.deleted_novel_ids))

            pending_upserts = set(delta.upsert_novel_ids) - set(delta.deleted_novel_ids)
            if (
                pending_upserts
                or delta.blocked_tags_dirty
                or delta.exclude_setting_dirty
            ):
                rows, blocked_tags, exclude_default = store.fetch_delta_payload(
                    upsert_ids=list(pending_upserts),
                    fetch_blocked=delta.blocked_tags_dirty,
                    fetch_exclude=delta.exclude_setting_dirty,
                )
                if pending_upserts:
                    found_ids = {r[0] for r in rows}
                    missing_ids = pending_upserts - found_ids
                    if missing_ids:
                        engine.delete_ids(list(missing_ids))
                    if rows:
                        engine.upsert_batch(rows)
                if blocked_tags is not None:
                    engine.set_blocked_tags(blocked_tags)
                if exclude_default is not None:
                    engine.set_exclude_blocked_default(exclude_default)

            for author_id, flag in delta.special_follow_changes.items():
                engine.toggle_special_follow(author_id, flag)

    return _on_commit


def attach_and_start_engine(
    store: SnapshotStoreProtocol,
    *,
    mode: EngineMode = "rust",
    build_initial: bool = True,
) -> RustNovelQueryEngine:
    """Create, optionally build, and attach a query engine + hook onto *store*."""
    sync_lock = threading.Lock()
    engine = RustNovelQueryEngine(mode=mode)
    if build_initial:
        build_engine_from_store(store, mode=mode, engine=engine)
    hook = make_post_commit_hook(store, engine, sync_lock=sync_lock)
    store.attach_runtime(engine, hook)
    return engine
