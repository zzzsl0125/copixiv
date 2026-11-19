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

/**
 * 排序字段。取值直接取自后端 `schemas.py` 的
 * `Literal["id","like","view","text","create_time","random"]`。
 *
 * 为什么必须是字面量联合而不是 `string`：GET /api/novels/ 的 `order_by` 在
 * OpenAPI 里**没有 enum**（Pydantic 侧就是裸 `str`），而 `repo.py` 对未知字段
 * 走 `getattr(model, order_by, None)` → 拿到 None 就**静默不加排序**。也就是说
 * `order_by=lkke` 不会报错、不会 400，只会让列表悄悄退回默认顺序。
 * 唯一能在这个拼写错误到达网络之前拦住它的地方，就是这个类型。
 */
export type OrderBy = 'id' | 'like' | 'view' | 'text' | 'create_time' | 'random'

/** 排序方向。同样是后端 `Literal["ASC","DESC"]` 的镜像。 */
export type OrderDirection = 'ASC' | 'DESC'

export type SortableOrderBy = S['SortableOrderBy']
export type FormatMode = S['FormatMode']

/** 运行时校验：URL 查询参数是不可信输入，必须过这一道才能进 `NovelFilters`。 */
const ORDER_BY_VALUES: readonly OrderBy[] = ['id', 'like', 'view', 'text', 'create_time', 'random']
export function isOrderBy(value: string): value is OrderBy {
  return (ORDER_BY_VALUES as readonly string[]).includes(value)
}
export function isOrderDirection(value: string): value is OrderDirection {
  return value === 'ASC' || value === 'DESC'
}

/**
 * 从查询参数解析排序，**非法值一律回落到缺省**。
 *
 * 三处调用点（首屏、popstate 回退、测试夹具）此前各写一遍
 * `params.get('order_by') || 'random'`，那只能兜住「缺省」、兜不住「乱写」：
 * `?order_by=lkke` 会原样进入 filters → 发到后端 → `getattr(..., None)` →
 * 静默无排序。收到这个函数里，一处修好三处生效。
 */
export function parseOrderBy(raw: string | null): OrderBy {
  return raw !== null && isOrderBy(raw) ? raw : 'random'
}

export function parseOrderDirection(raw: string | null): OrderDirection {
  return raw !== null && isOrderDirection(raw) ? raw : 'DESC'
}

export interface NovelFilters {
  keyword: string
  order_by: OrderBy
  order_direction: OrderDirection
  min_like?: number
  min_text?: number
}

export interface GetNovelsParams {
  keyword?: string
  order_by?: OrderBy
  order_direction?: OrderDirection
  cursor?: Record<string, unknown>
  per_page?: number
  min_like?: number
  min_text?: number
  excluded_ids?: number[]
  /** 排除厌恶标签小说：true/undefined → 排除；false → 本次不排除 */
  exclude_blocked?: boolean
}
