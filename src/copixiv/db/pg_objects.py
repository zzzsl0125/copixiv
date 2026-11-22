"""PostgreSQL-only custom DDL objects (sequences and table tuning).

Single source of truth imported by Alembic migrations so ``alembic check``
and PostgreSQL runtime behavior stay identical.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

LOCAL_COORD_SEQUENCE_SQL = """
CREATE SEQUENCE IF NOT EXISTS novel_local_coord_seq
    AS bigint
    START WITH 1
    INCREMENT BY 1
    MINVALUE 1
    NO CYCLE
"""

TABLE_ANALYZE_TUNING_STATEMENTS = (
    "ALTER TABLE novel SET (autovacuum_analyze_scale_factor = 0.02, autovacuum_analyze_threshold = 500)",
)


def install_pg_objects(conn: Connection) -> None:
    """Install all custom sequences and table tuning idempotently."""
    conn.execute(text(LOCAL_COORD_SEQUENCE_SQL))
    for stmt in TABLE_ANALYZE_TUNING_STATEMENTS:
        conn.execute(text(stmt))


def uninstall_pg_objects(conn: Connection) -> None:
    """Drop custom sequences cleanly."""
    conn.execute(text("DROP SEQUENCE IF EXISTS novel_local_coord_seq"))


__all__ = [
    "LOCAL_COORD_SEQUENCE_SQL",
    "TABLE_ANALYZE_TUNING_STATEMENTS",
    "install_pg_objects",
    "uninstall_pg_objects",
]
