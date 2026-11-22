"""Local library repository and timeline coordinate allocation for locally uploaded books."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV, EpubStatus
from copixiv.db import models

# 坐标游标（迁移 0005 建的 ``novel_local_id_seq``，0006 改名并改为承载坐标）。
COORD_SEQUENCE = "novel_local_coord_seq"
ID_LOCK_KEY = "copixiv.library.local_novel_id"

# 只在「库里还没有任何 pixiv 行」时使用（新装、先上传后抓取）：此时没有水位
# 可参照，只能按时间线性外推作品号，否则本地书会被钉在 id 轴起点（2012 年），
# 等第一次抓取落地就永远沉底。
#
# 校准值来自线上库（2026-09-18）：最新作品号 29,155,910 / create_time
# 2026-09-17T15:00Z，近 90 天实测增速 8,509 号/天。
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


def pixiv_watermark(session: Session) -> int | None:
    """已抓取的最大 pixiv 作品号；一行都没有时 ``None``。"""
    return session.execute(
        select(func.max(models.Novel.id)).where(models.Novel.source == SOURCE_PIXIV)
    ).scalar()


def lock_local_ids(session: Session) -> None:
    """获取事务级咨询锁，串行化本地书的去重检查、发号与落库。"""
    session.execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": ID_LOCK_KEY},
    )


def next_local_id(session: Session, now: datetime | None = None, *, lock: bool = True) -> int:
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


class LocalLibraryRepository:
    """Repository for local library dedup, coordinate allocation, and inserts."""

    def __init__(self, session: Session):
        self.session = session

    def lock_local_ids(self) -> None:
        """Acquire the transaction-level advisory lock serializing dedup+insert."""
        lock_local_ids(self.session)

    def next_local_id(self) -> int:
        """Allocate the next monotonic negative coordinate ID (lock already held)."""
        return next_local_id(self.session, lock=False)

    def find_local_duplicate(self, source_key: str) -> tuple[int, str] | None:
        """Return ``(id, title)`` if a local book with *source_key* already exists."""
        row = self.session.execute(
            select(models.Novel.id, models.Novel.title).where(
                models.Novel.source == SOURCE_LOCAL,
                models.Novel.source_key == source_key,
            )
        ).first()
        if row is None:
            return None
        return int(row[0]), str(row[1])

    def get_novel(self, novel_id: int) -> models.Novel | None:
        """Load an existing novel row by primary key."""
        return self.session.get(models.Novel, novel_id)

    def apply_preset_author(
        self,
        novel: models.Novel | None,
        preset_author_id: int,
        preset_author_name: str | None,
    ) -> bool:
        """Attach *preset_author_id*/*preset_author_name* onto an existing duplicate."""
        if novel is None:
            return False
        if novel.author_id == preset_author_id and novel.author_name == preset_author_name:
            return False
        novel.author_id = preset_author_id
        novel.author_name = preset_author_name
        self.session.flush()
        return True

    def mark_epub_done(self, novel: models.Novel) -> None:
        """Record ``has_epub=DONE`` after attaching an EPUB original onto disk."""
        novel.has_epub = EpubStatus.DONE
        self.session.flush()

    def insert_local_novel(
        self,
        *,
        novel_id: int,
        title: str,
        author_id: int | None,
        author_name: str | None,
        path: str,
        text_length: int,
        has_epub: EpubStatus,
        source_key: str,
    ) -> models.Novel:
        """Insert a new local book and flush within the enclosing write transaction."""
        novel = models.Novel(
            id=novel_id,
            title=title,
            author_id=author_id,
            author_name=author_name,
            path=path,
            like=0,
            view=0,
            text=text_length,
            caption=None,
            series_id=None,
            series_name=None,
            series_index=None,
            create_time=datetime.now(UTC),
            has_epub=has_epub,
            tags=[],
            is_favourite=False,
            source=SOURCE_LOCAL,
            source_key=source_key,
        )
        self.session.add(novel)
        self.session.flush()
        return novel
