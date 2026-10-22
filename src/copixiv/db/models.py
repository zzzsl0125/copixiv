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
    Computed,
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
    # 0 = 未分类（列默认值，迁移遗留，对账时会归类）
    # 1 = 待处理（正文有图片占位符，EPUB 还没成）  2 = 已完成
    # 3 = 确认无图（终态，无需 EPUB）
    # 归属：**产出该 EPUB 的路径**置位（抓取流水线、上传路径写出成品后），
    # 对账任务（check_epub）负责降级；元数据刷新**永不**写它。
    # （2026-09 追溯：元数据刷新曾无条件写 0，把 22 万行"已完成"冲掉。）
    has_epub = Column(Integer, default=0, nullable=False)
    shuffle = Column(Integer, default=0, nullable=False)
    tags = Column(ARRAY(Text), nullable=False, server_default=sa_text("'{}'"))
    is_favourite = Column(Boolean, default=False, nullable=False)
    # Where the row came from — migration 0005, docs/LIBRARY_DESIGN.md §2:
    # ``'pixiv'`` for everything the scraper wrote, ``'local'`` for a TXT the
    # user uploaded.  A column and not a tag: tags are user assets, and a
    # retroactive alias rewrite could merge 「本地」 away and empty the shelf.
    source = Column(
        Text,
        nullable=False,
        default="pixiv",
        server_default=sa_text("'pixiv'"),
    )
    # Local books: sha256 of the normalised body (the dedup key).  NULL for
    # Pixiv rows — hence the *partial* unique index below.
    source_key = Column(Text, nullable=True)
    # Keyword search text — the four searchable fields assembled exactly like
    # the old ``copixiv_novel_text(title, author_name, series_name, tags)``
    # (space-joined, empty fields skipped via ``nullif``), then lowercased.
    # PostgreSQL generates it from the row being written (migration 0007), so
    # the ``pg_bigm`` GIN index below cannot drift from the data; the query
    # side (``features/novels/search.py``) matches it with one ``LIKE`` per
    # whitespace-separated keyword segment.  Case folding on the query side
    # must use SQL ``lower()`` too — the database runs with the ``C`` locale,
    # where Python's ``str.lower()`` folds characters this does not.
    #
    # The assembly lives in the IMMUTABLE SQL function ``copixiv_search_text``
    # (created by migration 0007) rather than inline: a STORED generated column
    # requires an immutable expression, and ``concat_ws`` /
    # ``array_to_string`` are catalogued ``STABLE``.  Keep this metadata
    # expression in step with that function.
    search_lower = Column(
        Text,
        Computed(
            "copixiv_search_text(title, author_name, series_name, tags)",
            persisted=True,
        ),
    )

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
        Index("ix_novel_source", "source"),
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
        # 时间轴排序键（迁移 0006，docs/LIBRARY_DESIGN.md §3.1）：pixiv 行的 id
        # 就是作品号，本地行的 id 是**负的**坐标（上传时刻的作品号位置），所以
        # 「按时间」= ``ORDER BY abs(id)``。``abs(id)`` 不是主键，必须自带一条
        # 表达式索引，否则每次列表排序都要全表 top-N Sort（实测 1.3ms → 35ms）。
        Index("ix_novel_abs_id", sa_text("abs(id)"), sa_text("id")),
        # Keyword search: a ``pg_bigm`` bigram GIN index over the generated
        # ``search_lower`` column (migration 0007).  The column is generated
        # by PostgreSQL from the row being written, so the index cannot drift
        # from the data, and the query side builds real substring predicates
        # (one ``LIKE`` per keyword segment) — see
        # ``features/novels/search.py``.
        Index(
            "novel_search_bigm",
            "search_lower",
            postgresql_using="gin",
            postgresql_ops={"search_lower": "gin_bigm_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<Novel(id={self.id}, title='{self.title}')>"


class Tag(Base):
    __tablename__ = C.TABLE_TAG

    id = Column(BigInteger, Identity(), primary_key=True)
    name = Column(Text, nullable=False, unique=True)
    reference_count = Column(Integer, nullable=False, default=0)

    # ``lower(name)`` is catalogued IMMUTABLE, so — unlike ``novel.search_lower``
    # (which needs the ``copixiv_search_text`` wrapper for the STABLE
    # ``concat_ws`` / ``array_to_string``) — the generated column is inlined.
    # Keep this expression in step with migration 0008.
    search_lower = Column(
        Text,
        Computed("lower(name)", persisted=True),
    )

    __table_args__ = (
        # 标签别名建议的两条索引（迁移 0008）：pg_bigm GIN 回答「哪些标签名字
        # 相似」（真子串语义），``(reference_count DESC, id DESC)`` btree 回答
        # 「取最热的 N 个」与 keyset 分页续扫 —— GIN 不提供顺序，缺了它就只能
        # 对整表 Seq Scan + Sort（见 features/tags/repo.py）。
        Index(
            "tag_search_bigm",
            "search_lower",
            postgresql_using="gin",
            postgresql_ops={"search_lower": "gin_bigm_ops"},
        ),
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
    # No dedup index: repeated enqueues for the same ``task_func`` insert
    # separate pending rows and queue up (task ordering is the worker's job).


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
    # NOTE: deliberately NO ForeignKey.  The ingest pipeline downloads
    # BEFORE persisting (plan → download → persist), so a download failure
    # can occur for a novel that has never been written to ``novel`` —
    # such records must still be captured in the failure ledger.  Deleting
    # a novel cleans its ledger rows explicitly in the repository layer
    # (``SQLAlchemyNovelWriteRepository.delete`` / ``delete_many``).
    novel_id = Column(
        BigInteger,
        primary_key=True,
    )
    failure_type = Column(Text)
    error_message = Column(Text)
    failed_times = Column(Integer, default=1, nullable=False)
    title = Column(Text, nullable=True)
    last_failed_at = Column(DateTime(timezone=True), nullable=True)

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
