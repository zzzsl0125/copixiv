"""initial_baseline

Revision ID: 4bdb5c432fa2
Revises:
Create Date: 2026-06-29 22:20:43.756652

First migration — creates all tables and FTS5 virtual table if they don't already
exist.  Idempotent: safe to run on a database already created by Base.metadata.create_all().
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = '4bdb5c432fa2'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all application tables (IF NOT EXISTS for idempotency).

    Uses raw SQL for most operations because SQLite's ALTER TABLE support is
    limited and alembic's batch mode can interfere with virtual tables.
    """

    # ------------------------------------------------------------------
    # Core tables
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS author (
            author_id INTEGER NOT NULL,
            author_name VARCHAR,
            novel_count INTEGER,
            "like" INTEGER,
            "view" INTEGER,
            "text" INTEGER,
            last_update VARCHAR,
            PRIMARY KEY (author_id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS series (
            series_id INTEGER NOT NULL,
            series_name VARCHAR,
            novel_count INTEGER,
            author_id INTEGER,
            "like" INTEGER,
            "view" INTEGER,
            "text" INTEGER,
            PRIMARY KEY (series_id),
            FOREIGN KEY(author_id) REFERENCES author (author_id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS novel (
            id INTEGER NOT NULL,
            title VARCHAR,
            author_id INTEGER,
            author_name VARCHAR,
            path VARCHAR,
            "like" INTEGER,
            "view" INTEGER,
            "text" INTEGER,
            caption TEXT,
            series_id INTEGER,
            series_name VARCHAR,
            series_index INTEGER,
            create_time VARCHAR,
            has_epub INTEGER,
            PRIMARY KEY (id),
            FOREIGN KEY(author_id) REFERENCES author (author_id),
            FOREIGN KEY(series_id) REFERENCES series (series_id)
        )
    """)

    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_novel_path ON novel (path)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_novel_like ON novel (\"like\")")
    op.execute("CREATE INDEX IF NOT EXISTS ix_novel_text ON novel (\"text\")")
    op.execute("CREATE INDEX IF NOT EXISTS ix_novel_has_epub ON novel (has_epub)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_novel_author_likes ON novel (author_id, \"like\")")
    op.execute("CREATE INDEX IF NOT EXISTS idx_novel_series_likes ON novel (series_id, \"like\")")
    op.execute("CREATE INDEX IF NOT EXISTS idx_novel_like_id ON novel (\"like\", id)")

    # ------------------------------------------------------------------
    # Association & feature tables
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS tag (
            id INTEGER NOT NULL,
            name VARCHAR NOT NULL,
            reference_count INTEGER NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (name)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS novel_tag (
            novel_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (novel_id, tag_id),
            FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tag (id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_novel_tag_tag_id ON novel_tag (tag_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_novel_tag_novel_id ON novel_tag (novel_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS favourite (
            novel_id INTEGER NOT NULL,
            PRIMARY KEY (novel_id),
            FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS special_follow (
            author_id INTEGER NOT NULL,
            PRIMARY KEY (author_id),
            FOREIGN KEY(author_id) REFERENCES author (author_id) ON DELETE CASCADE
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS failed_novel (
            novel_id INTEGER NOT NULL,
            failure_type VARCHAR,
            error_message TEXT,
            failed_times INTEGER,
            PRIMARY KEY (novel_id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS processed_periods (
            period_type VARCHAR(10) NOT NULL,
            period_value VARCHAR(10) NOT NULL,
            PRIMARY KEY (period_type, period_value)
        )
    """)

    # ------------------------------------------------------------------
    # Search, tasks, preferences, aliases
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER NOT NULL,
            type VARCHAR NOT NULL,
            value VARCHAR NOT NULL,
            display_value VARCHAR,
            timestamp VARCHAR NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (type, value)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_history_timestamp ON search_history (timestamp)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER NOT NULL,
            name VARCHAR NOT NULL,
            arguments TEXT,
            status VARCHAR NOT NULL,
            start_time VARCHAR NOT NULL,
            end_time VARCHAR,
            duration FLOAT,
            result TEXT,
            PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            task VARCHAR(255) NOT NULL,
            cron VARCHAR(255) NOT NULL,
            params JSON,
            is_enabled BOOLEAN,
            config TEXT,
            sort_index INTEGER,
            PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS tag_preferences (
            id INTEGER NOT NULL,
            tag VARCHAR NOT NULL,
            preference VARCHAR NOT NULL,
            sort_index INTEGER,
            PRIMARY KEY (id),
            UNIQUE (tag)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_tag_preferences_tag ON tag_preferences (tag)")

    # tag_aliases uses integer FKs per the Phase 3 plan
    op.execute("""
        CREATE TABLE IF NOT EXISTS tag_aliases (
            id INTEGER NOT NULL,
            source INTEGER NOT NULL,
            target INTEGER NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(source) REFERENCES tag (id),
            FOREIGN KEY(target) REFERENCES tag (id)
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_tag_aliases_source ON tag_aliases (source)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tag_aliases_target ON tag_aliases (target)")

    # ------------------------------------------------------------------
    # EPUB conversion, random pool, tokens
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS novel_epub_conversions (
            novel_id INTEGER NOT NULL,
            status VARCHAR NOT NULL,
            last_processed VARCHAR,
            PRIMARY KEY (novel_id),
            FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS random_novel_pool (
            id INTEGER NOT NULL,
            novel_id INTEGER NOT NULL,
            min_likes INTEGER NOT NULL,
            min_texts INTEGER NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (novel_id, min_likes, min_texts),
            FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_random_pool_criteria ON random_novel_pool (min_likes, min_texts)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            token VARCHAR(255) NOT NULL,
            premium BOOLEAN,
            valid BOOLEAN,
            sort_index INTEGER,
            PRIMARY KEY (id),
            UNIQUE (name)
        )
    """)

    # ------------------------------------------------------------------
    # FTS5 virtual table (idempotent)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS novel_fts USING fts5(
            title, author_name, series_name,
            content='novel', content_rowid='id'
        )
    """)


def downgrade() -> None:
    """Drop all application tables — use with caution (destroys data)."""
    op.execute("DROP TABLE IF EXISTS novel_fts")
    for table in [
        "tokens", "random_novel_pool", "novel_epub_conversions",
        "tag_aliases", "tag_preferences", "scheduled_tasks",
        "task_history", "search_history",
        "processed_periods", "failed_novel",
        "special_follow", "favourite",
        "novel_tag", "tag",
        "novel", "series", "author",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table}")
