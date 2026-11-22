"""Typed NamedTuple records shared between PostgreSQL snapshots and the Rust engine."""

from __future__ import annotations

from typing import NamedTuple


class SlimNovelRecord(NamedTuple):
    """16-column slim projection tuple consumed directly by ``copixiv_rs``."""

    id: int
    title: str
    author_id: int | None
    author_name: str | None
    series_id: int | None
    series_name: str | None
    series_index: int | None
    text: int
    like: int
    view: int
    has_epub: int
    create_time_us: int | None
    source: str
    is_favourite: bool
    is_special_follow: bool
    tags: list[str]


class SlimTagRecord(NamedTuple):
    """3-column tag tuple ``(id, name, reference_count)`` consumed by ``copixiv_rs``."""

    id: int
    name: str
    reference_count: int


__all__ = [
    "SlimNovelRecord",
    "SlimTagRecord",
]
