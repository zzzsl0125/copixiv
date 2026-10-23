"""``core/calendar.py`` —— 把「最近 N 个月」拆成日历窗口。

只被搜索任务用（按月问 pixiv）。这条规则原本住在
``features/novels/ingest.py``，任务层得去 import 另一个切片的私有函数 ——
它不认识 novel / pixiv，只认识月初与月末。
"""

from __future__ import annotations

from datetime import datetime

from copixiv.core.calendar import month_ranges


class TestMonthRanges:
    def test_the_first_window_runs_from_the_first_of_the_month_to_today(self):
        windows = list(month_ranges(datetime(2026, 10, 24), 1))

        assert windows == [(datetime(2026, 10, 1), datetime(2026, 10, 24))]

    def test_later_windows_are_whole_calendar_months_newest_first(self):
        windows = list(month_ranges(datetime(2026, 10, 24), 3))

        assert windows == [
            (datetime(2026, 10, 1), datetime(2026, 10, 24)),
            (datetime(2026, 9, 1), datetime(2026, 9, 30)),
            (datetime(2026, 8, 1), datetime(2026, 8, 31)),
        ]

    def test_it_crosses_a_year_boundary(self):
        windows = list(month_ranges(datetime(2026, 1, 15), 3))

        assert windows[1] == (datetime(2025, 12, 1), datetime(2025, 12, 31))
        assert windows[2] == (datetime(2025, 11, 1), datetime(2025, 11, 30))

    def test_a_leap_february_gets_29_days(self):
        windows = list(month_ranges(datetime(2026, 3, 10), 2))

        assert windows[1][1] == datetime(2026, 2, 28)   # 2026 不是闰年

        windows = list(month_ranges(datetime(2028, 3, 10), 2))
        assert windows[1][1] == datetime(2028, 2, 29)   # 2028 是

    def test_zero_months_yields_nothing(self):
        assert list(month_ranges(datetime(2026, 10, 24), 0)) == []

    def test_the_windows_do_not_overlap(self):
        windows = list(month_ranges(datetime(2026, 10, 24), 6))

        for (_, earlier_end), (next_start, _) in zip(windows, windows[1:]):
            assert next_start < earlier_end
