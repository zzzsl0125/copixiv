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

AUTHOR_EXT_ID_SEQUENCE_SQL = """
CREATE SEQUENCE IF NOT EXISTS author_ext_id_seq
    AS bigint
    START WITH 1
    INCREMENT BY 1
    MINVALUE 1
    NO CYCLE
"""

SERIES_EXT_ID_SEQUENCE_SQL = """
CREATE SEQUENCE IF NOT EXISTS series_ext_id_seq
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
    for stmt in (
        LOCAL_COORD_SEQUENCE_SQL,
        AUTHOR_EXT_ID_SEQUENCE_SQL,
        SERIES_EXT_ID_SEQUENCE_SQL,
        *TABLE_ANALYZE_TUNING_STATEMENTS,
    ):
        conn.execute(text(stmt))


def uninstall_pg_objects(conn: Connection) -> None:
    """Drop custom sequences cleanly."""
    for seq in ("novel_local_coord_seq", "author_ext_id_seq", "series_ext_id_seq"):
        conn.execute(text(f"DROP SEQUENCE IF EXISTS {seq}"))


__all__ = [
    "AUTHOR_EXT_ID_SEQUENCE_SQL",
    "LOCAL_COORD_SEQUENCE_SQL",
    "SERIES_EXT_ID_SEQUENCE_SQL",
    "TABLE_ANALYZE_TUNING_STATEMENTS",
    "install_pg_objects",
    "uninstall_pg_objects",
]
