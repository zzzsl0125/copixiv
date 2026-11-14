"""死系列台账 —— 「pixiv 侧已不存在」的可判定终态。

**它解决什么问题。** ``fix_series_index`` 的候选集是「``series_id`` 有值、
``series_index`` 为空」的系列，而这个谓词**无法区分**「这一轮拉不到」与
「永远拉不到」。系列在 pixiv 侧被删之后（``novel_series`` 稳定 404），本地
``novel.series_id`` 仍指着它，于是它每一轮都重新成为候选、每一轮都重新发一次
必然失败的请求。生产实测：

| 轮次 | 候选 | 其中 404 | 有效产出 | 耗时 |
|---|---:|---:|---:|---:|
| 2026-10-22 | 200 | **183** | 补号 0–47 | 1740 s |
| 2026-10-23 | 82 | **49** | 补号 26 | 481 s |

60–92% 的预算烧在注定拿不到任何东西的系列上，而候选集总共只有 462 个系列。
加预算治不了，因为病根是**候选集里混着死数据**。

**判据：连续 N 次「确定性失败」才判死。** 单次 404 不足以判死——pixiv 会在
限流兜底、CDN 回源失败时也回 404，把它当永久删除会误杀还能修的系列。所以：

* **确定性失败** = ``PixivApiError`` 且 ``status_code`` 在
  :data:`DEFINITIVE_STATUSES`（404 / 410）。只有它们累加计数。
* **其它失败**（超时、5xx、限流、解析不出来）**不累加**——那不是「pixiv 说它
  没了」，只是这轮运气不好，不该把系列往死里推。它们仍然计入任务的
  ``stats["failed"]`` 并由游标越过（见 ``series.py``），只是不进台账。
* **任何一次成功**把 ``consecutive_failures`` 归零、``retired_at`` 清空。判死是
  可逆的：pixiv 侧恢复（系列被重建 / 当初其实是误判）时，只要有一轮跑到
  ``sweep_all``、或手动重跑任务，它就会自己复活。

**台账在哪个环节生效。** 只有候选查询：``series_with_empty_index()`` /
``series_with_stored_index()`` 排除 ``retired_at IS NOT NULL`` 的系列。
``sweep_all`` 也尊重它（否则全量复查会把死系列重新烧一遍），但``sweep_all`` 的
候选**默认关闭**，所以「复活」的途径是显式打开全量复查或清掉台账行——
这正是「写一个标记后续不再处理」应有的可逆性，而不是不可撤销的删除。

为什么是独立表而不是 ``series`` 上的一列：见 :class:`copixiv.db.models.
SeriesDeadLetter` 的 docstring。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete as _delete
from sqlalchemy import select as _select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from copixiv.db import models
from copixiv.log import logger

# 连续多少轮「确定性失败」判死。3 轮 ≈ 连续三夜，足以过滤掉单次的 pixiv 抖动，
# 又不至于把一个刚被原作者删掉的系列拖上几周。
RETIRE_AFTER_FAILURES = 3

# 这两种状态码是 pixiv 在**明确**说「这个资源不存在」。其余 4xx（401 鉴权、
# 403 受限、429 限流）都不算：它们描述的是「这次访问不被允许」，不是「东西没了」。
DEFINITIVE_STATUSES = frozenset({404, 410})


def is_definitive_error(exc: BaseException) -> bool:
    """True when *exc* is pixiv definitively saying the series is gone.

    Imported lazily to keep this module free of the pixiv adapter at import
    time (``features`` may not be imported by ``db``; the reverse is fine but
    we do not need it).
    """
    from copixiv.pixiv.errors import PixivApiError

    if not isinstance(exc, PixivApiError):
        return False
    return getattr(exc, "status_code", None) in DEFINITIVE_STATUSES


def note_failure(
    session, series_id: int, exc: BaseException | None,
) -> bool:
    """Record one failed roster fetch; return True when this retires the series.

    Only definitive failures advance the counter — a timeout or a 5xx says
    nothing about whether the series still exists, and letting them count
    would retire healthy series during a bad night on pixiv's side.

    Writes with an ``ON CONFLICT`` upsert so concurrent fan-out writes for the
    same series cannot collide, and so the first failure for a series does not
    need a separate existence check.
    """
    if not is_definitive_error(exc):
        return False

    status = getattr(exc, "status_code", None)
    detail = f"HTTP {status}: {exc}"
    now = datetime.now(timezone.utc)

    stmt = (
        pg_insert(models.SeriesDeadLetter)
        .values(
            series_id=series_id,
            consecutive_failures=1,
            last_error=detail,
            last_failed_at=now,
        )
        .on_conflict_do_update(
            index_elements=[models.SeriesDeadLetter.series_id],
            # ``+ 1`` reads the *stored* value (the excluded row only carries
            # the insert candidate), and ``retired_at`` is set in the same
            # statement once the count reaches the threshold — no second
            # round trip, no window where the row is over the limit but not
            # yet retired.
            set_={
                "consecutive_failures":
                    models.SeriesDeadLetter.consecutive_failures + 1,
                "last_error": detail,
                "last_failed_at": now,
                "retired_at": (
                    _case_retire(models.SeriesDeadLetter.consecutive_failures)
                ),
            },
        )
        .returning(models.SeriesDeadLetter.consecutive_failures)
    )
    count = int(session.execute(stmt).scalar_one())

    if count >= RETIRE_AFTER_FAILURES:
        logger.warning(
            f"fix_series_index: 系列 {series_id} 连续 {count} 轮 404，"
            f"已判死并从候选集移除（恢复见 series_dead_letter 文档）"
        )
        return True
    return False


def _case_retire(prior_failures):
    """``retired_at`` for the upsert: set once the *new* count reaches N.

    Expressed in SQL so the decision uses the stored counter rather than a
    Python-side read that could race another writer.
    """
    from sqlalchemy import case, func

    return case(
        (prior_failures + 1 >= RETIRE_AFTER_FAILURES, func.now()),
        else_=models.SeriesDeadLetter.retired_at,
    )


def note_success(session, series_id: int) -> None:
    """Clear a series' ledger entry after a successful roster fetch.

    Unconditional (not "only if retired"): a success also resets the partial
    counter, so a series that failed twice and then worked starts from zero
    again rather than being one bad night away from retirement.  Deleting the
    row is cheaper and more honest than keeping a zeroed one — its absence
    *is* "no verdict".
    """
    session.execute(
        _delete(models.SeriesDeadLetter).where(
            models.SeriesDeadLetter.series_id == series_id
        )
    )


def retired_series_ids(session) -> set[int]:
    """Series currently excluded from the candidate queries."""
    return set(session.execute(
        _select(models.SeriesDeadLetter.series_id).where(
            models.SeriesDeadLetter.retired_at.isnot(None)
        )
    ).scalars().all())


def _all_series_ids(session) -> set[int]:
    """Every series with a ledger row (retired or not) — for tests/inspection."""
    return set(session.execute(
        _select(models.SeriesDeadLetter.series_id)
    ).scalars().all())


__all__ = [
    "DEFINITIVE_STATUSES",
    "RETIRE_AFTER_FAILURES",
    "is_definitive_error",
    "note_failure",
    "note_success",
    "retired_series_ids",
]
