"""Timestamp normalization, epoch-microsecond conversion, and calendar windows."""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_JST = ZoneInfo("Asia/Tokyo")
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def parse_pixiv_time(value: str | datetime | None) -> datetime | None:
    """Normalise a Pixiv source timestamp to an aware UTC ``datetime``.

    Naive values (e.g. ``YYYY-MM-DD``) are interpreted in ``Asia/Tokyo`` before
    converting to UTC. Returns ``None`` for empty or unparseable input.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_JST)
    return dt.astimezone(UTC)


def datetime_to_us(dt: datetime | None) -> int | None:
    """Convert a Python ``datetime`` to exact integer UTC microseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    dt_utc = dt.astimezone(UTC)
    delta = dt_utc - _UTC_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def us_to_datetime(us: int | None) -> datetime | None:
    """Convert integer UTC microseconds back to a UTC ``datetime``."""
    if us is None:
        return None
    return _UTC_EPOCH + timedelta(microseconds=us)


def month_ranges(end_date: datetime, months: int) -> Iterator[tuple[datetime, datetime]]:
    """Yield ``(start, end)`` for the last *months* calendar windows.

    Uses O(1) ``divmod`` month arithmetic and preserves ``end_date.tzinfo`` on
    every boundary.
    """
    tz = end_date.tzinfo
    base_total = end_date.year * 12 + (end_date.month - 1)
    for offset in range(months):
        if offset == 0:
            yield end_date.replace(day=1), end_date
            continue
        year, zero_month = divmod(base_total - offset, 12)
        month = zero_month + 1
        last_day = calendar.monthrange(year, month)[1]
        yield (
            datetime(year, month, 1, tzinfo=tz),
            datetime(year, month, last_day, tzinfo=tz),
        )


__all__ = ["datetime_to_us", "month_ranges", "parse_pixiv_time", "us_to_datetime"]
