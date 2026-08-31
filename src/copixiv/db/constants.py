"""Table and column name constants.

postgres-migration: table names now match the greenfield target schema
(``db_greenfield_design.md`` §4).  Several old SQLite-era names have been
dropped or repointed; the placeholders below are kept ONLY so that the
not-yet-rewritten repository layer (phase 2) imports without an
``AttributeError`` at module load.  Each placeholder is annotated
DEPRECATED and must be cleaned up in phase 2.
"""

# Table Names
TABLE_NOVEL = "novel"
# DEPRECATED placeholder (phase 2): the char-gram search table is now
# ``novel_search``; old FTSManager code (``features/novels/fts.py``) still
# references ``TABLE_NOVEL_FTS`` and must be rewritten in phase 2.
TABLE_NOVEL_FTS = "novel_search"
TABLE_NOVEL_SEARCH = "novel_search"
TABLE_AUTHOR = "author"
TABLE_SERIES = "series"
TABLE_TAG = "tag"
# DEPRECATED placeholder (phase 2): the novel_tag join table is gone —
# tags now live in ``novel.tags text[]``.  Kept only so repository code
# referencing ``C.TABLE_NOVEL_TAG`` still imports.
TABLE_NOVEL_TAG = "novel_tag"
# DEPRECATED placeholder (phase 2): favourite is now ``novel.is_favourite``.
TABLE_FAVOURITE = "favourite"
# DEPRECATED placeholder (phase 2): special_follow is now
# ``author.is_special_follow``.
TABLE_SPECIAL_FOLLOW = "special_follow"
TABLE_FAILED_NOVEL = "failed_novel"
TABLE_SEARCH_HISTORY = "search_history"
TABLE_AUTHOR_CACHE = "author_cache"
TABLE_SERIES_CACHE = "series_cache"
TABLE_TASK_HISTORY = "task_history"
TABLE_SCHEDULED_TASK = "scheduled_task"
TABLE_TAG_PREFERENCE = "tag_preference"
TABLE_TAG_ALIAS = "tag_alias"
TABLE_TOKEN = "token"
TABLE_SETTINGS = "setting"

# Column Names
COL_ID = "id"
COL_TITLE = "title"
COL_AUTHOR_NAME = "author_name"
COL_AUTHOR_ID = "author_id"
COL_PATH = "path"
COL_SOURCE = "source"
COL_TEXTS = "text"
COL_LIKES = "like"
COL_VIEWS = "view"
COL_CAPTION = "caption"
COL_SERIES_ID = "series_id"
COL_SERIES_NAME = "series_name"
COL_SERIES_INDEX = "series_index"
COL_CREATE_TIME = "create_time"
COL_HAS_EPUB = "has_epub"
COL_INDEX = "index"
COL_TAGS = "tags"
COL_NOVEL_COUNT = "novel_count"

# Query Fields
FIELD_KEYWORD = "keyword"
FIELD_TAGS = "tags"
FIELD_IS_FAVOURITE = "is_favourite"
FIELD_IS_SPECIAL_FOLLOW = "is_special_follow"

# Order By Options
ORDER_BY_RANDOM = "random"
ORDER_BY_NONE = "none"
