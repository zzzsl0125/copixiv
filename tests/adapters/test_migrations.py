"""Alembic migration-chain tests (D2).

Pins the core promise of the v2 rewrite: an existing v1-shaped database
can be upgraded in place by running the 6 Alembic migrations to head.
Two scenarios:

1. ``test_upgrade_v1_shaped_database`` — a faithful v1 schema (string
   tag_aliases, no shuffle column, old index set, legacy 3-column FTS
   table) plus sample data is upgraded and verified: revision stamped,
   alias names resolved to integer FKs, shuffle backfilled, new indexes
   created, and the legacy FTS table replaced by the char-gram definition
   (its content is a derived index, repopulated by the startup self-heal
   or a ``rebuild_fts`` run).
2. Fresh-database scenarios — empty file upgraded to head, and the
   upgrade being idempotent (second run is a no-op).
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from copixiv.db.engine import run_migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "69cbf0dfa207"

# v1 schema — the shape the old project left behind.  Deliberately
# *different* from the migrations' own baseline where v1 differed:
# - tag_aliases.source/target are tag-name strings (v1), not integer FKs
# - novel has no shuffle column
# - the post-v1 indexes don't exist yet
# - novel_fts is the legacy 3-column external-content table
_V1_SCHEMA = """
CREATE TABLE author (
    author_id INTEGER NOT NULL,
    author_name VARCHAR,
    novel_count INTEGER,
    "like" INTEGER,
    "view" INTEGER,
    "text" INTEGER,
    last_update VARCHAR,
    PRIMARY KEY (author_id)
);
CREATE TABLE series (
    series_id INTEGER NOT NULL,
    series_name VARCHAR,
    novel_count INTEGER,
    author_id INTEGER,
    "like" INTEGER,
    "view" INTEGER,
    "text" INTEGER,
    PRIMARY KEY (series_id),
    FOREIGN KEY(author_id) REFERENCES author (author_id)
);
CREATE TABLE novel (
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
);
CREATE UNIQUE INDEX ix_novel_path ON novel (path);
CREATE INDEX ix_novel_like ON novel ("like");
CREATE INDEX ix_novel_text ON novel ("text");
CREATE INDEX ix_novel_has_epub ON novel (has_epub);
CREATE INDEX idx_novel_author_likes ON novel (author_id, "like");
CREATE INDEX idx_novel_series_likes ON novel (series_id, "like");
CREATE INDEX idx_novel_like_id ON novel ("like", id);
CREATE TABLE tag (
    id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    reference_count INTEGER NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name)
);
CREATE TABLE novel_tag (
    novel_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (novel_id, tag_id),
    FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE,
    FOREIGN KEY(tag_id) REFERENCES tag (id) ON DELETE CASCADE
);
CREATE INDEX idx_novel_tag_tag_id ON novel_tag (tag_id);
CREATE INDEX idx_novel_tag_novel_id ON novel_tag (novel_id);
CREATE TABLE favourite (
    novel_id INTEGER NOT NULL,
    PRIMARY KEY (novel_id),
    FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE
);
CREATE TABLE special_follow (
    author_id INTEGER NOT NULL,
    PRIMARY KEY (author_id),
    FOREIGN KEY(author_id) REFERENCES author (author_id) ON DELETE CASCADE
);
CREATE TABLE failed_novel (
    novel_id INTEGER NOT NULL,
    failure_type VARCHAR,
    error_message TEXT,
    failed_times INTEGER,
    PRIMARY KEY (novel_id)
);
CREATE TABLE processed_periods (
    period_type VARCHAR(10) NOT NULL,
    period_value VARCHAR(10) NOT NULL,
    PRIMARY KEY (period_type, period_value)
);
CREATE TABLE search_history (
    id INTEGER NOT NULL,
    type VARCHAR NOT NULL,
    value VARCHAR NOT NULL,
    display_value VARCHAR,
    timestamp VARCHAR NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (type, value)
);
CREATE INDEX ix_search_history_timestamp ON search_history (timestamp);
CREATE TABLE task_history (
    id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    arguments TEXT,
    status VARCHAR NOT NULL,
    start_time VARCHAR NOT NULL,
    end_time VARCHAR,
    duration FLOAT,
    result TEXT,
    PRIMARY KEY (id)
);
CREATE TABLE scheduled_tasks (
    id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    task VARCHAR(255) NOT NULL,
    cron VARCHAR(255) NOT NULL,
    params JSON,
    is_enabled BOOLEAN,
    config TEXT,
    sort_index INTEGER,
    PRIMARY KEY (id)
);
CREATE TABLE tag_preferences (
    id INTEGER NOT NULL,
    tag VARCHAR NOT NULL,
    preference VARCHAR NOT NULL,
    sort_index INTEGER,
    PRIMARY KEY (id),
    UNIQUE (tag)
);
CREATE INDEX ix_tag_preferences_tag ON tag_preferences (tag);
-- v1: string alias columns
CREATE TABLE tag_aliases (
    id INTEGER NOT NULL,
    source VARCHAR NOT NULL,
    target VARCHAR NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_tag_aliases_source ON tag_aliases (source);
CREATE INDEX ix_tag_aliases_target ON tag_aliases (target);
CREATE TABLE novel_epub_conversions (
    novel_id INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    last_processed VARCHAR,
    PRIMARY KEY (novel_id),
    FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE
);
CREATE TABLE random_novel_pool (
    id INTEGER NOT NULL,
    novel_id INTEGER NOT NULL,
    min_likes INTEGER NOT NULL,
    min_texts INTEGER NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (novel_id, min_likes, min_texts),
    FOREIGN KEY(novel_id) REFERENCES novel (id) ON DELETE CASCADE
);
CREATE TABLE tokens (
    id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    token VARCHAR(255) NOT NULL,
    premium BOOLEAN,
    valid BOOLEAN,
    sort_index INTEGER,
    PRIMARY KEY (id),
    UNIQUE (name)
);
-- legacy FTS: 3 columns, external content (the shape v1 shipped with)
CREATE VIRTUAL TABLE novel_fts USING fts5(
    title, author_name, series_name,
    content='novel', content_rowid='id'
);
"""

_V1_DATA = """
INSERT INTO author (author_id, author_name) VALUES (1, '作者甲'), (2, '作者乙');
INSERT INTO novel
    (id, title, author_id, author_name, path, "like", "view", "text")
VALUES
    (100, '小说一', 1, '作者甲', '/tmp/100.txt', 100, 1000, 5000),
    (200, '小说二', 2, '作者乙', '/tmp/200.txt', 50, 500, 3000);
INSERT INTO tag (id, name, reference_count) VALUES
    (1, 'R-18', 1), (2, 'R18', 0), (3, 'NTR', 1), (4, 'ntr', 0);
INSERT INTO novel_tag (novel_id, tag_id) VALUES (100, 1), (200, 3);
-- v1 aliases reference tags by NAME
INSERT INTO tag_aliases (id, source, target) VALUES
    (1, 'R-18', 'R18'),
    (2, 'NTR', 'ntr');
INSERT INTO search_history (type, value, timestamp) VALUES
    ('keyword', 'R-18', '2026-01-01T00:00:00');
INSERT INTO tokens (name, token, valid) VALUES ('main', 'refresh-token', 1);
"""


def _make_v1_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA)
        conn.executescript(_V1_DATA)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def v1_db(tmp_path):
    db = tmp_path / "v1.db"
    _make_v1_db(db)
    return db


def _table_info(db_path: Path, table: str) -> dict[str, str]:
    """Return {column_name: declared_type} for a table."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1]: (r[2] or "").upper() for r in rows}


def _index_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.slow
class TestUpgradeV1Database:
    def test_v1_db_upgrades_to_head(self, v1_db):
        run_migrations(str(v1_db), project_root=str(REPO_ROOT))

        with sqlite3.connect(v1_db) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        assert version == HEAD_REVISION

        # tag_aliases: strings converted to integer FKs, data resolved
        cols = _table_info(v1_db, "tag_aliases")
        assert "INT" in cols["source"] and "INT" in cols["target"]
        with sqlite3.connect(v1_db) as conn:
            rows = conn.execute(
                "SELECT src.name, tgt.name FROM tag_aliases a "
                "JOIN tag src ON a.source = src.id "
                "JOIN tag tgt ON a.target = tgt.id ORDER BY a.id"
            ).fetchall()
        assert rows == [("R-18", "R18"), ("NTR", "ntr")]

        # novel: shuffle column added and backfilled with non-zero values
        novel_cols = _table_info(v1_db, "novel")
        assert "shuffle" in novel_cols
        with sqlite3.connect(v1_db) as conn:
            shuffles = conn.execute(
                "SELECT shuffle FROM novel ORDER BY id"
            ).fetchall()
        assert all(row[0] not in (None, 0) for row in shuffles)

        # post-v1 indexes created
        indexes = _index_names(v1_db)
        assert {
            "ix_novel_create_time",
            "ix_search_history_type_timestamp",
            "idx_novel_author_id",
            "ix_novel_shuffle_like_text",
            "ix_novel_shuffle_id",
        } <= indexes
        # the old shuffle-only index was replaced
        assert "ix_novel_shuffle" not in indexes

        # FTS table replaced by the char-gram migration (the content is a
        # derived index, repopulated by the startup self-heal / rebuild_fts)
        with sqlite3.connect(v1_db) as conn:
            fts = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='novel_fts'"
            ).fetchone()[0]
        assert "novel_fts" in fts
        assert "unicode61" in fts

        # original data intact
        with sqlite3.connect(v1_db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM novel").fetchone()[0]
        assert count == 2

    def test_upgrade_is_idempotent(self, v1_db):
        run_migrations(str(v1_db), project_root=str(REPO_ROOT))
        # Second run must be a clean no-op (no data loss, no errors)
        run_migrations(str(v1_db), project_root=str(REPO_ROOT))
        with sqlite3.connect(v1_db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM novel"
            ).fetchone()[0] == 2


@pytest.mark.slow
class TestFreshDatabase:
    def test_empty_db_upgrades_to_head(self, tmp_path):
        db = tmp_path / "fresh.db"
        sqlite3.connect(db).close()

        run_migrations(str(db), project_root=str(REPO_ROOT))

        with sqlite3.connect(db) as conn:
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        assert version == HEAD_REVISION
        # baseline created every table (PRAGMA table_info returns its columns)
        assert "id" in _table_info(db, "novel")
        assert "id" in _table_info(db, "tag_aliases")
        assert "ix_novel_shuffle_like_text" in _index_names(db)

    def test_orm_models_load_after_migration(self, tmp_path):
        """ORM metadata matches the migrated schema (no missing columns)."""
        db = tmp_path / "orm.db"
        _make_v1_db(db)
        run_migrations(str(db), project_root=str(REPO_ROOT))

        engine = create_engine(f"sqlite:///{db}")
        from sqlalchemy import inspect
        insp = inspect(engine)
        orm_tables = {
            "novel", "author", "series", "tag", "novel_tag", "tag_aliases",
            "tag_preferences", "search_history", "task_history",
            "scheduled_tasks", "tokens", "favourite", "special_follow",
            "failed_novel", "novel_epub_conversions", "random_novel_pool",
        }
        assert orm_tables <= set(insp.get_table_names())
        # columns the ORM relies on
        novel_cols = {c["name"] for c in insp.get_columns("novel")}
        assert {"shuffle", "like", "view", "text", "has_epub"} <= novel_cols
        engine.dispose()
