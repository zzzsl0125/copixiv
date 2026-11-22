"""本地书的时间轴坐标 —— ``id = -(坐标)``，坐标 = 上传时刻的 pixiv 作品号位置。

契约见 ``docs/LIBRARY_DESIGN.md`` §3.1。要点三条：

1. **符号管身份，模管时间。** pixiv 行的 id 就是作品号（发布序，全库最细的时钟：
   实测 ≈10 秒/号）；本地行没有作品号，于是取一个「上传时刻的坐标」——已抓取的
   最新作品号 + 1——再把坐标取负。负数区间是本地行独占的命名空间（pixiv 作品号
   恒为正），所以**永不撞主键**；而排序只要用 ``abs(id)`` 就回到同一根时间轴
   （``features/novels/repo.py`` 的三处 id 排序即此语义）。

   为什么不能直接用正数：pixiv 作品号是密集顺序发放的（≈8,500 号/天），本库只
   收了全站的 0.8%，正数区间里 99.2% 的号是「别人的、只是还没抓下来」。本地行
   占任何一个正数号，迟早会被真实作品认领 —— 而抓取写入是按 id 命中就地覆盖
   （``novels/repo.py::_upsert_rows``），那等于把这本书改名换姓。

2. **单调不复用。** 坐标游标放在专用序列 ``novel_local_coord_seq`` 上：序列不回退
   （``nextval`` 与事务无关，回滚也消耗号），所以删掉最新那本再上传，新书拿到的是
   **更大**的坐标（id 更小）—— id 是用户可见的（DSL ``id:``、导出 ``{id}``），
   复用会把旧引用指向另一本书。

3. **水位只向前推。** 坐标 = ``max(pixiv 水位 + 1, 序列游标)``。抓取落后时本地书
   落在水位之上（语义：上传发生在「已知最新作品」之后）；抓取追上来后，新作品
   自然排到它前面 —— 误差上限就是一个抓取延迟。

``id = -(坐标)`` 与旧锚点 ``id = -(10**9) + n`` 的区别只有一条：**模不再是计数器**。
旧式的模没有时间含义，``abs(id)`` 会把本地书顶到全库最前（10^9 ≫ 3×10^7）；迁移
0006 因此要给存量本地行重编号（见该迁移的 ``_renumber``）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy import text as sa_text

from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV
from copixiv.db import models

# 坐标游标（迁移 0005 建的 ``novel_local_id_seq``，0006 改名并改为承载坐标）。
COORD_SEQUENCE = "novel_local_coord_seq"
ID_LOCK_KEY = "copixiv.library.local_novel_id"

# ---------------------------------------------------------------------------
# 空轴种子：pixiv 侧一行都没有时的兜底
# ---------------------------------------------------------------------------

# 只在「库里还没有任何 pixiv 行」时使用（新装、先上传后抓取）：此时没有水位
# 可参照，只能按时间线性外推作品号，否则本地书会被钉在 id 轴起点（2012 年），
# 等第一次抓取落地就永远沉底。
#
# 校准值来自线上库（2026-09-18）：最新作品号 29,155,910 / create_time
# 2026-09-17T15:00Z，近 90 天实测增速 8,509 号/天。增速会漂移，但空轴场景下
# 的量级误差（几天的作品号 ≈ 几万）远小于「钉在 2012」。
SEED_ID = 29_155_910
SEED_AT = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)
SEED_IDS_PER_DAY = 8509.0


def seed_coord(now: datetime | None = None) -> int:
    """空轴兜底：把时刻线性外推成作品号坐标（恒 ≥ 1）。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    days = (now - SEED_AT).total_seconds() / 86400.0
    return max(1, int(SEED_ID + days * SEED_IDS_PER_DAY))


# ---------------------------------------------------------------------------
# 发号
# ---------------------------------------------------------------------------


def pixiv_watermark(session) -> int | None:
    """已抓取的最大 pixiv 作品号；一行都没有时 ``None``。

    走 ``novel_pkey`` 反向索引扫描（最新的号全是 pixiv 行），代价可忽略。
    必须限定 ``source='pixiv'``：本地 id 是负数，不筛来源时 ``max(id)`` 会
    取到本地行，取负后翻成正数 —— 那就踩进 pixiv 号段了。
    """
    return session.execute(
        select(func.max(models.Novel.id)).where(models.Novel.source == SOURCE_PIXIV)
    ).scalar()


def lock_local_ids(session) -> None:
    """获取事务级咨询锁，串行化本地书的去重检查、发号与落库。"""
    session.execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": ID_LOCK_KEY},
    )


def next_local_id(session, now: datetime | None = None, *, lock: bool = True) -> int:
    """下一个本地书 id（负数）。发号在写事务内进行（2 条 SQL）。

    调用方若已在当前事务持有 :func:`lock_local_ids` 咨询锁，可传 ``lock=False``
    省去重复加锁；独立调用时默认 ``lock=True``，确保并发 ``setval`` 不撞号。
    """
    if lock:
        lock_local_ids(session)
    watermark = pixiv_watermark(session)
    floor = watermark + 1 if watermark is not None else seed_coord(now)
    coord = int(
        session.execute(
            sa_text(
                f"SELECT setval('{COORD_SEQUENCE}', GREATEST(nextval('{COORD_SEQUENCE}'), :floor), true)"
            ),
            {"floor": floor},
        ).scalar_one()
    )
    return -coord


__all__ = [
    "COORD_SEQUENCE",
    "ID_LOCK_KEY",
    "SOURCE_LOCAL",
    "SOURCE_PIXIV",
    "lock_local_ids",
    "next_local_id",
    "pixiv_watermark",
    "seed_coord",
]
