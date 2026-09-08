"""search index becomes an expression GIN index on ``novel``

Revision ID: 0003_search_expression_index
Revises: 0002_tag_refs_batch
Create Date: 2026-09-10 00:00:00.000000

Replaces the application-maintained ``novel_search`` derived table with an
expression index over ``novel`` itself.

Why (engineering, not features): ``novel_search.search_text`` is derived from
``novel.title + author_name + series_name + tags``, but nothing in the schema
enforced that derivation.  Five repository call sites refreshed it and at
least one write path forgot (``SQLAlchemyAuthorRepository.update_author_name``
— the author-name writeback that runs after every webview download), so the
index could silently drift from the columns it is derived from.  An
expression index cannot drift: PostgreSQL recomputes the index entry from the
row being written, so every writer — repository, task, migration script,
``psql`` — keeps it correct by construction.

Two IMMUTABLE SQL functions own the token stream:

* ``copixiv_gram(text)`` — character-unigram ("char-gram") mapping: characters
  are kept as themselves when they are ASCII alphanumeric **or** non-ASCII
  (every script, symbol and emoji the ``simple`` tokeniser recognises),
  whitespace is dropped, and every other character — i.e. ASCII punctuation
  and control characters — becomes the placeholder ``龖`` (a token character,
  so punctuation stays addressable in phrase queries and ``R-18`` never
  collapses into ``R18``).  The classifier is spelled out in ASCII ranges on
  purpose: POSIX classes such as ``[[:alnum:]]`` follow the database locale,
  which here is ``C`` and therefore classifies *no* CJK character as
  alphanumeric.  The application keeps a Python mirror of this mapping
  (``copixiv.features.novels.search.gram_tokenize``) for building query
  phrases; a test asserts the two agree character by character.
* ``copixiv_novel_text(title, author_name, series_name, tags)`` — assembles the
  per-novel text from the four source columns (empty parts dropped, so an empty
  author never leaves a stray separator).

Downgrade recreates the derived table and repopulates it from ``novel`` with
the same functions, so it is lossless.
"""

from typing import Union, Sequence

from alembic import op

revision: str = "0003_search_expression_index"
down_revision: Union[str, Sequence[str], None] = "0002_tag_refs_batch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The character mapping, three regex passes over the whole string (no per-char
# PL/pgSQL loop, so an index build stays a linear C-level scan):
#   1. drop whitespace — the exact code points of Python's ``str.isspace()``,
#      spelled out because ``[[:space:]]`` is ASCII-only in the C locale;
#   2. every ASCII non-alphanumeric becomes the placeholder ``龖``;
#   3. insert a space after each character, so the ``simple`` tokeniser sees one
#      token per character (the trailing space is trimmed).
# ``PARALLEL SAFE`` matters: index builds and bitmap scans run this in parallel
# workers.
_GRAM_FUNCTION = r"""
CREATE OR REPLACE FUNCTION copixiv_gram(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT rtrim(
           regexp_replace(
             regexp_replace(
               regexp_replace(
                 coalesce(txt, ''),
                 '[\x09-\x0D\x1C-\x1F\x20\x85\xA0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]',
                 '', 'g'),
               '[\x00-\x2F\x3A-\x40\x5B-\x60\x7B-\x7F]', '龖', 'g'),
             '(.)', '\1 ', 'g'))
$$;
"""

# The per-novel text.  ``concat_ws`` skips NULLs; ``nullif(..., '')`` turns
# empty strings into NULLs so an empty author/series does not contribute an
# empty part (matching the Python reference implementation exactly).
_NOVEL_TEXT_FUNCTION = r"""
CREATE OR REPLACE FUNCTION copixiv_novel_text(
  title text, author_name text, series_name text, tags text[]
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT copixiv_gram(concat_ws(
    ' ',
    nullif(title, ''),
    nullif(author_name, ''),
    nullif(series_name, ''),
    nullif(array_to_string(coalesce(tags, '{}'::text[]), ' '), '')
  ));
$$;
"""

_EXPRESSION_INDEX = """
CREATE INDEX novel_search_gin ON novel USING gin (
  to_tsvector('simple', copixiv_novel_text(title, author_name, series_name, tags))
)
"""


def upgrade() -> None:
    op.execute(_GRAM_FUNCTION)
    op.execute(_NOVEL_TEXT_FUNCTION)
    # The derived table (and its GIN index) is replaced by the expression
    # index below.  Nothing else depends on it: deletes used to rely on the FK
    # cascade, and reads always went through the repository query builder.
    op.execute("DROP TABLE novel_search")
    op.execute(_EXPRESSION_INDEX)


def downgrade() -> None:
    # Drop the expression index first: the derived table reuses its name, and
    # the functions cannot be dropped while the index depends on them.
    op.execute("DROP INDEX novel_search_gin")
    op.execute(
        """
        CREATE TABLE novel_search (
            novel_id BIGINT PRIMARY KEY
                REFERENCES novel(id) ON DELETE CASCADE,
            search_text TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO novel_search (novel_id, search_text)
        SELECT id, copixiv_novel_text(title, author_name, series_name, tags)
        FROM novel
        """
    )
    op.execute(
        "CREATE INDEX novel_search_gin ON novel_search "
        "USING gin (to_tsvector('simple', search_text))"
    )
    op.execute("DROP FUNCTION copixiv_novel_text(text, text, text, text[])")
    op.execute("DROP FUNCTION copixiv_gram(text)")
