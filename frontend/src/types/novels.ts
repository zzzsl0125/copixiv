/** Novel domain types.
 *
 * 后端 Pydantic 模型的**唯一真相**在 `openapi.json`；本模块只做
 * `api.generated.ts` 上的友好别名，字段不再手抄。客户端专有的查询状态
 * （`NovelFilters` / `GetNovelsParams`）不是 API 实体，仍手写在这里。
 */
import type { components } from './api.generated'

type S = components['schemas']

export type Novel = S['NovelBase']
export type NovelListResult = S['NovelListResponse']
/** 单本元数据编辑的可提交字段（`PATCH /api/novels/{id}`，三态见后端 schemas）。 */
export type NovelUpdatePayload = S['NovelUpdateRequest']

/** 作者 ID 预览（`GET /api/novels/author/{author_id}`）。 */
export type AuthorLookup = S['AuthorLookupResponse']
/** 系列 ID 预览（`GET /api/novels/series/{series_id}`）—— 与作者那份同构。 */
export type SeriesLookup = S['SeriesLookupResponse']

/** `/api/novels/count` 响应：total 可见数，excluded 被厌恶标签隐藏数。 */
export type NovelCountResult = S['NovelCountResponse']

/** Which novels a batch operation applies to (mirrors backend BatchScope). */
export type BatchScope = S['BatchScope']
/** Batch delete / add_tags / remove_tags. */
export type BatchOperation = S['BatchOperationRequest']['operation']
export type BatchOperationResult = S['BatchOperationResponse']

/** 批量编辑「作者」/「系列」（`POST /api/novels/batch-metadata`）。 */
export type BatchMetadataRequest = S['BatchMetadataRequest']
export type BatchMetadataResult = S['BatchMetadataResponse']

/** Matching IDs for the 「全选匹配」bulk-add action. */
export type NovelIdsResponse = S['NovelIdsResponse']
/** Novels fetched by explicit ID list (「查看已选」view). */
export type NovelsByIdsResponse = S['NovelsByIdsResponse']
/** Intersection of a selection with the current search scope (scoped clear). */
export type MatchIdsResult = S['MatchIdsResponse']

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
