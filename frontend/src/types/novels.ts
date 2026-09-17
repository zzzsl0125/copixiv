/** Novel domain types */

export interface Novel {
  id: number
  title: string
  author_id: number
  author_name?: string
  series_id?: number
  series_name?: string
  series_index?: number
  like: number
  view: number
  text: number
  caption?: string
  create_time?: string
  has_epub: number
  tags: string[]
  is_favourite: number
  is_special_follow: number
  /** 来源：'pixiv'（缺省）/ 'local'（本地 TXT 上传，id 为负）。见 LIBRARY_DESIGN.md §2 */
  source?: string
}

export interface NovelFilters {
  keyword: string
  order_by: string
  order_direction: string
  min_like?: number
  min_text?: number
}

/**
 * 单本元数据编辑的可提交字段（`PATCH /api/novels/{id}`，docs/NOVEL_EDIT_DESIGN.md §5）。
 *
 * PATCH 三态：键不出现 = 不动；显式 `null` = 清空（`series_name` /
 * `series_index` / `create_time`）。
 *
 * 服务端执法（不是静默忽略）：
 * - `title` / `series_name` **仅本地书**可提交，pixiv 书提交 → 400；
 * - `caption` 全局不可编辑，与 `source` / `path` / `id` / `has_epub` 等结构性
 *   字段一样，提交即被 `extra="forbid"` 拒掉 → 422。
 */
export interface NovelUpdatePayload {
  title?: string
  author_name?: string
  series_name?: string | null
  series_index?: number | null
  tags?: string[]
  create_time?: string | null
}

export interface GetNovelsParams {
  keyword?: string
  order_by?: string
  order_direction?: string
  cursor?: Record<string, unknown>
  per_page?: number
  min_like?: number
  min_text?: number
  excluded_ids?: number[]
  /** 排除厌恶标签小说：true/undefined → 排除；false → 本次不排除 */
  exclude_blocked?: boolean
}

/** /api/novels/count 响应：total 可见数，excluded 被厌恶标签隐藏数 */
export interface NovelCountResult {
  total: number
  excluded: number
}

/** /api/novels/ 响应：has_excluded 首屏附带「范围内是否存在被厌恶标签排除的小说」 */
export interface NovelListResult {
  novels: Novel[]
  cursor: Record<string, unknown> | null
  has_excluded: boolean
}

/** Which novels a batch operation applies to (mirrors backend BatchScope). */
export interface BatchScope {
  mode: 'ids' | 'all_matched'
  novel_ids: number[]
  keyword?: string
  min_like?: number
  min_text?: number
  excluded_ids: number[]
}

export type BatchOperation = 'delete' | 'add_tags' | 'remove_tags'

export interface BatchOperationResult {
  matched: number
  affected: number
}

/** Matching IDs for the 「全选匹配」bulk-add action. */
export interface NovelIdsResponse {
  ids: number[]
  total: number
  truncated: boolean
}

/** Novels fetched by explicit ID list (「查看已选」view). */
export interface NovelsByIdsResponse {
  novels: Novel[]
  truncated: boolean
}

/** Intersection of a selection with the current search scope (scoped clear). */
export interface MatchIdsResult {
  matching_ids: number[]
  truncated: boolean
}
