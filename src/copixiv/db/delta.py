"""Transaction-scoped pending delta collection & post-commit hook dispatch.

Stays purely within ``copixiv.db`` so no reverse dependency on
``copixiv.query_engine`` or ``copixiv.features`` exists.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from copixiv.db import models

SESSION_ENGINE_KEY = "query_engine"
SESSION_DELTA_KEY = "pending_engine_delta"
_EXCLUDE_BLOCKED_SETTING_KEY = "exclude_blocked_tag_novels"


@dataclass
class PendingEngineDelta:
    """In-flight writes accumulated inside a single UoW transaction."""

    upsert_novel_ids: set[int] = field(default_factory=set)
    deleted_novel_ids: set[int] = field(default_factory=set)
    special_follow_changes: dict[int, bool] = field(default_factory=dict)
    blocked_tags_dirty: bool = False
    exclude_setting_dirty: bool = False

    def is_empty(self) -> bool:
        return (
            not self.upsert_novel_ids
            and not self.deleted_novel_ids
            and not self.special_follow_changes
            and not self.blocked_tags_dirty
            and not self.exclude_setting_dirty
        )

    def clear(self) -> None:
        self.upsert_novel_ids.clear()
        self.deleted_novel_ids.clear()
        self.special_follow_changes.clear()
        self.blocked_tags_dirty = False
        self.exclude_setting_dirty = False


PostCommitHook = Callable[[PendingEngineDelta], None]

_ENGINE_BY_FACTORY: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_HOOK_BY_FACTORY: weakref.WeakKeyDictionary[Any, PostCommitHook] = weakref.WeakKeyDictionary()
# Fallback for non-weakrefable custom factory callables in tests.
_FALLBACK_RUNTIME: dict[int, tuple[Any, PostCommitHook | None]] = {}


def attach_engine_runtime(
    session_factory: Any,
    engine: Any,
    post_commit_hook: PostCommitHook | None = None,
) -> None:
    """Associate a query engine and optional post-commit hook with *session_factory*."""
    try:
        _ENGINE_BY_FACTORY[session_factory] = engine
        if post_commit_hook is not None:
            _HOOK_BY_FACTORY[session_factory] = post_commit_hook
        else:
            _HOOK_BY_FACTORY.pop(session_factory, None)
    except TypeError:
        _FALLBACK_RUNTIME[id(session_factory)] = (engine, post_commit_hook)


def detach_engine_runtime(session_factory: Any) -> None:
    """Remove any query engine association from *session_factory*."""
    try:
        _ENGINE_BY_FACTORY.pop(session_factory, None)
        _HOOK_BY_FACTORY.pop(session_factory, None)
    except TypeError:
        pass
    _FALLBACK_RUNTIME.pop(id(session_factory), None)


def get_attached_engine(session_factory: Any) -> Any:
    """Return the query engine attached to *session_factory*, or ``None``."""
    try:
        eng = _ENGINE_BY_FACTORY.get(session_factory)
        if eng is not None:
            return eng
    except TypeError:
        pass
    pair = _FALLBACK_RUNTIME.get(id(session_factory))
    return pair[0] if pair is not None else None


def get_post_commit_hook(session_factory: Any) -> PostCommitHook | None:
    """Return the post-commit delta hook for *session_factory*, or ``None``."""
    try:
        hook = _HOOK_BY_FACTORY.get(session_factory)
        if hook is not None:
            return hook
    except TypeError:
        pass
    pair = _FALLBACK_RUNTIME.get(id(session_factory))
    return pair[1] if pair is not None else None


def _get_delta_if_active(session: Session | None) -> PendingEngineDelta | None:
    if session is None or SESSION_ENGINE_KEY not in session.info:
        return None
    if session.info.get(SESSION_ENGINE_KEY) is None:
        return None
    delta = session.info.get(SESSION_DELTA_KEY)
    if delta is None:
        delta = PendingEngineDelta()
        session.info[SESSION_DELTA_KEY] = delta
    return delta


def clear_pending_delta(session: Session | None) -> None:
    """Discard any accumulated delta (called on rollback)."""
    if session is None:
        return
    delta = session.info.pop(SESSION_DELTA_KEY, None)
    if delta is not None:
        delta.clear()


def pop_pending_delta(session: Session | None) -> PendingEngineDelta | None:
    """Remove and return accumulated delta after commit."""
    if session is None:
        return None
    return session.info.pop(SESSION_DELTA_KEY, None)


def record_novel_upserts(session: Session, novel_ids: Iterable[int]) -> None:
    delta = _get_delta_if_active(session)
    if delta is None:
        return
    for nid in novel_ids:
        delta.deleted_novel_ids.discard(nid)
        delta.upsert_novel_ids.add(nid)


def record_novel_deletes(session: Session, novel_ids: Iterable[int]) -> None:
    delta = _get_delta_if_active(session)
    if delta is None:
        return
    for nid in novel_ids:
        delta.upsert_novel_ids.discard(nid)
        delta.deleted_novel_ids.add(nid)


def record_special_follow(
    session: Session, author_id: int, is_special_follow: bool,
) -> None:
    delta = _get_delta_if_active(session)
    if delta is None:
        return
    delta.special_follow_changes[author_id] = bool(is_special_follow)


def record_blocked_tags_dirty(session: Session) -> None:
    delta = _get_delta_if_active(session)
    if delta is not None:
        delta.blocked_tags_dirty = True


def record_exclude_setting_dirty(session: Session) -> None:
    delta = _get_delta_if_active(session)
    if delta is not None:
        delta.exclude_setting_dirty = True


@event.listens_for(Session, "after_flush")
def _collect_orm_delta_after_flush(
    session: Session, _flush_context: Any,
) -> None:
    """Capture ORM-level mutations (insert/update/delete) before commit clears flags."""
    delta = _get_delta_if_active(session)
    if delta is None:
        return

    for obj in session.deleted:
        if isinstance(obj, models.Novel):
            delta.upsert_novel_ids.discard(obj.id)
            delta.deleted_novel_ids.add(obj.id)
        elif isinstance(obj, models.TagPreference):
            delta.blocked_tags_dirty = True
        elif isinstance(obj, models.Setting) and obj.key == _EXCLUDE_BLOCKED_SETTING_KEY:
            delta.exclude_setting_dirty = True

    for obj in session.new:
        if isinstance(obj, models.Novel):
            delta.deleted_novel_ids.discard(obj.id)
            delta.upsert_novel_ids.add(obj.id)
        elif isinstance(obj, models.Author) and obj.is_special_follow:
            delta.special_follow_changes[obj.author_id] = True
        elif isinstance(obj, models.TagPreference):
            delta.blocked_tags_dirty = True
        elif isinstance(obj, models.Setting) and obj.key == _EXCLUDE_BLOCKED_SETTING_KEY:
            delta.exclude_setting_dirty = True

    for obj in session.dirty:
        if isinstance(obj, models.Novel):
            if session.is_modified(obj, include_collections=False):
                delta.deleted_novel_ids.discard(obj.id)
                delta.upsert_novel_ids.add(obj.id)
        elif isinstance(obj, models.Author):
            state = inspect(obj)
            if state.attrs.is_special_follow.history.has_changes():
                delta.special_follow_changes[obj.author_id] = bool(obj.is_special_follow)
        elif isinstance(obj, models.TagPreference):
            delta.blocked_tags_dirty = True
        elif isinstance(obj, models.Setting) and obj.key == _EXCLUDE_BLOCKED_SETTING_KEY:
            delta.exclude_setting_dirty = True
