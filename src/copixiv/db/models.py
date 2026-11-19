"""SQLAlchemy ORM models for the copixiv database.

- ``novel.tags TEXT[]`` stores tags directly; full-text/substring and tag
  similarity search run exclusively in the in-process Rust engine
  (``copixiv_rs``), so PG keeps only constraint / maintenance indexes.
- ID columns are ``BIGINT``; aggregate counters on ``author`` / ``series``
  are ``BIGINT``; timestamp columns are ``timestamptz``;
  ``task_history.result`` / ``progress`` and ``scheduled_task.params`` use
  :class:`JsonObjectType` over ``JSONB``.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import quoted_name

from . import constants as C


class JsonObjectType(TypeDecorator[Any]):
    """JSONB column wrapper that safely unwraps legacy double-encoded JSON strings."""

    impl = JSONB
    cache_ok = True

    @staticmethod
    def _unwrap(value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (ValueError, TypeError):
                pass
        return value

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return self._unwrap(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return self._unwrap(value)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums (kept for repository-layer backward compatibility; columns store the
# plain string values, matching the design's TEXT + CHECK constraint).
# ---------------------------------------------------------------------------


class TagPreferenceORM(enum.StrEnum):
    favourite = "favourite"
    blocked = "blocked"


# Reserved-word column names: SQLAlchemy auto-quotes ``like`` but ``view`` /
# ``text`` are non-reserved in PG and would be emitted bare.  Quote all three
# explicitly so column names always exactly match the design DDL and never
# collide with a SQL keyword.
LIKE_COL = quoted_name(C.COL_LIKES, True)
VIEW_COL = quoted_name(C.COL_VIEWS, True)
TEXT_COL = quoted_name(C.COL_TEXTS, True)


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class Author(Base):
    __tablename__ = C.TABLE_AUTHOR

    author_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    author_name: Mapped[str | None] = mapped_column(Text)
    novel_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)
    like: Mapped[int] = mapped_column(
        LIKE_COL, BigInteger, default=0, server_default=sa_text("0"), nullable=False
    )
    view: Mapped[int] = mapped_column(
        VIEW_COL, BigInteger, default=0, server_default=sa_text("0"), nullable=False
    )
    text: Mapped[int] = mapped_column(
        TEXT_COL, BigInteger, default=0, server_default=sa_text("0"), nullable=False
    )
    last_update: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_special_follow: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false"), nullable=False
    )

    novels: Mapped[list[Novel]] = relationship("Novel", back_populates="author_rel")
    series: Mapped[list[Series]] = relationship("Series", back_populates="author_rel")

    __table_args__ = (
        Index(
            "ix_author_special_follow",
            "author_id",
            postgresql_where=sa_text("is_special_follow"),
        ),
        Index("ix_author_last_update", "last_update"),
    )

    def __repr__(self) -> str:
        return f"<Author(id={self.author_id}, name='{self.author_name}')>"


class Series(Base):
    __tablename__ = C.TABLE_SERIES

    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series_name: Mapped[str | None] = mapped_column(Text)
    novel_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)
    author_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(f"{C.TABLE_AUTHOR}.author_id", ondelete="SET NULL")
    )
    like: Mapped[int] = mapped_column(
        LIKE_COL, BigInteger, default=0, server_default=sa_text("0"), nullable=False
    )
    view: Mapped[int] = mapped_column(
        VIEW_COL, BigInteger, default=0, server_default=sa_text("0"), nullable=False
    )
    text: Mapped[int] = mapped_column(
        TEXT_COL, BigInteger, default=0, server_default=sa_text("0"), nullable=False
    )

    author_rel: Mapped[Author | None] = relationship("Author", back_populates="series")
    novels: Mapped[list[Novel]] = relationship("Novel", back_populates="series_rel")

    __table_args__ = (Index("ix_series_author_id", "author_id"),)

    def __repr__(self) -> str:
        return f"<Series(id={self.series_id}, name='{self.series_name}')>"


class Novel(Base):
    __tablename__ = C.TABLE_NOVEL

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey(f"{C.TABLE_AUTHOR}.author_id"))
    author_name: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text, unique=True)
    like: Mapped[int] = mapped_column(
        LIKE_COL, Integer, default=0, server_default=sa_text("0"), nullable=False
    )
    view: Mapped[int] = mapped_column(
        VIEW_COL, Integer, default=0, server_default=sa_text("0"), nullable=False
    )
    text: Mapped[int] = mapped_column(
        TEXT_COL, Integer, default=0, server_default=sa_text("0"), nullable=False
    )
    caption: Mapped[str | None] = mapped_column(Text)
    series_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(f"{C.TABLE_SERIES}.series_id", ondelete="SET NULL"),
        nullable=True,
    )
    series_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    create_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 0 = 未分类（列默认值，迁移遗留，对账时会归类）
    # 1 = 待处理（正文有图片占位符，EPUB 还没成）  2 = 已完成
    # 3 = 确认无图（终态，无需 EPUB）
    # 归属：**产出该 EPUB 的路径**置位（抓取流水线、上传路径写出成品后），
    # 对账任务（check_epub）负责降级；元数据刷新**永不**写它。
    # （2026-09 追溯：元数据刷新曾无条件写 0，把 22 万行"已完成"冲掉。）
    has_epub: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)
    shuffle: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa_text("'{}'::text[]")
    )
    is_favourite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pixiv",
        server_default=sa_text("'pixiv'::text"),
    )
    source_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    author_rel: Mapped[Author | None] = relationship("Author", back_populates="novels")
    series_rel: Mapped[Series | None] = relationship("Series", back_populates="novels")

    __table_args__ = (
        Index("ix_novel_author_id", "author_id", "id"),
        Index("ix_novel_series_id", "series_id", "id"),
        Index(
            "ux_novel_source_key",
            "source",
            "source_key",
            unique=True,
            postgresql_where=sa_text("source_key IS NOT NULL"),
        ),
        Index(
            "ix_novel_favourite",
            "id",
            postgresql_where=sa_text("is_favourite"),
        ),
        Index(
            "ix_novel_local_id",
            "id",
            postgresql_where=sa_text("id < 0"),
        ),
        Index(
            "ix_novel_missing_source",
            "id",
            postgresql_where=sa_text("source = ''"),
        ),
        Index(
            "ix_novel_series_missing_index",
            "series_id",
            postgresql_where=sa_text("series_id IS NOT NULL AND series_index IS NULL"),
        ),
        Index(
            "ix_novel_empty_author_name",
            "author_id",
            postgresql_where=sa_text("author_id IS NOT NULL AND (author_name IS NULL OR author_name = '')"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Novel(id={self.id}, title='{self.title}')>"


class Tag(Base):
    __tablename__ = C.TABLE_TAG

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    reference_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )

    __table_args__ = (
        Index(
            "tag_reference_count_id_idx",
            sa_text("reference_count DESC"),
            sa_text("id DESC"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Tag(name='{self.name}')>"


class TagPreference(Base):
    __tablename__ = C.TABLE_TAG_PREFERENCE

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tag: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    preference: Mapped[str] = mapped_column(Text, nullable=False)
    sort_index: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "preference IN ('favourite', 'blocked')",
            name="ck_tag_preference_value",
        ),
    )


class Setting(Base):
    """Runtime settings key-value store (UI-changeable, survives restarts)."""

    __tablename__ = C.TABLE_SETTINGS
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<Setting(key={self.key!r}, value={self.value!r})>"


class TagAlias(Base):
    __tablename__ = C.TABLE_TAG_ALIAS
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{C.TABLE_TAG}.id"),
        unique=True,
        nullable=False,
    )
    target: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{C.TABLE_TAG}.id"),
        nullable=False,
    )

    source_tag: Mapped[Tag] = relationship("Tag", foreign_keys=[source])
    target_tag: Mapped[Tag] = relationship("Tag", foreign_keys=[target])

    __table_args__ = (Index("ix_tag_alias_target", "target"),)

    def __repr__(self) -> str:
        return f"<TagAlias(source_id={self.source}, target_id={self.target})>"


class ScheduledTask(Base):
    __tablename__ = C.TABLE_SCHEDULED_TASK

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JsonObjectType(), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false"), nullable=False
    )
    sort_index: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)


class TaskHistory(Base):
    __tablename__ = C.TABLE_TASK_HISTORY
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    task_func: Mapped[str | None] = mapped_column(Text, nullable=True)
    arguments: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float)
    result: Mapped[Any | None] = mapped_column(JsonObjectType())
    progress: Mapped[Any | None] = mapped_column(JsonObjectType())
    # No dedup index: repeated enqueues for the same ``task_func`` insert
    # separate pending rows and queue up (task ordering is the worker's job).

    __table_args__ = (
        Index(
            "ix_task_history_pending",
            "id",
            postgresql_where=sa_text("status = 'pending'"),
        ),
        Index(
            "ix_task_history_active_func",
            "task_func",
            postgresql_where=sa_text("status IN ('pending', 'running')"),
        ),
    )


class Token(Base):
    __tablename__ = C.TABLE_TOKEN

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    premium: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false"), nullable=False
    )
    valid: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    sort_index: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"), nullable=False)
    is_follow: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false"), nullable=False
    )

    __table_args__ = (
        Index(
            "ux_token_follow",
            "is_follow",
            unique=True,
            postgresql_where=sa_text("is_follow"),
        ),
    )


class FailedNovel(Base):
    __tablename__ = C.TABLE_FAILED_NOVEL
    # NOTE: deliberately NO ForeignKey.  The ingest pipeline downloads
    # BEFORE persisting (plan → download → persist), so a download failure
    # can occur for a novel that has never been written to ``novel`` —
    # such records must still be captured in the failure ledger.  Deleting
    # a novel cleans its ledger rows explicitly in the repository layer
    # (``SQLAlchemyNovelWriteRepository.delete`` / ``delete_many``).
    novel_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    failure_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    failed_times: Mapped[int] = mapped_column(Integer, default=1, server_default=sa_text("1"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_failed_novel_last_failed_at", "last_failed_at"),)


class SeriesDeadLetter(Base):
    """Verdicts about series pixiv no longer serves.

    The ``fix_series_index`` candidate set is "``series_id`` set,
    ``series_index`` NULL", and that predicate cannot tell "could not fetch
    this round" from "will never fetch".  A series deleted on pixiv keeps
    matching forever, so it is re-requested every night and can never
    succeed: 183 of 200 candidates in one measured run were such series,
    which is why a 30-minute task filled 0–47 chapter numbers per run.

    This is deliberately **not** a column on ``series``.  ``series`` is an
    entity that ``update_summary`` aggregates into and that the author page /
    series preview / batch edit all read; adding an operational "this row is
    not really there" flag would force every reader to consult it.  A ledger
    row is a verdict, like ``failed_novel`` — separate table, separate owner,
    invisible to business reads.

    ``consecutive_failures`` guards against a single 404: pixiv can answer
    404 transiently, so a series is retired only after
    :data:`copixiv.features.series.ledger.RETIRE_AFTER_FAILURES`
    consecutive *definitive* 404s.  Any success resets the count and clears
    ``retired_at``, so a mis-retired series comes back on its own.
    """

    __tablename__ = C.TABLE_SERIES_DEAD_LETTER

    # Same shape as ``failed_novel``: the business key, no ForeignKey (a
    # verdict may exist before/without a ``series`` row, and deleting a
    # ``series`` row must not silently resurrect the fetch attempts).
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa_text("0"), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = still worth trying; set = excluded from the candidate query.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_series_dead_letter_retired_at",
            "retired_at",
            postgresql_where=sa_text("retired_at IS NOT NULL"),
        ),
    )


class SearchHistory(Base):
    __tablename__ = C.TABLE_SEARCH_HISTORY
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    display_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("type", "value", name="uq_search_history_type_value"),
        Index("ix_search_history_type_timestamp", "type", "timestamp"),
    )
