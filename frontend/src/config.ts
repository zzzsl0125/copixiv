/**
 * Frontend-bundled config defaults.
 *
 * The backend no longer serves ``default_min_like`` / ``default_min_text``
 * (the ``frontend`` config section was removed in the config cleanup); the
 * default filter values live here in the client instead.
 */

export const DEFAULT_MIN_LIKE = 500
export const DEFAULT_MIN_TEXT = 3000

/** 分页页大小：浏览列表（useNovels per_page）与「查看已选/被排除」集合视图
 * （usePagedNovelIdView 每页切片）共用，改页大小只改这一处。 */
export const DEFAULT_PAGE_SIZE = 30
