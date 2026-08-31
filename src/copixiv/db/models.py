"""SQLAlchemy ORM models for the copixiv database.

postgres-migration: these models mirror the PostgreSQL-only greenfield
target schema (``db_greenfield_design.md`` §4).  SQLite-era constructs are
gone:

- ``novel.tags TEXT[]`` (+ GIN) replaces the ``novel_tag`` join table.
- ``novel.is_favourite`` / ``author.is_special_follow`` booleans replace the
  ``favourite`` / ``special_follow`` join tables.
- ID columns are ``BIGINT``; counter columns are ``INTEGER``; timestamp
  columns are ``timestamptz``; ``task_history.result`` / ``progress`` and
  ``scheduled_task.params`` are ``JSONB``.

The ``NovelTag``, ``Favourite`` and ``SpecialFollow`` model classes are
**removed** — repository code still references them inside function bodies
(phase 2 rewrites those call sites), so removing them here does not break
module import.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql.elements import quoted_name

from . import constants as C

Base = declarative_base()


# ---------------------------------------------------------------------------
# Enums (kept for repository-layer backward compatibility; columns store the
# plain string values, matching the design's TEXT + CHECK constraint).
# ---------------------------------------------------------------------------

class TagPreferenceORM(str, enum.Enum):
    favourite = "favourite"
    blocked = "blocked"


class TaskStatusORM(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


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

    author_id = Column(BigInteger, primary_key=True)
    author_name = Column(Text)
    novel_count = Column(Integer, default=0, nullable=False)
    like = Column(LIKE_COL, Integer, default=0, nullable=False)
    view = Column(VIEW_COL, Integer, default=0, nullable=False)
    text = Column(TEXT_COL, Integer, default=0, nullable=False)
    last_update = Column(DateTime(timezone=True), nullable=True)
    is_special_follow = Column(Boolean, default=False, nullable=False)

    novels = relationship("Novel", back_populates="author_rel")
    series = relationship("Series", back_populates="author_rel")

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

    series_id = Column(BigInteger, primary_key=True)
    series_name = Column(Text)
    novel_count = Column(Integer, default=0, nullable=False)
    author_id = Column(
        BigInteger, ForeignKey(f"{C.TABLE_AUTHOR}.author_id", ondelete="SET NULL")
    )
    like = Column(LIKE_COL, Integer, default=0, nullable=False)
    view = Column(VIEW_COL, Integer, default=0, nullable=False)
    text = Column(TEXT_COL, Integer, default=0, nullable=False)

    author_rel = relationship("Author", back_populates="series")
    novels = relationship("Novel", back_populates="series_rel")

    __table_args__ = (
        Index("ix_series_author_id", "author_id"),
    )

    def __repr__(self) -> str:
        return f"<Series(id={self.series_id}, name='{self.series_name}')>"


class Novel(Base):
    __tablename__ = C.TABLE_NOVEL

    id = Column(BigInteger, primary_key=True)
    title = Column(Text, nullable=False)
    author_id = Column(BigInteger, ForeignKey(f"{C.TABLE_AUTHOR}.author_id"))
    author_name = Column(Text)
    path = Column(Text, unique=True)
    like = Column(LIKE_COL, Integer, default=0, nullable=False)
    view = Column(VIEW_COL, Integer, default=0, nullable=False)
    text = Column(TEXT_COL, Integer, default=0, nullable=False)
    caption = Column(Text)
    series_id = Column(
        BigInteger,
        ForeignKey(f"{C.TABLE_SERIES}.series_id", ondelete="SET NULL"),
        nullable=True,
    )
    series_name = Column(Text, nullable=True)
    series_index = Column(Integer, nullable=True)
    create_time = Column(DateTime(timezone=True), nullable=True)
    has_epub = Column(Integer, default=0, nullable=False)
    shuffle = Column(Integer, default=0, nullable=False)
    tags = Column(ARRAY(Text), nullable=False, server_default=sa_text("'{}'"))
    is_favourite = Column(Boolean, default=False, nullable=False)

    author_rel = relationship("Author", back_populates="novels")
    series_rel = relationship("Series", back_populates="novels")

    __table_args__ = (
        Index("ix_novel_like_text_id", "like", "text", "id"),
        Index("ix_novel_like_id", "like", "id"),
        Index("ix_novel_shuffle_id", "shuffle", "id"),
        Index("ix_novel_shuffle_like_text", "shuffle", "like", "text"),
        Index("ix_novel_author_id", "author_id", "id"),
        Index("ix_novel_series_id", "series_id", "id"),
        Index("ix_novel_author_like", "author_id", "like"),
        Index("ix_novel_series_like", "series_id", "like"),
        Index("ix_novel_create_time", "create_time"),
        Index("ix_novel_tags_gin", "tags", postgresql_using="gin"),
        Index(
            "ix_novel_favourite",
            "id",
            postgresql_where=sa_text("is_favourite"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Novel(id={self.id}, title='{self.title}')>"


class NovelSearch(Base):
    """Application-maintained char-gram search table (``novel_search``).

    ``search_text`` is the char-gram text computed by
    ``copixiv.features.novels.fts.gram_tokenize`` over
    ``title + author_name + series_name + tags``.  The GIN index on
    ``to_tsvector('simple', search_text)`` is what keyword search uses.
    """

    __tablename__ = C.TABLE_NOVEL_SEARCH

    novel_id = Column(
        BigInteger,
        ForeignKey(f"{C.TABLE_NOVEL}.id", ondelete="CASCADE"),
        primary_key=True,
    )
    search_text = Column(Text, nullable=False)

    novel = relationship("Novel")

    __table_args__ = (
        Index(
            "novel_search_gin",
            sa_text("to_tsvector('simple', search_text)"),
            postgresql_using="gin",
        ),
    )

    def __repr__(self) -> str:
        return f"<NovelSearch(novel_id={self.novel_id})>"


class Tag(Base):
    __tablename__ = C.TABLE_TAG

    id = Column(BigInteger, Identity(), primary_key=True)
    name = Column(Text, nullable=False, unique=True)
    reference_count = Column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Tag(name='{self.name}')>"


class TagPreference(Base):
    __tablename__ = C.TABLE_TAG_PREFERENCE

    id = Column(BigInteger, Identity(), primary_key=True)
    tag = Column(Text, unique=True, index=True, nullable=False)
    preference = Column(Text, nullable=False)
    sort_index = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "preference IN ('favourite', 'blocked')",
            name="ck_tag_preference_value",
        ),
    )


class Setting(Base):
    """Runtime settings key-value store (UI-changeable, survives restarts)."""

    __tablename__ = C.TABLE_SETTINGS
    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<Setting(key={self.key!r}, value={self.value!r})>"


class TagAlias(Base):
    __tablename__ = C.TABLE_TAG_ALIAS
    id = Column(BigInteger, Identity(), primary_key=True)
    source = Column(
        BigInteger,
        ForeignKey(f"{C.TABLE_TAG}.id"),
        unique=True,
        index=True,
        nullable=False,
    )
    target = Column(
        BigInteger,
        ForeignKey(f"{C.TABLE_TAG}.id"),
        index=True,
        nullable=False,
    )

    source_tag = relationship("Tag", foreign_keys=[source])
    target_tag = relationship("Tag", foreign_keys=[target])

    __table_args__ = (
        Index("ix_tag_alias_target", "target"),
    )

    def __repr__(self) -> str:
        return f"<TagAlias(source_id={self.source}, target_id={self.target})>"


class ScheduledTask(Base):
    __tablename__ = C.TABLE_SCHEDULED_TASK

    id = Column(BigInteger, Identity(), primary_key=True)
    name = Column(Text, nullable=False)
    task = Column(Text, nullable=False)
    cron = Column(Text, nullable=False)
    params = Column(JSONB, nullable=True)
    is_enabled = Column(Boolean, default=False, nullable=False)
    config = Column(Text, nullable=True, default="{}")
    sort_index = Column(Integer, default=0, nullable=False)


class TaskHistory(Base):
    __tablename__ = C.TABLE_TASK_HISTORY
    id = Column(BigInteger, Identity(), primary_key=True)
    name = Column(Text, nullable=False)
    task_func = Column(Text, nullable=True)
    arguments = Column(Text)
    status = Column(Text, nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=True)
    duration = Column(Float)
    result = Column(JSONB)
    progress = Column(JSONB)

    __table_args__ = (
        # Partial unique index: only pending/running rows constrain re-enqueue.
        # NULL ``task_func`` (legacy rows) are not constrained.
        Index(
            "ux_task_history_running",
            "task_func",
            unique=True,
            postgresql_where=sa_text("status IN ('pending', 'running')"),
        ),
    )


class Token(Base):
    __tablename__ = C.TABLE_TOKEN

    id = Column(BigInteger, Identity(), primary_key=True)
    name = Column(Text, unique=True, nullable=False)
    token = Column(Text, nullable=False)
    premium = Column(Boolean, default=False, nullable=False)
    valid = Column(Boolean, default=True, nullable=False)
    sort_index = Column(Integer, default=0, nullable=False)
    is_follow = Column(Boolean, default=False, nullable=False)


class FailedNovel(Base):
    __tablename__ = C.TABLE_FAILED_NOVEL
    novel_id = Column(
        BigInteger,
        ForeignKey(f"{C.TABLE_NOVEL}.id", ondelete="CASCADE"),
        primary_key=True,
    )
    failure_type = Column(Text)
    error_message = Column(Text)
    failed_times = Column(Integer, default=1, nullable=False)
    title = Column(Text, nullable=True)
    last_failed_at = Column(DateTime(timezone=True), nullable=True)

    novel = relationship("Novel")

    __table_args__ = (
        Index("ix_failed_novel_last_failed_at", "last_failed_at"),
    )


class SearchHistory(Base):
    __tablename__ = C.TABLE_SEARCH_HISTORY
    id = Column(BigInteger, Identity(), primary_key=True)
    type = Column(Text, nullable=False)
    value = Column(Text, nullable=False)
    display_value = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "type", "value", name="uq_search_history_type_value"
        ),
        Index("ix_search_history_type_timestamp", "type", "timestamp"),
    )
