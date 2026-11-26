"""Cross-source entity resolution and negative timeline coordinate allocation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from copixiv.core.models import SOURCE_PIXIV
from copixiv.db import models
from copixiv.db.base import ensure_parent_rows

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
    """下一个外部/本地书 id（负数）。发号在写事务内进行（2 条 SQL）。

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


next_external_novel_id = next_local_id


class EntityResolver:
    """Resolve ``(source, source_key)`` pairs into internal numeric entity IDs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def next_novel_id(self, *, lock: bool = True) -> int:
        """Allocate the next monotonic negative timeline coordinate for a non-Pixiv novel."""
        return next_external_novel_id(self.session, lock=lock)

    def ensure_pixiv_parents(
        self,
        *,
        author_ids: Iterable[int | None] = (),
        series_ids: Iterable[int | None] = (),
    ) -> None:
        """Idempotently ensure placeholder Pixiv author/series rows exist for FK safety."""
        ensure_parent_rows(self.session, author_ids=author_ids, series_ids=series_ids)

    def resolve_author(
        self,
        source: str,
        source_key: str,
        *,
        name: str | None = None,
    ) -> int:
        """Resolve ``(source, source_key)`` into an ``author_id``, creating or updating if needed."""
        if source == SOURCE_PIXIV:
            aid = int(source_key)
            self.session.execute(
                postgresql_insert(models.Author)
                .values(
                    author_id=aid,
                    author_name=name,
                    source=SOURCE_PIXIV,
                    source_key=str(aid),
                )
                .on_conflict_do_nothing(index_elements=["author_id"])
            )
            if name:
                self.session.execute(
                    update(models.Author)
                    .where(
                        models.Author.author_id == aid,
                        models.Author.author_name.is_distinct_from(name),
                    )
                    .values(author_name=name)
                )
            return aid

        self.session.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"author:{source}:{source_key}"},
        )
        existing = self.session.execute(
            select(models.Author).where(
                models.Author.source == source,
                models.Author.source_key == source_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if name and existing.author_name != name:
                existing.author_name = name
                self.session.flush()
            return int(existing.author_id)

        aid = -int(self.session.execute(sa_text("SELECT nextval('author_ext_id_seq')")).scalar_one())
        self.session.add(
            models.Author(
                author_id=aid,
                source=source,
                source_key=source_key,
                author_name=name,
            )
        )
        self.session.flush()
        return aid

    def resolve_series(
        self,
        source: str,
        source_key: str,
        *,
        name: str | None = None,
        author_id: int | None = None,
    ) -> int:
        """Resolve ``(source, source_key)`` into a ``series_id``, creating or updating if needed."""
        if source == SOURCE_PIXIV:
            sid = int(source_key)
            self.session.execute(
                postgresql_insert(models.Series)
                .values(
                    series_id=sid,
                    series_name=name,
                    author_id=author_id,
                    source=SOURCE_PIXIV,
                    source_key=str(sid),
                )
                .on_conflict_do_nothing(index_elements=["series_id"])
            )
            updates: dict[str, Any] = {}
            conditions = []
            if name:
                updates["series_name"] = name
                conditions.append(models.Series.series_name.is_distinct_from(name))
            if author_id is not None:
                updates["author_id"] = author_id
                conditions.append(models.Series.author_id.is_distinct_from(author_id))
            if updates:
                self.session.execute(
                    update(models.Series)
                    .where(models.Series.series_id == sid, or_(*conditions))
                    .values(**updates)
                )
            return sid

        self.session.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"series:{source}:{source_key}"},
        )
        existing = self.session.execute(
            select(models.Series).where(
                models.Series.source == source,
                models.Series.source_key == source_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            changed = False
            if name and existing.series_name != name:
                existing.series_name = name
                changed = True
            if author_id is not None and existing.author_id != author_id:
                existing.author_id = author_id
                changed = True
            if changed:
                self.session.flush()
            return int(existing.series_id)

        sid = -int(self.session.execute(sa_text("SELECT nextval('series_ext_id_seq')")).scalar_one())
        self.session.add(
            models.Series(
                series_id=sid,
                source=source,
                source_key=source_key,
                series_name=name,
                author_id=author_id,
            )
        )
        self.session.flush()
        return sid


__all__ = [
    "COORD_SEQUENCE",
    "ID_LOCK_KEY",
    "SEED_AT",
    "SEED_ID",
    "SEED_IDS_PER_DAY",
    "EntityResolver",
    "lock_local_ids",
    "next_external_novel_id",
    "next_local_id",
    "pixiv_watermark",
    "seed_coord",
]
