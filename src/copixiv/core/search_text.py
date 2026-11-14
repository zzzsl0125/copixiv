"""Pure text-normalization & time conversion helpers shared cleanly across layers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SlimNovelTuple = tuple[
    int,         # id
    str,         # title
    int | None,  # author_id
    str | None,  # author_name
    int | None,  # series_id
    str | None,  # series_name
    int | None,  # series_index
    int,         # text
    int,         # like
    int,         # view
    int,         # has_epub
    int | None,  # create_time_us
    str,         # source
    bool,        # is_favourite
    bool,        # is_special_follow
    list[str],   # tags
]


def datetime_to_us(dt: datetime | None) -> int | None:
    """Convert a Python ``datetime`` to exact integer UTC microseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    dt_utc = dt.astimezone(timezone.utc)
    delta = dt_utc - _UTC_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def us_to_datetime(us: int | None) -> datetime | None:
    """Convert integer UTC microseconds back to a UTC ``datetime``."""
    if us is None:
        return None
    return _UTC_EPOCH + timedelta(microseconds=us)


def normalize_search_segments(keyword: str) -> list[str]:
    """Split *keyword* on whitespace, dropping segments with no letter/digit.

    A keyword that collapses to nothing filters nothing: ``"---"`` produces
    an empty list so callers skip the predicate instead of adding an empty match.
    """
    if not keyword:
        return []
    return [
        segment
        for segment in keyword.split()
        if any(ch.isalpha() or ch.isnumeric() for ch in segment)
    ]
