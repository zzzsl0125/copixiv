"""Startup snapshot builder and serialized post-commit delta synchronization."""

from __future__ import annotations

import gc
import threading
import time
from collections.abc import Callable
from typing import Any

from copixiv.log import logger
from copixiv.query_engine.adapter import RustNovelQueryEngine


def build_engine_from_store(
    store: Any,
    *,
    engine: RustNovelQueryEngine | None = None,
) -> RustNovelQueryEngine:
    """Populate *engine* (or a new one) from *store* (e.g. ``PgSnapshotStore``) via a staged atomic swap."""
    t0 = time.perf_counter()
    target = engine or RustNovelQueryEngine()
    staging_holder: list[Any] = []
    loaded = 0

    def _on_total(total: int) -> None:
        staging_holder.append(target.create_staging(capacity=total))

    def _on_batch(batch) -> None:
        nonlocal loaded
        staging_holder[0].push_batch(batch)
        loaded += len(batch)

    blocked_tags, exclude_default, all_tags, aliased_tag_ids = store.stream_snapshot(
        _on_total,
        _on_batch,
        batch_size=2_000,
    )
    target.swap_from_staging(
        staging_holder.pop(),
        blocked_tags,
        exclude_default,
        all_tags,
        aliased_tag_ids,
    )
    del blocked_tags, all_tags, aliased_tag_ids
    gc.collect()
    elapsed_ms = (time.perf_counter() - t0) * 1_000
    logger.info(
        "Rust query engine ready: docs={} blocked_tags={} exclude_default={} ({:.1f} ms)",
        loaded,
        target.stats()[2],
        exclude_default,
        elapsed_ms,
    )
    return target


def make_post_commit_hook(
    store: Any,
    engine: RustNovelQueryEngine,
    sync_lock: threading.Lock | None = None,
) -> Callable[[Any], None]:
    """Return a thread-safe post-commit hook reading latest committed rows."""
    lock = sync_lock or threading.Lock()

    def _on_commit(delta: Any) -> None:
        with lock:
            if delta.is_empty() and not engine.needs_rebuild():
                return
            try:
                if engine.needs_rebuild():
                    build_engine_from_store(store, engine=engine)
                    return

                payload = store.fetch_delta_payload(delta)
                has_changes = bool(
                    payload.delete_ids
                    or payload.upsert_rows
                    or payload.blocked_tags is not None
                    or payload.exclude_blocked_default is not None
                    or payload.special_follow_changes
                    or payload.delete_tag_names is not None
                    or payload.upsert_tags is not None
                    or payload.aliased_tag_ids is not None
                )
                if has_changes:
                    engine.apply_delta(
                        delete_ids=payload.delete_ids,
                        upsert_rows=payload.upsert_rows,
                        blocked_tags=payload.blocked_tags,
                        exclude_blocked_default=payload.exclude_blocked_default,
                        special_follow_changes=payload.special_follow_changes,
                        delete_tag_names=payload.delete_tag_names,
                        upsert_tags=payload.upsert_tags,
                        aliased_tag_ids=payload.aliased_tag_ids,
                    )
            except Exception:
                logger.exception("Post-commit delta sync failed; attempting full engine rebuild")
                engine.mark_needs_rebuild()
                try:
                    build_engine_from_store(store, engine=engine)
                except Exception:
                    logger.exception(
                        "Self-healing full engine rebuild also failed; engine marked needs_rebuild"
                    )

    return _on_commit


def attach_and_start_engine(
    store: Any,
    *,
    build_initial: bool = True,
) -> RustNovelQueryEngine:
    """Create, optionally build, and attach a query engine + hook onto *store*."""
    sync_lock = threading.Lock()
    engine = RustNovelQueryEngine()
    if build_initial:
        build_engine_from_store(store, engine=engine)
    hook = make_post_commit_hook(store, engine, sync_lock=sync_lock)
    store.attach_runtime(engine, hook)
    return engine
