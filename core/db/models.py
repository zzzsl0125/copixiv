# -*- coding: utf-8 -*-
from sqlalchemy import (
    Column, Integer, String, Text, Float, ForeignKey, Index, UniqueConstraint,
    Boolean, JSON, Enum
)
from sqlalchemy.orm import declarative_base, relationship
import enum
from . import constants as C

Base = declarative_base()

# Enums
class TagPreferenceType(str, enum.Enum):
    favourite = "favourite"
    blocked = "blocked"

class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

# Models

class Author(Base):
    __tablename__ = C.TABLE_AUTHOR
    
    author_id = Column(Integer, primary_key=True)
    author_name = Column(String)
    novel_count = Column(Integer, default=0)
    like = Column(Integer, default=0)
    view = Column(Integer, default=0)
    text = Column(Integer, default=0)
    
    # Relationships
    novels = relationship("Novel", back_populates="author_rel")
    series = relationship("Series", back_populates="author_rel")

    def __repr__(self):
        return f"<Author(id={self.author_id}, name='{self.author_name}')>"


class Series(Base):
    __tablename__ = C.TABLE_SERIES

    series_id = Column(Integer, primary_key=True)
    series_name = Column(String)
    novel_count = Column(Integer, default=0)
    author_id = Column(Integer, ForeignKey(f'{C.TABLE_AUTHOR}.author_id'))
    like = Column(Integer, default=0)
    view = Column(Integer, default=0)
    text = Column(Integer, default=0)
    
    # Relationships
    author_rel = relationship("Author", back_populates="series")
    novels = relationship("Novel", back_populates="series_rel")

    def __repr__(self):
        return f"<Series(id={self.series_id}, name='{self.series_name}')>"


class Novel(Base):
    __tablename__ = C.TABLE_NOVEL

    id = Column(Integer, primary_key=True)
    title = Column(String)
    author_id = Column(Integer, ForeignKey(f'{C.TABLE_AUTHOR}.author_id'))
    author_name = Column(String) # Redundant with author_rel but kept for cache/display
    path = Column(String, unique=True)
    like = Column(Integer, default=0, index=True)
    view = Column(Integer, default=0)
    text = Column(Integer, default=0, index=True)
    caption = Column(Text)
    series_id = Column(Integer, ForeignKey(f'{C.TABLE_SERIES}.series_id'), nullable=True)
    series_name = Column(String, nullable=True) # Redundant
    series_index = Column(Integer, nullable=True)
    create_time = Column(String) # ISO format or timestamp
    # 0: Cannot/No need (default)
    # 1: Needs making (pending)
    # 2: Has epub (done)
    has_epub = Column(Integer, default=0)

    # Relationships
    author_rel = relationship("Author", back_populates="novels")
    series_rel = relationship("Series", back_populates="novels")
    tags = relationship("Tag", secondary=C.TABLE_NOVEL_TAG, back_populates="novels")

    __table_args__ = (
        Index('idx_novel_author_likes', 'author_id', 'like'),
        Index('idx_novel_series_likes', 'series_id', 'like'),
    )

    def __repr__(self):
        return f"<Novel(id={self.id}, title='{self.title}')>"


class Tag(Base):
    __tablename__ = C.TABLE_TAG

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    reference_count = Column(Integer, nullable=False, default=0)

    # Relationships
    novels = relationship("Novel", secondary=C.TABLE_NOVEL_TAG, back_populates="tags")

    def __repr__(self):
        return f"<Tag(name='{self.name}')>"


class NovelTag(Base):
    __tablename__ = C.TABLE_NOVEL_TAG

    novel_id = Column(Integer, ForeignKey(f'{C.TABLE_NOVEL}.id', ondelete='CASCADE'), primary_key=True)
    tag_id = Column(Integer, ForeignKey(f'{C.TABLE_TAG}.id', ondelete='CASCADE'), primary_key=True)

    __table_args__ = (
        Index('idx_novel_tag_tag_id', 'tag_id'),
        Index('idx_novel_tag_novel_id', 'novel_id'),
    )


class Favourite(Base):
    __tablename__ = C.TABLE_FAVOURITE
    novel_id = Column(Integer, ForeignKey(f'{C.TABLE_NOVEL}.id', ondelete='CASCADE'), primary_key=True)
 

class SpecialFollow(Base):
    __tablename__ = C.TABLE_SPECIAL_FOLLOW
    author_id = Column(Integer, ForeignKey(f'{C.TABLE_AUTHOR}.author_id', ondelete='CASCADE'), primary_key=True)


class ProcessedPeriod(Base):
    __tablename__ = C.TABLE_PROCESSED_PERIOD
    period_type = Column(String(10), primary_key=True)  # 'day', 'month', 'year'
    period_value = Column(String(10), primary_key=True)  # YYYY-MM-DD etc


class FailedNovel(Base):
    __tablename__ = C.TABLE_FAILED_NOVEL
    novel_id = Column(Integer, primary_key=True)
    failure_type = Column(String)
    error_message = Column(Text)
    failed_times = Column(Integer, default=1)


class SearchHistory(Base):
    __tablename__ = C.TABLE_SEARCH_HISTORY
    id = Column(Integer, primary_key=True, autoincrement=True)
    queries = Column(String, nullable=False, unique=True)
    sort_by = Column(String, nullable=False)
    sort_order = Column(String, nullable=False)
    timestamp = Column(String, nullable=False)
    display_text = Column(String)


class TaskHistory(Base):
    __tablename__ = C.TABLE_TASK_HISTORY
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    arguments = Column(Text) # JSON
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
    config = Column(Text, nullable=True, default='{}')
    sort_index = Column(Integer, default=0)
    

class TagPreference(Base):
    __tablename__ = C.TABLE_TAG_PREFERENCE
    id = Column(Integer, primary_key=True, autoincrement=True)
    tag = Column(String, unique=True, index=True, nullable=False)
    preference = Column(Enum(TagPreferenceType), nullable=False)


class NovelEpubConversion(Base):
    __tablename__ = C.TABLE_NOVEL_EPUB_CONVERSION
    novel_id = Column(Integer, ForeignKey(f'{C.TABLE_NOVEL}.id', ondelete='CASCADE'), primary_key=True)
    status = Column(String, nullable=False, default='pending')
    last_processed = Column(String)


class RandomNovelPool(Base):
    __tablename__ = C.TABLE_RANDOM_NOVEL_POOL
    id = Column(Integer, primary_key=True, autoincrement=True)
    novel_id = Column(Integer, ForeignKey(f'{C.TABLE_NOVEL}.id', ondelete='CASCADE'), nullable=False)
    min_likes = Column(Integer, nullable=False, default=0)
    min_texts = Column(Integer, nullable=False, default=0)
    
    __table_args__ = (
        UniqueConstraint('novel_id', 'min_likes', 'min_texts', name='uq_random_pool_novel'),
        Index('idx_random_pool_criteria', 'min_likes', 'min_texts'),
    )
