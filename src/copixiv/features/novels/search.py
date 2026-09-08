r"""Keyword search — the query half of the char-gram contract.

The index is **owned by PostgreSQL**, not by application code: migration 0003
replaced the ``novel_search`` derived table with an expression GIN index over
``novel`` itself::

    CREATE INDEX novel_search_gin ON novel USING gin (
      to_tsvector('simple', copixiv_novel_text(title, author_name, series_name, tags))
    )

Because the indexed expression reads the row being written, every writer —
repository, background task, migration script, ``psql`` — keeps the index
consistent with the data.  (The previous derived table was refreshed by five
repository call sites and forgotten by the author-name writeback, so
``search_text`` could silently drift.)

Two halves share one token-stream definition:

* **storage side** — the database functions ``copixiv_gram`` /
  ``copixiv_novel_text`` (created by migration 0003);
* **query side** — :func:`gram_tokenize` / :func:`build_tsquery` here, which
  turn a user keyword into a PostgreSQL ``tsquery`` phrase list.

:func:`gram_tokenize` is a deliberate byte-for-byte mirror of ``copixiv_gram``
(ASCII alphanumerics and non-ASCII characters kept, whitespace dropped,
ASCII punctuation/controls → ``龖``); the two implementations are pinned
together by ``tests/features/test_search_index.py`` so a divergence fails
loudly instead of silently returning zero hits.

Query semantics (unchanged from the jieba-replacement design):

* whitespace separates segments, which are AND-ed — ``哈利 波特`` matches
  novels containing both ``哈利`` and ``波特`` anywhere;
* a segment without whitespace is a contiguous substring match —
  ``哈利波特`` → the phrase ``'哈 利 波 特'``;
* a segment with no letter/digit carries no meaning and is dropped, so a
  keyword that collapses to nothing means "no filter" rather than "no results".
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text as _text
from sqlalchemy.orm import Session

# The placeholder substituted for every ASCII punctuation/control character on
# BOTH the storage and query sides.  U+9F96 is a CJK unified ideograph: a token
# character for the ``simple`` tokeniser, in the same Unicode block as CJK
# text, and effectively absent from the corpus.  Must match ``copixiv_gram``.
_GRAM_PLACEHOLDER = "龖"

# Whitespace code points — exactly Python's ``str.isspace()`` set, mirrored
# character for character by the database function ``copixiv_gram`` (which
# cannot use ``[[:space:]]``: POSIX classes follow the database locale, and
# this deployment runs with the ``C`` locale).  Keeping the two sides in
# lockstep is what makes index-side whitespace collapse and query-side
# whitespace segmentation agree.
_WHITESPACE = frozenset(
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)

# The index name is referenced by the maintenance task (REINDEX) and by the
# health check; both live here so the database object has one owner in code.
_INDEX_NAME = "novel_search_gin"

# The keyword predicate.  The expression is written exactly as in the index
# definition (migration 0003) so PostgreSQL can use the GIN index for it.
_FTS_PREDICATE = (
    "to_tsvector('simple', "
    "copixiv_novel_text(novel.title, novel.author_name, "
    "novel.series_name, novel.tags)) "
    "@@ to_tsquery('simple', :fts_query)"
)


def gram_tokenize(text: str) -> str:
    """Convert *text* into character-unigram token text (char-gram).

    Mirror of the database function ``copixiv_gram`` (migration 0003).  Both
    sides MUST produce identical strings — any divergence silently breaks every
    keyword search, so the pair is pinned by a cross-check test.

    Rules (per character, joined by a single space):

    * whitespace (:data:`_WHITESPACE`) is dropped — it carries no meaning as a
      token, and dropping it keeps ``哈利波特`` matching ``哈利 波特``;
    * ASCII alphanumerics are kept as themselves (case preserved; ``simple``
      folds case on both sides anyway);
    * non-ASCII characters are kept as themselves — the ``simple`` tokeniser
      recognises every one of them (CJK, kana, Cyrillic, symbols, emoji), and
      POSIX classes cannot be used for the test because they follow the
      database locale, which is ``C`` here;
    * every other character (ASCII punctuation, symbols, controls) maps to the
      placeholder ``龖``, which keeps punctuation addressable: ``R-18`` becomes
      ``R 龖 1 8`` and never matches ``R18``.

    Examples::

        gram_tokenize("普通文本") == "普 通 文 本"
        gram_tokenize("R-18")     == "R 龖 1 8"
    """
    if not text:
        return ""
    chars: list[str] = []
    for ch in text:
        if ch in _WHITESPACE:
            continue
        if ch.isascii() and not ch.isalnum():
            chars.append(_GRAM_PLACEHOLDER)
        else:
            chars.append(ch)
    return " ".join(chars)


def build_tsquery(keyword: str) -> str:
    """Convert a keyword string into a PostgreSQL ``tsquery`` phrase list.

    Returns the text bound to ``to_tsquery('simple', :fts_query)``: each
    whitespace-separated segment becomes a single-quoted phrase (adjacency
    match), segments are joined with ``&`` (AND).  An empty string means
    "no keyword filter" — the caller must not add a predicate for it.

    Phrase quoting is safe because :func:`gram_tokenize` maps every
    non-alphanumeric character (including ``'``) to the placeholder, so the
    only quote characters in the result are the phrase delimiters.
    """
    if not keyword or not keyword.strip():
        return ""

    phrases: list[str] = []
    for segment in keyword.split():
        # A segment with no letter/digit (e.g. ``---``) carries no search
        # meaning: it cannot form a phrase, so it is dropped rather than
        # turning the whole query into a no-hit filter.
        if not any(ch.isalpha() or ch.isnumeric() for ch in segment):
            continue
        phrases.append(f"'{gram_tokenize(segment)}'")

    return " & ".join(phrases)


def keyword_condition(keyword: str) -> Any | None:
    """SQLAlchemy condition matching *keyword*, or ``None`` for "no filter".

    The returned expression is the char-gram phrase predicate against the
    expression index; ``None`` lets callers skip the WHERE clause entirely
    (the "empty keyword filters nothing" contract).
    """
    tsquery = build_tsquery(keyword)
    if not tsquery:
        return None
    return _text(_FTS_PREDICATE).bindparams(fts_query=tsquery)


# ---------------------------------------------------------------------------
# Index maintenance (the content is derived; only the physical index needs ops)
# ---------------------------------------------------------------------------


def reindex(session: Session) -> None:
    """Rebuild the search index (bloat maintenance, not a data repair).

    Nothing to recompute — the index entry is derived from the ``novel`` row
    by PostgreSQL — so a rebuild is a plain ``REINDEX``.
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
