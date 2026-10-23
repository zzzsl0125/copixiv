"""Calendar windows — pure date arithmetic shared by the task layer.

**为什么在 ``core/``**：``month_ranges`` 原本住在
``features/novels/ingest.py``，但唯一的调用者是 ``tasks/novels.py`` 的搜索任务
（"过去 N 个月，一个月一个月地问 pixiv"）。一个纯日期工具住在按小说切片的抓取
管线里，让任务层必须 ``import _month_ranges`` 去拿另一个切片的私有函数。
它不认识 novel、不认识 pixiv、不接受也不返回任何领域对象 —— 只认识月初与月末。

``core`` 保持纯 Python（无框架、无 I/O），这里只有标准库的 ``calendar``。
"""

from __future__ import annotations

import calendar
from datetime import datetime


def month_ranges(end_date: datetime, months: int):
    """Yield ``(start, end)`` for the last *months* calendar windows.

    第一个窗口是 ``end_date`` 所在的月（从月初到 ``end_date`` 本身，即"本月到
    今天"）；之后每个月都是完整的自然月，从新到旧。用来把"最近一年"拆成 12 次
    带日期范围的搜索 —— pixiv 的搜索接口按区间计费，按月拆既能翻页稳定，也
    避免一次请求跨过太多结果。
    """
    for offset in range(months):
        if offset == 0:
            yield end_date.replace(day=1), end_date
            continue
        year, month = end_date.year, end_date.month
        for _ in range(offset):
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        last_day = calendar.monthrange(year, month)[1]
        yield datetime(year, month, 1), datetime(year, month, last_day)


__all__ = ["month_ranges"]
