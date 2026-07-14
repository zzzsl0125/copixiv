"""SQLAlchemy ORM models — table schema kept identical to v1 for DB compatibility."""

import enum

from sqlalchemy import (
    Column, Integer, String, Text, Float, ForeignKey, Index,
    UniqueConstraint, Boolean, JSON, Enum,
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
        Index("idx_novel_like_text_id", "like", "text", "id"),
        Index("idx_novel_author_id", "author_id", "id"),
        Index("ix_novel_shuffle_like_text", "shuffle", "like", "text"),
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
    arguments = Column(Text)
    status = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String)
    duration = Column(Float)
    result = Column(Text)


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
