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
}

export interface NovelFilters {
  keyword: string
  order_by: string
  order_direction: string
  min_like?: number
  min_text?: number
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
