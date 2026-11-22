"""SQLAlchemy session-event delta tracker and post-commit runtime sync.

Stays inside ``copixiv.db`` so no runtime dependency on ``copixiv.query_engine``
or ``copixiv.features`` exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker

from copixiv.core.search import EXCLUDE_BLOCKED_SETTING_KEY
from copixiv.db import models

SESSION_ENGINE_KEY = "query_engine"
SESSION_DELTA_KEY = "pending_engine_delta"
_SESSION_COMMITTED_KEY = "_engine_tx_committed"
_SESSION_HOOK_KEY = "_query_engine_sync_hook"

_REGISTERED_RUNTIME: dict[
    int, tuple[Callable[[PendingEngineDelta], None], list[tuple[str, Callable[..., Any]]]]
] = {}


@dataclass
class PendingEngineDelta:
    """Dirty entity identifiers / invalidation flags accumulated in one transaction."""

    novel_ids: set[int] = field(default_factory=set)
    author_ids: set[int] = field(default_factory=set)
    aliases_dirty: bool = False
    blocked_tags_dirty: bool = False
    exclude_setting_dirty: bool = False

    def is_empty(self) -> bool:
        return (
            not self.novel_ids
            and not self.author_ids
            and not self.aliases_dirty
            and not self.blocked_tags_dirty
            and not self.exclude_setting_dirty
        )


def attach_engine_runtime(
    session_factory: sessionmaker,
    engine: Any,
    post_commit_hook: Callable[[PendingEngineDelta], None],
) -> None:
    """Bind ``engine`` and four transaction lifecycle listeners to ``session_factory``."""
    detach_engine_runtime(session_factory)

    existing_info = dict(session_factory.kw.get("info") or {})
    existing_info[SESSION_ENGINE_KEY] = engine
    existing_info[_SESSION_HOOK_KEY] = post_commit_hook
    session_factory.configure(info=existing_info)

    def _on_after_flush(session: Session, _flush_context: Any) -> None:
        _collect_flushed_orm_deltas(session)

    def _on_after_commit(session: Session) -> None:
        if session.in_nested_transaction():
            return
        if SESSION_ENGINE_KEY not in session.info:
            return
        session.info[_SESSION_COMMITTED_KEY] = True

    def _on_after_rollback(session: Session) -> None:
        if session.in_nested_transaction():
            return
        session.info.pop(_SESSION_COMMITTED_KEY, None)
        clear_pending_delta(session)

    def _on_after_transaction_end(session: Session, transaction: Any) -> None:
        if getattr(transaction, "nested", False) or getattr(transaction, "parent", None) is not None:
            return
        committed = bool(session.info.pop(_SESSION_COMMITTED_KEY, False))
        pending = pop_pending_delta(session)
        if not committed:
            return
        needs_rebuild = bool(engine.needs_rebuild())
        if (pending is None or pending.is_empty()) and not needs_rebuild:
            return
        post_commit_hook(pending if pending is not None else PendingEngineDelta())

    listeners: list[tuple[str, Callable[..., Any]]] = [
        ("after_flush", _on_after_flush),
        ("after_commit", _on_after_commit),
        ("after_rollback", _on_after_rollback),
        ("after_transaction_end", _on_after_transaction_end),
    ]
    for event_name, fn in listeners:
        event.listen(session_factory, event_name, fn)
    _REGISTERED_RUNTIME[id(session_factory)] = (post_commit_hook, listeners)


def detach_engine_runtime(session_factory: sessionmaker) -> None:
    """Remove lifecycle listeners and ``SESSION_ENGINE_KEY`` from ``session_factory``."""
    existing_info = dict(session_factory.kw.get("info") or {})
    if SESSION_ENGINE_KEY in existing_info or _SESSION_HOOK_KEY in existing_info:
        existing_info.pop(SESSION_ENGINE_KEY, None)
        existing_info.pop(_SESSION_HOOK_KEY, None)
        session_factory.configure(info=existing_info)
    registered = _REGISTERED_RUNTIME.pop(id(session_factory), None)
    if registered is not None:
        _, listeners = registered
        for event_name, fn in listeners:
            if event.contains(session_factory, event_name, fn):
                event.remove(session_factory, event_name, fn)


def get_attached_engine(session_factory: sessionmaker) -> Any | None:
    info = session_factory.kw.get("info") or {}
    return info.get(SESSION_ENGINE_KEY)


def get_or_create_pending_delta(session: Session | None) -> PendingEngineDelta | None:
    """Return per-session pending delta buffer when an engine is attached."""
    if session is None or SESSION_ENGINE_KEY not in session.info:
        return None
    if session.info.get(SESSION_ENGINE_KEY) is None:
        return None
    delta = session.info.get(SESSION_DELTA_KEY)
    if delta is None:
        delta = PendingEngineDelta()
        session.info[SESSION_DELTA_KEY] = delta
    return delta


def pop_pending_delta(session: Session | None) -> PendingEngineDelta | None:
    if session is None:
        return None
    return session.info.pop(SESSION_DELTA_KEY, None)


def clear_pending_delta(session: Session | None) -> None:
    if session is not None:
        session.info.pop(SESSION_DELTA_KEY, None)


def _collect_flushed_orm_deltas(session: Session) -> None:
    pending = get_or_create_pending_delta(session)
    if pending is None:
        return

    for bucket_name, objs in (
        ("new", session.new),
        ("dirty", session.dirty),
        ("deleted", session.deleted),
    ):
        for obj in objs:
            if isinstance(obj, models.Novel):
                if obj.id is not None:
                    pending.novel_ids.add(int(obj.id))
            elif isinstance(obj, models.Author):
                if obj.author_id is not None:
                    if bucket_name in ("new", "deleted"):
                        pending.author_ids.add(int(obj.author_id))
                    else:
                        hist = sa_inspect(obj).attrs.is_special_follow.history
                        if hist.has_changes():
                            pending.author_ids.add(int(obj.author_id))
            elif isinstance(obj, models.TagAlias):
                pending.aliases_dirty = True
            elif isinstance(obj, models.TagPreference):
                if _tag_preference_affects_blocked(obj, bucket_name):
                    pending.blocked_tags_dirty = True
            elif isinstance(obj, models.Setting) and obj.key == EXCLUDE_BLOCKED_SETTING_KEY:
                pending.exclude_setting_dirty = True


def _tag_preference_affects_blocked(obj: models.TagPreference, bucket_name: str) -> bool:
    if bucket_name in ("new", "deleted"):
        return obj.preference == models.TagPreferenceORM.blocked
    insp = sa_inspect(obj)
    pref_hist = insp.attrs.preference.history
    tag_hist = insp.attrs.tag.history
    if not pref_hist.has_changes() and not tag_hist.has_changes():
        return False
    if obj.preference == models.TagPreferenceORM.blocked:
        return True
    return any(val == models.TagPreferenceORM.blocked for val in pref_hist.deleted)


def record_novel_upserts(session: Session | None, novel_ids: Iterable[int]) -> None:
    pending = get_or_create_pending_delta(session)
    if pending is not None:
        pending.novel_ids.update(int(nid) for nid in novel_ids)


record_novel_deletes = record_novel_upserts


def record_authors_dirty(session: Session | None, author_ids: Iterable[int]) -> None:
    pending = get_or_create_pending_delta(session)
    if pending is not None:
        pending.author_ids.update(int(aid) for aid in author_ids)


def record_aliases_dirty(session: Session | None) -> None:
    pending = get_or_create_pending_delta(session)
    if pending is not None:
        pending.aliases_dirty = True


def record_exclude_setting_dirty(session: Session | None) -> None:
    pending = get_or_create_pending_delta(session)
    if pending is not None:
        pending.exclude_setting_dirty = True


def sync_engine_if_needed(session: Session | None, engine: Any | None = None) -> None:
    """Trigger the registered rebuild hook if *engine* is marked ``needs_rebuild``."""
    if session is None:
        return
    eng = engine if engine is not None else session.info.get(SESSION_ENGINE_KEY)
    if eng is None:
        return
    if not eng.needs_rebuild():
        return
    hook = session.info.get(_SESSION_HOOK_KEY)
    if callable(hook):
        hook(PendingEngineDelta())


__all__ = [
    "PendingEngineDelta",
    "SESSION_DELTA_KEY",
    "SESSION_ENGINE_KEY",
    "attach_engine_runtime",
    "clear_pending_delta",
    "detach_engine_runtime",
    "get_attached_engine",
    "get_or_create_pending_delta",
    "pop_pending_delta",
    "record_aliases_dirty",
    "record_authors_dirty",
    "record_exclude_setting_dirty",
    "record_novel_deletes",
    "record_novel_upserts",
    "sync_engine_if_needed",
]
