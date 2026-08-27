"""SQLAlchemy ORM models for the copixiv database."""

import enum

from sqlalchemy import (
    Column, Integer, String, Text, Float, ForeignKey, Index,
    UniqueConstraint, Boolean, JSON, Enum, text,
)
from sqlalchemy.orm import declarative_base, relationship

from . import constants as C

Base = declarative_base()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TagPreferenceORM(str, enum.Enum):
    favourite = "favourite"
    blocked = "blocked"


class TaskStatusORM(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class Author(Base):
    __tablename__ = C.TABLE_AUTHOR

    author_id = Column(Integer, primary_key=True)
    author_name = Column(String)
    novel_count = Column(Integer, default=0)
    like = Column(Integer, default=0)
    view = Column(Integer, default=0)
    text = Column(Integer, default=0)
    last_update = Column(String, nullable=True)

    novels = relationship("Novel", back_populates="author_rel")
    series = relationship("Series", back_populates="author_rel")

    def __repr__(self) -> str:
        return f"<Author(id={self.author_id}, name='{self.author_name}')>"


class Series(Base):
    __tablename__ = C.TABLE_SERIES

    series_id = Column(Integer, primary_key=True)
    series_name = Column(String)
    novel_count = Column(Integer, default=0)
    author_id = Column(Integer, ForeignKey(f"{C.TABLE_AUTHOR}.author_id"))
    like = Column(Integer, default=0)
    view = Column(Integer, default=0)
    text = Column(Integer, default=0)

    author_rel = relationship("Author", back_populates="series")
    novels = relationship("Novel", back_populates="series_rel")

    def __repr__(self) -> str:
        return f"<Series(id={self.series_id}, name='{self.series_name}')>"


class Novel(Base):
    __tablename__ = C.TABLE_NOVEL

    id = Column(Integer, primary_key=True)
    title = Column(String)
    author_id = Column(Integer, ForeignKey(f"{C.TABLE_AUTHOR}.author_id"))
    author_name = Column(String)
    path = Column(String, unique=True)
    like = Column(Integer, default=0, index=True)
    view = Column(Integer, default=0)
    text = Column(Integer, default=0, index=True)
    caption = Column(Text)
    series_id = Column(
        Integer, ForeignKey(f"{C.TABLE_SERIES}.series_id"), nullable=True
    )
    series_name = Column(String, nullable=True)
    series_index = Column(Integer, nullable=True)
    create_time = Column(String, index=True)
    has_epub = Column(Integer, default=0, index=True)
    shuffle = Column(Integer, default=0)

    author_rel = relationship("Author", back_populates="novels")
    series_rel = relationship("Series", back_populates="novels")
    tags = relationship(
        "Tag", secondary=C.TABLE_NOVEL_TAG, back_populates="novels"
    )

    __table_args__ = (
        Index("idx_novel_author_likes", "author_id", "like"),
        Index("idx_novel_series_likes", "series_id", "like"),
        Index("idx_novel_series_id", "series_id", "id"),
        Index("idx_novel_like_text_id", "like", "text", "id"),
        Index("idx_novel_author_id", "author_id", "id"),
        Index("ix_novel_shuffle_like_text", "shuffle", "like", "text"),
        Index("ix_novel_shuffle_id", "shuffle", "id"),
    )

    def __repr__(self) -> str:
        return f"<Novel(id={self.id}, title='{self.title}')>"


class Tag(Base):
    __tablename__ = C.TABLE_TAG

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    reference_count = Column(Integer, nullable=False, default=0)

    novels = relationship(
        "Novel", secondary=C.TABLE_NOVEL_TAG, back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag(name='{self.name}')>"


class NovelTag(Base):
    __tablename__ = C.TABLE_NOVEL_TAG

    novel_id = Column(
        Integer,
        ForeignKey(f"{C.TABLE_NOVEL}.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id = Column(
        Integer,
        ForeignKey(f"{C.TABLE_TAG}.id", ondelete="CASCADE"),
        primary_key=True,
    )

    __table_args__ = (
        Index("idx_novel_tag_tag_id", "tag_id"),
        Index("idx_novel_tag_novel_id", "novel_id"),
    )


class Favourite(Base):
    __tablename__ = C.TABLE_FAVOURITE
    novel_id = Column(
        Integer,
        ForeignKey(f"{C.TABLE_NOVEL}.id", ondelete="CASCADE"),
        primary_key=True,
    )


class SpecialFollow(Base):
    __tablename__ = C.TABLE_SPECIAL_FOLLOW
    author_id = Column(
        Integer,
        ForeignKey(f"{C.TABLE_AUTHOR}.author_id", ondelete="CASCADE"),
        primary_key=True,
    )


class FailedNovel(Base):
    __tablename__ = C.TABLE_FAILED_NOVEL
    novel_id = Column(Integer, primary_key=True)
    failure_type = Column(String)
    error_message = Column(Text)
    failed_times = Column(Integer, default=1)
    # Enrichment for the "下载失败" management view: title is captured at
    # failure time when available (the ingest pipeline knows it; the
    # single fetch path may not), last_failed_at is the most recent failure time.
    title = Column(Text, nullable=True)
    last_failed_at = Column(String, nullable=True, index=True)


class SearchHistory(Base):
    __tablename__ = C.TABLE_SEARCH_HISTORY
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String, nullable=False)
    value = Column(String, nullable=False)
    display_value = Column(String, nullable=True)
    timestamp = Column(String, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "type", "value", name="uq_search_history_type_value"
        ),
        Index("ix_search_history_type_timestamp", "type", "timestamp"),
    )


class TaskHistory(Base):
    __tablename__ = C.TABLE_TASK_HISTORY
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    # Registered *function* name (``scheduled_tasks.task`` / ``TaskSpec.name``)
    # — the dedup key.  ``name`` keeps the display name (UI label).
    task_func = Column(String, nullable=True)
    arguments = Column(Text)
    status = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String)
    duration = Column(Float)
    result = Column(Text)
    # Live progress (S2 d) — column only here; wire-up lands later.
    progress = Column(Text, nullable=True)

    __table_args__ = (
        # Partial unique index: only pending/running rows constrain re-enqueue.
        # NULL ``task_func`` (legacy rows) are not constrained (SQLite treats
        # NULLs as distinct in a unique index).
        Index(
            "ux_task_history_running",
            "task_func",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
    )


class ScheduledTask(Base):
    __tablename__ = C.TABLE_SCHEDULED_TASK

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    task = Column(String(255), nullable=False)
    cron = Column(String(255), nullable=False)
    params = Column(JSON, nullable=True)
    is_enabled = Column(Boolean, default=False)
    config = Column(Text, nullable=True, default="{}")
    sort_index = Column(Integer, default=0)


class TagPreference(Base):
    __tablename__ = C.TABLE_TAG_PREFERENCE
    id = Column(Integer, primary_key=True, autoincrement=True)
    tag = Column(String, unique=True, index=True, nullable=False)
    preference = Column(Enum(TagPreferenceORM), nullable=False)
    sort_index = Column(Integer, default=0)


class Setting(Base):
    """Runtime settings key-value store (UI-changeable, survives restarts).

    Separate from config.yaml: static deployment config stays in YAML,
    while settings that the web UI may change at runtime live here.
    """

    __tablename__ = C.TABLE_SETTINGS
    key = Column(String(255), primary_key=True)
    value = Column(String(255), nullable=False)

    def __repr__(self) -> str:
        return f"<Setting(key={self.key!r}, value={self.value!r})>"


class TagAlias(Base):
    __tablename__ = C.TABLE_TAG_ALIAS
    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(
        Integer,
        ForeignKey(f"{C.TABLE_TAG}.id"),
        unique=True,
        index=True,
        nullable=False,
    )
    target = Column(
        Integer,
        ForeignKey(f"{C.TABLE_TAG}.id"),
        index=True,
        nullable=False,
    )

    # Relationships for lazy lookups
    source_tag = relationship("Tag", foreign_keys=[source])
    target_tag = relationship("Tag", foreign_keys=[target])

    def __repr__(self) -> str:
        return f"<TagAlias(source_id={self.source}, target_id={self.target})>"


class Token(Base):
    __tablename__ = C.TABLE_TOKEN

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    token = Column(String(255), nullable=False)
    premium = Column(Boolean, default=False)
    valid = Column(Boolean, default=True)
    sort_index = Column(Integer, default=0)
    # Designated「追更账号」—— single source of truth for the account that
    # owns the Pixiv following-list feed (novel_follow / user_follow_add /
    # user_follow_delete).  Mirrors premium/valid; set from the UI.
    is_follow = Column(Boolean, default=False)
