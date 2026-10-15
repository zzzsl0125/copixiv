r"""Keyword search — the query half of the bigram (substring) contract.

The index is **owned by PostgreSQL**, not by application code: migration 0007
materialised the searchable text as a generated column and indexed it with
``pg_bigm``'s bigram GIN opclass::

    -- the assembly itself lives in copixiv_search_text(), an IMMUTABLE SQL
    -- function created by migration 0007 (a STORED generated column cannot
    -- call the STABLE pg_catalog concat_ws / array_to_string directly)
    ALTER TABLE novel ADD COLUMN search_lower text GENERATED ALWAYS AS (
      copixiv_search_text(title, author_name, series_name, tags)) STORED;
    CREATE INDEX novel_search_bigm ON novel USING gin (search_lower gin_bigm_ops);

The function body is ``lower(concat_ws(' ', nullif(title,''),
nullif(author_name,''), nullif(series_name,''),
nullif(array_to_string(coalesce(tags,'{}'::text[]),' '),'')))`` — i.e. the old
``copixiv_novel_text(...)`` composition without the char-gram tokenisation and
without case.

Because the column is generated from the row being written, every writer —
repository, background task, migration script, ``psql`` — keeps the index
consistent with the data.  (The pre-0003 derived table was refreshed by five
repository call sites and forgotten by the author-name writeback, so
``search_text`` could silently drift.  Migration 0007 keeps the same
"PostgreSQL derives it" guarantee — it does not reintroduce a refresh path.)

Query semantics (``docs/perf/PG_BIGM_FEASIBILITY.md`` has the measurements):

* whitespace separates segments, which are AND-ed — ``哈利 波特`` matches
  novels containing both ``哈利`` and ``波特`` anywhere;
* a segment matches iff it occurs as a **contiguous substring** of the
  concatenated, lowercased title/author/series/tags text;
* a segment with no letter/digit carries no meaning and is dropped, so a
  keyword that collapses to nothing means "no filter" rather than "no results".

This replaces the char-gram/``tsquery`` design built by migration 0003, and
the change is deliberate on two counts:

* **correctness** — ``copixiv_gram`` dropped whitespace, so two adjacent
  *words* became adjacent *tokens* and phrase matching crossed the word
  boundary: a novel tagged ``['R-18','gay']`` matched the search ``R-18G``
  (3,523 pure false positives, none of them containing the substring).  A
  substring match has no token positions, so that class of bug cannot exist.
* **planning** — ``tsvector @@ tsquery`` ignores column statistics for the
  indexed expression (real 15,780 hits estimated as 1,092), which made the
  planner choose a 660ms ``Bitmap Heap Scan`` for mid-frequency keywords.
  ``pg_bigm`` estimates from GIN index statistics (error ≤1.4×), so the cliff
  is gone rather than worked around.

**The one remaining footgun: case folding must be the same function on both
sides.**  The stored column is produced by PostgreSQL's ``lower()`` — and this
deployment runs with ``datcollate = datctype = C``, where ``lower()`` folds
ASCII only (``lower('Ñ') = 'Ñ'``, ``lower('Ä') = 'Ä'``).  The predicate
therefore lowercases the *pattern* in SQL (``search_lower LIKE lower(:pN)``)
instead of in Python: both sides then provably call the same function.  Had we
lowered in Python, ``str.lower()`` would fold ``'Ñ'``/``'Ä'``/``'İ'`` that the
database leaves alone, and searching such a keyword would silently return
nothing.  ``tests/features/test_search_index.py`` pins the observable contract
(``Chinese`` and ``chinese`` return identical result sets) end to end.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text as _text
from sqlalchemy.orm import Session

# The index name is referenced by the maintenance task (REINDEX) and by the
# health check; both live here so the database object has one owner in code.
_INDEX_NAME = "novel_search_bigm"

# One ``LIKE`` per keyword segment (AND-ed).  The column reference is left
# unqualified on purpose: the query builder selects from ``novel`` with no
# alias, and an unqualified column also survives a future ``.alias()``.
# ``ESCAPE '\'`` pairs with :func:`escape_like` (standard_conforming_strings
# is on, so the literal is a single backslash).
_LIKE_PREDICATE = "search_lower LIKE lower(:p{n}) ESCAPE '\\'"


def normalize_segments(keyword: str) -> list[str]:
    """Split *keyword* on whitespace, dropping segments with no letter/digit.

    This is the "a keyword that collapses to nothing filters nothing"
    contract: ``normalize_segments("---")`` is empty, so callers add no
    predicate at all instead of a no-hit one.  ``str.split()`` uses Python's
    whitespace set (the same set the old query-side segmentation used).
    """
    if not keyword:
        return []
    return [
        segment
        for segment in keyword.split()
        if any(ch.isalpha() or ch.isnumeric() for ch in segment)
    ]


def escape_like(value: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` for a ``LIKE ... ESCAPE '\\'`` pattern.

    Exported for other ``LIKE``/``ILIKE`` call sites (the tag-alias
    suggestion path builds its own patterns and currently does *not* escape —
    ``%高中生`` there matches every tag).
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def keyword_params(keyword: str) -> dict[str, str]:
    """Bind values for :func:`like_predicate` — one ``%…%`` pattern per segment.

    ``\\``, ``%`` and ``_`` are escaped (:func:`escape_like`) so a tag that
    literally contains them (``%高中生``, ``BanG_Dream!``) matches only itself.

    **Case is deliberately left alone here**: the predicate lowercases the
    pattern with the database's own ``lower()``, which is by construction the
    same function that produced ``search_lower`` (see the module docstring).
    """
    return {
        f"p{index}": f"%{escape_like(segment)}%"
        for index, segment in enumerate(normalize_segments(keyword))
    }


def like_predicate(keyword: str) -> str | None:
    """The ``LIKE`` predicate for *keyword*, or ``None`` for "no filter".

    One ``search_lower LIKE lower(:p0) ESCAPE '\\'`` per whitespace-separated
    segment, joined with ``AND``; bind parameter names match
    :func:`keyword_params`.  ``None`` means the keyword carries no searchable
    segment (empty or pure punctuation), which callers translate into "skip
    the WHERE clause" — never into "match nothing".
    """
    segments = normalize_segments(keyword)
    if not segments:
        return None
    return " AND ".join(
        _LIKE_PREDICATE.format(n=index) for index in range(len(segments))
    )


def keyword_condition(keyword: str) -> Any | None:
    """SQLAlchemy condition matching *keyword*, or ``None`` for "no filter".

    The returned expression is the per-segment ``LIKE`` predicate against
    ``novel.search_lower`` (the ``pg_bigm`` GIN index answers it); ``None``
    lets callers skip the WHERE clause entirely (the "empty keyword filters
    nothing" contract).
    """
    predicate = like_predicate(keyword)
    if predicate is None:
        return None
    return _text(predicate).bindparams(**keyword_params(keyword))


# ---------------------------------------------------------------------------
# Index maintenance (the column is generated; only the physical index needs ops)
# ---------------------------------------------------------------------------


def reindex(session: Session) -> None:
    """Rebuild the search index (bloat maintenance, not a data repair).

    Nothing to recompute — ``search_lower`` is generated by PostgreSQL from
    the row — so a rebuild is a plain ``REINDEX``.
    """
    session.execute(_text(f"REINDEX INDEX {_INDEX_NAME}"))


def index_health(session: Session) -> dict:
    """Report the search index's presence/validity plus the novel count.

    Content drift is impossible by construction, so health is a question of
    the physical index: does it exist, and is it valid (``REINDEX``/``CREATE
    INDEX CONCURRENTLY`` failures leave ``indisvalid = false``)?
    """
    result: dict = {
        "index_exists": False,
        "is_valid": False,
        "novel_count": 0,
        "is_healthy": False,
        "error": None,
    }
    try:
        row = session.execute(
            _text(
                "SELECT i.indisvalid FROM pg_index i "
                "WHERE i.indexrelid = to_regclass(:index_name)"
            ),
            {"index_name": _INDEX_NAME},
        ).first()
        result["index_exists"] = row is not None
        result["is_valid"] = bool(row[0]) if row is not None else False
        result["novel_count"] = session.execute(
            _text("SELECT count(*) FROM novel")
        ).scalar() or 0
        result["is_healthy"] = result["index_exists"] and result["is_valid"]
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = str(exc)
        result["is_healthy"] = False
    return result
