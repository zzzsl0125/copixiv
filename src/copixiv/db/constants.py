"""Table and column name constants."""

# Table Names
TABLE_NOVEL = "novel"
# (No search-table constant: keyword search runs in copixiv_rs.)
TABLE_AUTHOR = "author"
TABLE_SERIES = "series"
TABLE_TAG = "tag"
TABLE_FAILED_NOVEL = "failed_novel"
# Ledger of series pixiv no longer serves (``novel_series`` keeps answering
# 404).  A *verdict* table, not entity data: it only tells the repair task's
# candidate query which series are not worth re-fetching.
TABLE_SERIES_DEAD_LETTER = "series_dead_letter"
TABLE_SEARCH_HISTORY = "search_history"
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
# ``novel.source`` — where the row came from.  Value domain:
# ``'pixiv'`` (scraper) | ``'local'`` (a TXT / EPUB uploaded through
# 「我的上传」, docs/LIBRARY_DESIGN.md §2).
COL_SOURCE = "source"
COL_TEXTS = "text"
COL_LIKES = "like"
COL_VIEWS = "view"
COL_SERIES_ID = "series_id"
COL_SERIES_NAME = "series_name"
COL_SERIES_INDEX = "series_index"
COL_CREATE_TIME = "create_time"
COL_HAS_EPUB = "has_epub"

# Query Fields
FIELD_KEYWORD = "keyword"
FIELD_TAGS = "tags"
FIELD_IS_FAVOURITE = "is_favourite"
FIELD_IS_SPECIAL_FOLLOW = "is_special_follow"

# Order By Options
ORDER_BY_RANDOM = "random"
